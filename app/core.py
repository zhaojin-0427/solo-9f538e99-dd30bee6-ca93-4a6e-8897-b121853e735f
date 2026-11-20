"""校时换算、批量写入、重算与冲突检测的核心逻辑。

状态机（observations.status）：
  ok                   已校时且无冲突，供分析
  pending_calibration  计数落在本段校时区间外，保留原文、待校时
  conflict             不同开机段落在同一 UTC 毫秒，冲突观测均不供分析

校时依据有两种来源，合并后共同参与换算：

  * 单点（calibration_points）：旧库已有或经 /calibrations 后补；
    同段同计数时优先于等步长串，也可插在串内两点之间。
  * 等步长串（calibration_series）：每段至多一串，只存首点与步长、点数，
    绝不逐点展开。

合并后的有效校时点序列 = 串上的全部点被同计数单点替换后，再并入其余单点。
换算、上下界与冲突都按这一序列计算，因此与“把整串逐点存入”完全一致。

所有函数都假定运行在 db.transaction 包裹的同一事务里，
因此“原文 + 校时依据 + 分析供数”同次生效。
"""

from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass
from typing import Iterable

OK = "ok"
PENDING = "pending_calibration"
CONFLICT = "conflict"

SQLITE_MAX_INT = 2**63 - 1
MAX_SERIES_POINTS = 1_000_000


class SeriesValueError(ValueError):
    """等步长串的字段超出允许范围。"""


# ---------------------------------------------------------------- 等步长串

@dataclass(frozen=True)
class Series:
    """等步长校时串：首点 + 两个步长 + 点数。只占一行存储。"""

    start_counter: int
    start_utc_ms: int
    counter_step: int
    utc_step: int
    count: int

    def __post_init__(self) -> None:
        if not (1 <= self.count <= MAX_SERIES_POINTS):
            raise SeriesValueError(f"点数须在 1..{MAX_SERIES_POINTS} 之间")
        if self.counter_step <= 0:
            raise SeriesValueError("计数步长须为正")
        if self.utc_step < 0:
            raise SeriesValueError("UTC 毫秒步长须非负")
        if self.start_counter < 0 or self.start_utc_ms < 0:
            raise SeriesValueError("首点须在 SQLite 非负整数范围内")
        if self.last_counter > SQLITE_MAX_INT:
            raise SeriesValueError("末点计数超出 SQLite 非负整数范围")
        if self.last_utc_ms > SQLITE_MAX_INT:
            raise SeriesValueError("末点 UTC 毫秒超出 SQLite 非负整数范围")

    def point(self, index: int) -> tuple[int, int]:
        """第 index 个点（0 起）：(counter, utc_ms)，纯算术，不展开。"""
        return (
            self.start_counter + index * self.counter_step,
            self.start_utc_ms + index * self.utc_step,
        )

    @property
    def last_counter(self) -> int:
        return self.start_counter + (self.count - 1) * self.counter_step

    @property
    def last_utc_ms(self) -> int:
        return self.start_utc_ms + (self.count - 1) * self.utc_step


@dataclass(frozen=True)
class SegmentCalibration:
    """某开机段的合并校时视图（单点列表 + 至多一串）。"""

    explicit: tuple[tuple[int, int], ...]      # 按计数升序
    explicit_counters: tuple[int, ...]
    series: Series | None

    def has_any(self) -> bool:
        return bool(self.explicit) or self.series is not None

    def counter_range(self) -> tuple[int, int] | None:
        """合并后有效校时点的最小/最大计数；无点返回 None。"""
        lows: list[int] = []
        highs: list[int] = []
        if self.explicit:
            lows.append(self.explicit[0][0])
            highs.append(self.explicit[-1][0])
        if self.series is not None:
            lows.append(self.series.start_counter)
            highs.append(self.series.last_counter)
        if not lows:
            return None
        return min(lows), max(highs)

    def _explicit_overrides(self, counter: int) -> bool:
        i = bisect.bisect_left(self.explicit_counters, counter)
        return i < len(self.explicit_counters) and self.explicit_counters[i] == counter


