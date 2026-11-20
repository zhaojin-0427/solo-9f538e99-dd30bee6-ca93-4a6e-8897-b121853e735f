"""校时换算、批量写入、重算与冲突检测的核心逻辑。

状态机（observations.status）：
  ok                   已校时且无冲突，供分析
  pending_calibration  计数落在本段校时区间外，保留原文、待校时
  conflict             不同开机段落在同一 UTC 毫秒，冲突观测均不供分析

所有函数都假定运行在 db.transaction 包裹的同一事务里，
因此“原文 + 校时依据 + 分析供数”同次生效。
"""

from __future__ import annotations

import bisect
import sqlite3
from typing import Iterable

OK = "ok"
PENDING = "pending_calibration"
CONFLICT = "conflict"


# ---------------------------------------------------------------- 校时换算

def load_points(conn: sqlite3.Connection, segment: str) -> list[tuple[int, int]]:
    """某开机段的全部校时点，按计数升序：[(counter, utc_ms), ...]"""
    rows = conn.execute(
        "SELECT counter, utc_ms FROM calibration_points "
        "WHERE boot_segment = ? ORDER BY counter",
        (segment,),
    ).fetchall()
    return [(r["counter"], r["utc_ms"]) for r in rows]


def compute_utc(
    points: list[tuple[int, int]], counter: int
) -> tuple[int, tuple[int, int], tuple[int, int]] | None:
    """相邻校时点间线性换算并向下取整。

    返回 (utc_ms, 下界点, 上界点)；计数落在校时区间外返回 None（待校时）。
    恰好命中某个校时点时直接取该点 UTC（上下界相同）。
    """
    if not points:
        return None
    counters = [p[0] for p in points]
    i = bisect.bisect_left(counters, counter)
    if i < len(points) and counters[i] == counter:
        p = points[i]
        return p[1], p, p
    if i == 0 or i == len(points):
        return None
    (c1, t1), (c2, t2) = points[i - 1], points[i]
    # 段内 UTC 单调不倒退已由写入方校验，故各项均非负，// 即向下取整
    utc_ms = t1 + (counter - c1) * (t2 - t1) // (c2 - c1)
    return utc_ms, (c1, t1), (c2, t2)


def _pending_reason(segment: str, counter: int, points: list[tuple[int, int]]) -> str:
    if not points:
        return f"开机段 '{segment}' 尚无校时点，待校时"
    return (
        f"计数 {counter} 超出开机段 '{segment}' 的校时区间 "
        f"[{points[0][0]}, {points[-1][0]}]，待校时"
    )


# ---------------------------------------------------------------- 重算与冲突

def recalc_segment(conn: sqlite3.Connection, segment: str) -> None:
    """按当前校时点重算某开机段全部观测的 utc_ms / 状态 / 校时依据。"""
    points = load_points(conn, segment)
    rows = conn.execute(
        "SELECT obs_id, counter FROM observations WHERE boot_segment = ?",
        (segment,),
    ).fetchall()
    for row in rows:
        result = compute_utc(points, row["counter"])
        if result is None:
            conn.execute(
                "UPDATE observations SET utc_ms = NULL, status = ?, reason = ?, "
                "cal_lo_counter = NULL, cal_lo_utc_ms = NULL, "
                "cal_hi_counter = NULL, cal_hi_utc_ms = NULL "
                "WHERE obs_id = ?",
                (PENDING, _pending_reason(segment, row["counter"], points), row["obs_id"]),
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
    points_by_seg = {s: load_points(conn, s) for s in segments}
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
        computed[it["obs_id"]] = (it, compute_utc(points_by_seg[it["boot_segment"]], it["counter"]))

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
        if result is None:
            reason = _pending_reason(it["boot_segment"], it["counter"],
                                     points_by_seg[it["boot_segment"]])
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


def upsert_calibrations(conn: sqlite3.Connection, items: list[dict]) -> list[str]:
    """分批写入校时点（同段同计数替换旧值），重算受影响观测。

    合并新旧校时点后，任一开机段内 UTC 随计数倒退则抛 MonotonicViolation，
    由调用方回滚整批，旧数据照常供数。
    """
    if not items:
        return []
    segments = {it["boot_segment"] for it in items}
    for it in items:
        conn.execute(
            "INSERT INTO calibration_points (boot_segment, counter, utc_ms) "
            "VALUES (?,?,?) "
            "ON CONFLICT(boot_segment, counter) DO UPDATE SET utc_ms=excluded.utc_ms",
            (it["boot_segment"], it["counter"], it["utc_ms"]),
        )

    violations: list[dict] = []
    for seg in sorted(segments):
        points = load_points(conn, seg)
        for (c1, t1), (c2, t2) in zip(points, points[1:]):
            if t2 < t1:
                violations.append({
                    "boot_segment": seg,
                    "counter": c2, "utc_ms": t2,
                    "prev_counter": c1, "prev_utc_ms": t1,
                })
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

    for seg in segments:
        recalc_segment(conn, seg)
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