def _series_candidates(
    series: Series, counter: int
) -> tuple[tuple[int, int] | None, tuple[int, int] | None, tuple[int, int] | None]:
    """从串本身找与 counter 的关系：(恰好命中, 严格下界, 严格上界)。

    只做常数次算术与比较，不遍历串内各点。
    """
    start, step, n = series.start_counter, series.counter_step, series.count
    if counter < start:
        return None, None, series.point(0)
    q = (counter - start) // step
    if q >= n:  # counter 严格大于末点；末点即严格下界
        return None, series.point(n - 1), None
    if start + q * step == counter:
        below = series.point(q - 1) if q > 0 else None
        above = series.point(q + 1) if q + 1 < n else None
        return series.point(q), below, above
    # point(q) < counter < point(q+1)，q+1 可能已越界
    return None, series.point(q), (series.point(q + 1) if q + 1 < n else None)


def _merged_exact_or_neighbors(
    cal: SegmentCalibration, counter: int
) -> tuple[tuple[int, int] | None, tuple[int, int] | None, tuple[int, int] | None]:
    """合并视图下的 (恰好命中, 严格下界, 严格上界)。"""
    ec = cal.explicit_counters
    i = bisect.bisect_left(ec, counter)
    if i < len(ec) and ec[i] == counter:
        p = cal.explicit[i]
        return p, None, None

    below = cal.explicit[i - 1] if i > 0 else None
    above = cal.explicit[i] if i < len(ec) else None

    if cal.series is not None:
        se, sb, sa = _series_candidates(cal.series, counter)
        if se is not None:  # 有效串点恰好命中；同计数单点已在上面优先返回
            return se, None, None
        # 同计数单点优先：被单点覆盖的串点不再是有效点
        if sb is not None and cal._explicit_overrides(sb[0]):
            sb = None
        if sa is not None and cal._explicit_overrides(sa[0]):
            sa = None
        if sb is not None and (below is None or sb[0] > below[0]):
            below = sb
        if sa is not None and (above is None or sa[0] < above[0]):
            above = sa

    return None, below, above


def _strict_neighbors(
    cal: SegmentCalibration, counter: int
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """已知 counter 处有单点时，合并视图下严格小于/大于它的最近有效点。"""
    ec = cal.explicit_counters
    i = bisect.bisect_left(ec, counter)
    below = cal.explicit[i - 1] if i > 0 else None
    above = cal.explicit[i + 1] if i + 1 < len(ec) else None

    if cal.series is not None:
        _, sb, sa = _series_candidates(cal.series, counter)
        # 与 counter 同计数的串点被该单点覆盖，不再作为邻居
        if sb is not None and (below is None or sb[0] > below[0]):
            below = sb
        if sa is not None and (above is None or sa[0] < above[0]):
            above = sa

    return below, above


# ---------------------------------------------------------------- 校时换算

def load_calibration(conn: sqlite3.Connection, segment: str) -> SegmentCalibration:
    """读取某开机段的合并校时视图（单点若干 + 至多一串，均不展开）。"""
    row = conn.execute(
        "SELECT start_counter, start_utc_ms, counter_step, utc_step, count "
        "FROM calibration_series WHERE boot_segment = ?",
        (segment,),
    ).fetchone()
    series = (
        Series(row["start_counter"], row["start_utc_ms"], row["counter_step"],
               row["utc_step"], row["count"])
        if row is not None
        else None
    )
    rows = conn.execute(
        "SELECT counter, utc_ms FROM calibration_points "
        "WHERE boot_segment = ? ORDER BY counter",
        (segment,),
    ).fetchall()
    explicit = tuple((r["counter"], r["utc_ms"]) for r in rows)
    return SegmentCalibration(
        explicit=explicit,
        explicit_counters=tuple(c for c, _ in explicit),
        series=series,
    )


def load_calibrations(
    conn: sqlite3.Connection, segments: Iterable[str]
) -> dict[str, SegmentCalibration]:
    return {seg: load_calibration(conn, seg) for seg in set(segments)}


def load_points(conn: sqlite3.Connection, segment: str) -> list[tuple[int, int]]:
    """某开机段的全部**单点**校时点，按计数升序。

    等步长串不在此展开；需要合并视图请用 load_calibration。
    """
    return list(load_calibration(conn, segment).explicit)


def compute_utc(
    cal: SegmentCalibration, counter: int
) -> tuple[int, tuple[int, int], tuple[int, int]] | None:
    """合并视图下相邻校时点间线性换算并向下取整。

    返回 (utc_ms, 下界点, 上界点)；计数落在校时区间外返回 None（待校时）。
    恰好命中某个校时点时直接取该点 UTC（上下界相同）。
    """
    if not cal.has_any():
        return None
    exact, lo, hi = _merged_exact_or_neighbors(cal, counter)
    if exact is not None:
        return exact[1], exact, exact
    if lo is None or hi is None:
        return None
    (c1, t1), (c2, t2) = lo, hi
    # 段内 UTC 单调不倒退已由写入方校验，故各项均非负，// 即向下取整
    utc_ms = t1 + (counter - c1) * (t2 - t1) // (c2 - c1)
    return utc_ms, lo, hi


def _pending_reason(segment: str, counter: int, cal: SegmentCalibration) -> str:
    rng = cal.counter_range()
    if rng is None:
        return f"开机段 '{segment}' 尚无校时点，待校时"
    return (
        f"计数 {counter} 超出开机段 '{segment}' 的校时区间 "
        f"[{rng[0]}, {rng[1]}]，待校时"
    )


# ---------------------------------------------------------------- 重算与冲突

def recalc_segment(
    conn: sqlite3.Connection,
    segment: str,
    cal: SegmentCalibration | None = None,
) -> None:
    """按当前合并校时视图重算某开机段全部观测的 utc_ms / 状态 / 校时依据。"""
    if cal is None:
        cal = load_calibration(conn, segment)
    rows = conn.execute(
        "SELECT obs_id, counter FROM observations WHERE boot_segment = ?",
        (segment,),
    ).fetchall()
    for row in rows:
        result = compute_utc(cal, row["counter"])
        if result is None:
            conn.execute(
                "UPDATE observations SET utc_ms = NULL, status = ?, reason = ?, "
                "cal_lo_counter = NULL, cal_lo_utc_ms = NULL, "
                "cal_hi_counter = NULL, cal_hi_utc_ms = NULL "
                "WHERE obs_id = ?",
                (PENDING, _pending_reason(segment, row["counter"], cal), row["obs_id"]),
            )
        else:
            utc_ms, (c_lo, t_lo), (c_hi, t_hi) = result
            conn.execute(
                "UPDATE observations SET utc_ms = ?, status = ?, reason = NULL, "
                "cal_lo_counter = ?, cal_lo_utc_ms = ?, "
                "cal_hi_counter = ?, cal_hi_utc_ms = ? "
                "WHERE obs_id = ?",
                (utc_ms, OK, c_lo, t_lo, c_hi, t_hi, row["obs_id"]),
            )


def conflict_ms_set(conn: sqlite3.Connection) -> set[int]:
    """被 >=2 个不同开机段的观测占用的 UTC 毫秒集合。"""
    rows = conn.execute(
        "SELECT utc_ms FROM observations WHERE utc_ms IS NOT NULL "
        "GROUP BY utc_ms HAVING COUNT(DISTINCT boot_segment) > 1"
    ).fetchall()
    return {r["utc_ms"] for r in rows}


def resolve_conflicts(conn: sqlite3.Connection) -> None:
    """全局刷新冲突状态：新冲突标记 conflict，已解除的恢复 ok。"""
    ms_set = conflict_ms_set(conn)
    conflicted_ids: set[str] = set()
    if ms_set:
        ph = ",".join("?" for _ in ms_set)
        rows = conn.execute(
            f"SELECT obs_id, utc_ms, boot_segment FROM observations "
            f"WHERE utc_ms IN ({ph})",
            tuple(sorted(ms_set)),
        ).fetchall()
        by_ms: dict[int, list[sqlite3.Row]] = {}
        for r in rows:
            by_ms.setdefault(r["utc_ms"], []).append(r)
        for ms, group in by_ms.items():
            segments = {g["boot_segment"] for g in group}
            for g in group:
                others = "、".join(sorted(segments - {g["boot_segment"]}))
                reason = f"UTC 毫秒 {ms} 与开机段 {others} 的观测冲突，不供分析"
                conn.execute(
                    "UPDATE observations SET status = ?, reason = ? WHERE obs_id = ?",
                    (CONFLICT, reason, g["obs_id"]),
                )
                conflicted_ids.add(g["obs_id"])
    # 解除冲突后恢复
    if conflicted_ids:
        ph = ",".join("?" for _ in conflicted_ids)
        conn.execute(
            f"UPDATE observations SET status = ?, reason = NULL "
            f"WHERE status = ? AND obs_id NOT IN ({ph})",
            (OK, CONFLICT, *sorted(conflicted_ids)),
        )
    else:
        conn.execute(
            "UPDATE observations SET status = ?, reason = NULL WHERE status = ?",
            (OK, CONFLICT),
        )


# ---------------------------------------------------------------- 变更快照

def _snapshot(conn: sqlite3.Connection, where: str, params: Iterable) -> dict[str, tuple]:
    """obs_id -> (utc_ms, status)，用于事务内前后对比。"""
    rows = conn.execute(
        f"SELECT obs_id, utc_ms, status FROM observations WHERE {where}",
        tuple(params),
    ).fetchall()
    return {r["obs_id"]: (r["utc_ms"], r["status"]) for r in rows}


def _changed_ids(before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
    """时刻或状态发生变化（含新出现）的观测 ID。"""
    return sorted(oid for oid, state in after.items() if before.get(oid) != state)


def fetch_states(conn: sqlite3.Connection, obs_ids: list[str]) -> list[dict]:
    """读取给定观测的当前 (obs_id, utc_ms, status)，供提交响应使用。"""
    if not obs_ids:
        return []
    ph = ",".join("?" for _ in obs_ids)
    rows = conn.execute(
        f"SELECT obs_id, utc_ms, status FROM observations "
        f"WHERE obs_id IN ({ph}) ORDER BY obs_id",
        obs_ids,
    ).fetchall()
    return [{"obs_id": r["obs_id"], "utc_ms": r["utc_ms"], "status": r["status"]} for r in rows]


# ---------------------------------------------------------------- 批量写入

def upsert_observations(conn: sqlite3.Connection, items: list[dict]) -> list[str]:
    """分批写入观测（同 ID 覆盖原文并重算），返回时刻/状态变化的观测 ID。

    新观测可能与其他开机段落在同一 UTC 毫秒而触发冲突，
    因此写入后做全局冲突刷新，并把受波及的观测一并纳入变更清单。
    """
    if not items:
        return []
    segments = {it["boot_segment"] for it in items}
    cals = load_calibrations(conn, segments)
    ids = [it["obs_id"] for it in items]
    id_ph = ",".join("?" for _ in ids)

    # 被覆盖观测的旧 UTC 毫秒（其上的冲突可能因此解除）
    old_ms = {
        r["utc_ms"]
        for r in conn.execute(
            f"SELECT utc_ms FROM observations "
            f"WHERE obs_id IN ({id_ph}) AND utc_ms IS NOT NULL",
            ids,
        )
    }

    # 同批同 ID 后者覆盖前者
    computed: dict[str, tuple[dict, tuple | None]] = {}
    for it in items:
        cal = cals[it["boot_segment"]]
        computed[it["obs_id"]] = (it, compute_utc(cal, it["counter"]))

    new_ms = {res[0] for _, res in computed.values() if res is not None}
    affected_ms = old_ms | new_ms
    if affected_ms:
        ms_ph = ",".join("?" for _ in affected_ms)
        where = f"obs_id IN ({id_ph}) OR utc_ms IN ({ms_ph})"
        params: tuple = (*ids, *sorted(affected_ms))
    else:
        where = f"obs_id IN ({id_ph})"
        params = tuple(ids)
    before = _snapshot(conn, where, params)

    for obs_id, (it, result) in computed.items():
        cal = cals[it["boot_segment"]]
        if result is None:
            reason = _pending_reason(it["boot_segment"], it["counter"], cal)
            conn.execute(
                "INSERT INTO observations (obs_id, boot_segment, counter, tide_level, "
                "utc_ms, status, reason) VALUES (?,?,?,?,NULL,?,?) "
                "ON CONFLICT(obs_id) DO UPDATE SET "
                "boot_segment=excluded.boot_segment, counter=excluded.counter, "
                "tide_level=excluded.tide_level, utc_ms=NULL, "
                "status=excluded.status, reason=excluded.reason, "
                "cal_lo_counter=NULL, cal_lo_utc_ms=NULL, "
                "cal_hi_counter=NULL, cal_hi_utc_ms=NULL",
                (it["obs_id"], it["boot_segment"], it["counter"], it["tide_level"],
                 PENDING, reason),
            )
        else:
            utc_ms, (c_lo, t_lo), (c_hi, t_hi) = result
            conn.execute(
                "INSERT INTO observations (obs_id, boot_segment, counter, tide_level, "
                "utc_ms, status, reason, cal_lo_counter, cal_lo_utc_ms, "
                "cal_hi_counter, cal_hi_utc_ms) VALUES (?,?,?,?,?,?,NULL,?,?,?,?) "
                "ON CONFLICT(obs_id) DO UPDATE SET "
                "boot_segment=excluded.boot_segment, counter=excluded.counter, "
                "tide_level=excluded.tide_level, utc_ms=excluded.utc_ms, "
                "status=excluded.status, reason=NULL, "
                "cal_lo_counter=excluded.cal_lo_counter, "
                "cal_lo_utc_ms=excluded.cal_lo_utc_ms, "
                "cal_hi_counter=excluded.cal_hi_counter, "
                "cal_hi_utc_ms=excluded.cal_hi_utc_ms",
                (it["obs_id"], it["boot_segment"], it["counter"], it["tide_level"],
                 utc_ms, OK, c_lo, t_lo, c_hi, t_hi),
            )

    resolve_conflicts(conn)
    after = _snapshot(conn, where, params)
    return _changed_ids(before, after)


class MonotonicViolation(Exception):
    """校时点使段内 UTC 倒退；violations 携带明细。"""

    def __init__(self, violations: list[dict]):
        super().__init__("calibration points make UTC go backwards within a segment")
        self.violations = violations


def _order_violations(segment: str, cal: SegmentCalibration) -> list[dict]:
    """检查合并视图的 UTC 单调性，且不展开整串。

    串内 utc_step >= 0 已保证单调；只需检查每个单点与其合并后紧邻点的边界。
    相邻两个单点互相检查会得到同一条倒退记录，故去重。
    """
    found: dict[tuple[int, int, int, int], dict] = {}
    for counter, utc_ms in cal.explicit:
        below, above = _strict_neighbors(cal, counter)
        if below is not None and utc_ms < below[1]:
            key = (counter, utc_ms, below[0], below[1])
            found[key] = {
                "boot_segment": segment,
                "counter": counter, "utc_ms": utc_ms,
                "prev_counter": below[0], "prev_utc_ms": below[1],
            }
        if above is not None and utc_ms > above[1]:
            key = (above[0], above[1], counter, utc_ms)
            found[key] = {
                "boot_segment": segment,
                "counter": above[0], "utc_ms": above[1],
                "prev_counter": counter, "prev_utc_ms": utc_ms,
            }
    return [found[k] for k in sorted(found)]


def _apply_segment_changes(
    conn: sqlite3.Connection, segments: set[str]
) -> list[str]:
    """在已写入校时依据后：校验单调性、重算受影响观测、刷新冲突、返回变更。"""
    if not segments:
        return []
    cals = load_calibrations(conn, segments)

    violations: list[dict] = []
    for seg in sorted(segments):
        violations.extend(_order_violations(seg, cals[seg]))
    if violations:
        raise MonotonicViolation(violations)

    seg_ph = ",".join("?" for _ in segments)
    seg_params = tuple(sorted(segments))

    ms_before = conflict_ms_set(conn)
    if ms_before:
        ms_ph = ",".join("?" for _ in ms_before)
        where = f"boot_segment IN ({seg_ph}) OR utc_ms IN ({ms_ph})"
        params = (*seg_params, *sorted(ms_before))
    else:
        where = f"boot_segment IN ({seg_ph})"
        params = seg_params
    before = _snapshot(conn, where, params)

    for seg in sorted(segments):
        recalc_segment(conn, seg, cals[seg])
    resolve_conflicts(conn)

    # 冲突集合可能变化，前后两批毫秒上的观测都可能翻转状态
    ms_all = ms_before | conflict_ms_set(conn)
    if ms_all:
        ms_ph = ",".join("?" for _ in ms_all)
        where = f"boot_segment IN ({seg_ph}) OR utc_ms IN ({ms_ph})"
        params = (*seg_params, *sorted(ms_all))
    else:
        where = f"boot_segment IN ({seg_ph})"
        params = seg_params
    after = _snapshot(conn, where, params)
    return _changed_ids(before, after)


def apply_calibration_submission(
    conn: sqlite3.Connection,
    calibration_items: list[dict] | None = None,
    series_items: list[dict] | None = None,
) -> list[str]:
    """一次提交内写入单点与（或）整串，随后统一校验与重算。

    单点来自 /calibrations，整串来自等步长接口。两者可同批提交；
    最终以合并后的有效校时点序列为准。任一开机段 UTC 随计数倒退时
    抛 MonotonicViolation，由调用方回滚整批。
    """
    calibration_items = calibration_items or []
    series_items = series_items or []
    segments: set[str] = set()

    for it in series_items:
        series = Series(
            it["start_counter"], it["start_utc_ms"], it["counter_step"],
            it["utc_step"], it["count"],
        )
        # 重报只替换该段上一串：主键冲突即整行覆盖
        conn.execute(
            "INSERT INTO calibration_series "
            "(boot_segment, start_counter, start_utc_ms, counter_step, utc_step, count) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(boot_segment) DO UPDATE SET "
            "start_counter=excluded.start_counter, "
            "start_utc_ms=excluded.start_utc_ms, "
            "counter_step=excluded.counter_step, "
            "utc_step=excluded.utc_step, count=excluded.count",
            (it["boot_segment"], series.start_counter, series.start_utc_ms,
             series.counter_step, series.utc_step, series.count),
        )
        segments.add(it["boot_segment"])

    for it in calibration_items:
        conn.execute(
            "INSERT INTO calibration_points (boot_segment, counter, utc_ms) "
            "VALUES (?,?,?) "
            "ON CONFLICT(boot_segment, counter) DO UPDATE SET utc_ms=excluded.utc_ms",
            (it["boot_segment"], it["counter"], it["utc_ms"]),
        )
        segments.add(it["boot_segment"])

    return _apply_segment_changes(conn, segments)


def upsert_calibrations(conn: sqlite3.Connection, items: list[dict]) -> list[str]:
    """分批写入单点校时点（同段同计数替换旧值），重算受影响观测。

    单点与已有等步长串合并后若使段内 UTC 倒退，抛 MonotonicViolation，
    由调用方回滚整批，旧数据照常供数。
    """
    if not items:
        return []
    return apply_calibration_submission(conn, calibration_items=items)


def upsert_series(conn: sqlite3.Connection, items: list[dict]) -> list[str]:
    """整串写入/替换等步长校时记录，重算受影响观测。

    每段重报只替换上一串；旧库与后补单点仍在同计数上优先。
    合并后若使段内 UTC 倒退，抛 MonotonicViolation，由调用方回滚整批。
    """
    if not items:
        return []
    return apply_calibration_submission(conn, series_items=items)
