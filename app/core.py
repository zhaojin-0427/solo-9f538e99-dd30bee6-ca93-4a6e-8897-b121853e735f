"""校时换算、批量写入、重算与冲突检测的核心逻辑。

校时点有两种紧凑来源，合并后共同构成一段的“有效校时点”：

  1. 单点 calibration_points：旧库已有与 POST /calibrations 后补的点，
     同段同计数优先于串上的点（串点被覆盖），也可补在串内两点之间。
  2. 等步长校时串 calibration_strings：每段至多一串，以
     (首点计数, 计数步长, 首点 UTC, UTC 步长, 点数) 一行表示算术序列，
     重报整串替换；任何处理都只做 O(1) 几何运算，绝不逐点展开。

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
from typing import Iterable, Optional

OK = "ok"
PENDING = "pending_calibration"
CONFLICT = "conflict"

# 单串点上限
MAX_STRING_POINTS = 1_000_000


# ---------------------------------------------------------------- 校时点装载

def load_points(conn: sqlite3.Connection, segment: str) -> list[tuple[int, int]]:
    """某开机段的全部**单点**校时点，按计数升序：[(counter, utc_ms), ...]"""
    rows = conn.execute(
        "SELECT counter, utc_ms FROM calibration_points "
        "WHERE boot_segment = ? ORDER BY counter",
        (segment,),
    ).fetchall()
    return [(r["counter"], r["utc_ms"]) for r in rows]


def load_string(conn: sqlite3.Connection, segment: str) -> Optional[dict]:
    """某开机段的等步长校时串（每段至多一串）；无串返回 None。"""
    row = conn.execute(
        "SELECT first_counter, counter_step, first_utc_ms, utc_step_ms, point_count "
        "FROM calibration_strings WHERE boot_segment = ?",
        (segment,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def string_last(s: dict) -> tuple[int, int]:
    """串末点的 (counter, utc_ms)。"""
    k = s["point_count"] - 1
    return (
        s["first_counter"] + k * s["counter_step"],
        s["first_utc_ms"] + k * s["utc_step_ms"],
    )


def string_span(s: Optional[dict]) -> tuple[Optional[int], Optional[int]]:
    """串覆盖的计数区间 [min, max]；无串返回 (None, None)。"""
    if s is None:
        return None, None
    return s["first_counter"], string_last(s)[0]


# ---------------------------------------------------------------- 串的 O(1) 几何

def _lattice_index(s: dict, counter: int) -> Optional[int]:
    """counter 是否落在串的格点上；是则返回序号 k（0 <= k < n），否则 None。"""
    delta = counter - s["first_counter"]
    if delta < 0 or delta % s["counter_step"] != 0:
        return None
    k = delta // s["counter_step"]
    if k >= s["point_count"]:
        return None
    return k


def _string_value(s: dict, k: int) -> tuple[int, int]:
    """串上第 k 个点的 (counter, utc_ms)。"""
    return (
        s["first_counter"] + k * s["counter_step"],
        s["first_utc_ms"] + k * s["utc_step_ms"],
    )


def _prev_free_index(aligned: list[int], j: int) -> Optional[int]:
    """<= j 的最大“未被单点覆盖”格点序号；aligned 为被覆盖序号的升序表。

    全程二分：先判断 j 是否被覆盖，再定位 j 所在连续 run 的左边界，
    run 左边界的前一个序号即所求自由点。复杂度 O(log m)，与串长、run 长无关。
    """
    pos = bisect.bisect_right(aligned, j) - 1
    if pos < 0 or aligned[pos] != j:
        return j  # j 本身未被覆盖
    # 二分找连续 run 的左边界：最小的 lo，使 aligned[lo..pos] 为连续整数
    lo, hi = 0, pos
    while lo < hi:
        mid = (lo + hi) // 2
        if aligned[pos] - aligned[mid] == pos - mid:
            hi = mid  # mid..pos 连续，左边界还能往左
        else:
            lo = mid + 1
    idx = aligned[lo] - 1
    return idx if idx >= 0 else None


def _next_free_index(aligned: list[int], j: int, n: int) -> Optional[int]:
    """>= j 的最小“未被单点覆盖”格点序号（j < n）；越界或全被覆盖返回 None。

    同样用二分定位 j 所在连续 run 的右边界。复杂度 O(log m)。
    """
    pos = bisect.bisect_left(aligned, j)
    if pos == len(aligned) or aligned[pos] != j:
        return j  # j 本身未被覆盖
    # 二分找连续 run 的右边界：最大的 hi，使 aligned[pos..hi] 为连续整数
    lo, hi = pos, len(aligned) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if aligned[mid] - aligned[pos] == mid - pos:
            lo = mid  # pos..mid 连续，右边界还能往右
        else:
            hi = mid - 1
    idx = aligned[lo] + 1
    return idx if idx < n else None


# ---------------------------------------------------------------- 校时换算

def compute_utc(
    points: list[tuple[int, int]],
    s: Optional[dict],
    counter: int,
    aligned: Optional[list[int]] = None,
) -> tuple[int, tuple[int, int], tuple[int, int]] | None:
    """在“单点 + 等步长串”合并后的有效校时点上线性换算并向下取整。

    返回 (utc_ms, 下界点, 上界点)；计数落在校时区间外返回 None（待校时）。
    恰好命中有效校时点时直接取该点 UTC（上下界相同）。
    单点与串同计数时单点优先（串点被覆盖，不参与换算）。

    aligned 为串上被单点覆盖的格点序号升序表；批量重算时可预构建一次传入，
    避免每个观测重复 O(m) 构建（m 为单点数，与串长无关）。
    """
    counters = [p[0] for p in points]

    # 1) 恰好命中单点：单点优先
    i = bisect.bisect_left(counters, counter)
    if i < len(points) and counters[i] == counter:
        p = points[i]
        return p[1], p, p

    # 2) 无串：只在相邻单点之间换算
    if s is None:
        if not points:
            return None
        if i == 0 or i == len(points):
            return None
        (c1, t1), (c2, t2) = points[i - 1], points[i]
        utc_ms = t1 + (counter - c1) * (t2 - t1) // (c2 - c1)
        return utc_ms, (c1, t1), (c2, t2)

    n = s["point_count"]
    if aligned is None:
        aligned = [
            k for c, _t in points
            if (k := _lattice_index(s, c)) is not None
        ]

    # 3) 恰好命中串上未被覆盖的格点
    k_hit = _lattice_index(s, counter)
    if k_hit is not None:
        hit_pos = bisect.bisect_left(aligned, k_hit)
        if hit_pos == len(aligned) or aligned[hit_pos] != k_hit:
            pt = _string_value(s, k_hit)
            return pt[1], pt, pt

    # 4) 收集 floor / ceil 候选（单点 + 串格点，被覆盖的串格点剔除）
    floor_cands: list[tuple[int, int]] = []
    ceil_cands: list[tuple[int, int]] = []
    if i > 0:
        floor_cands.append(points[i - 1])
    if i < len(points):
        ceil_cands.append(points[i])

    delta = counter - s["first_counter"]
    if delta > 0:
        # floor 格点序号：counter 落在第 kf 与 kf+1 个格点之间时 kf = delta // step
        kf = min(delta // s["counter_step"], n - 1)
        kf = _prev_free_index(aligned, kf)
        if kf is not None:
            floor_cands.append(_string_value(s, kf))
    # ceil：counter 在串首之前时串首格点就是 ceil 候选
    kc0 = max(delta // s["counter_step"] + 1, 0) if delta >= 0 else 0
    if kc0 < n:
        kc = _next_free_index(aligned, kc0, n)
        if kc is not None:
            ceil_cands.append(_string_value(s, kc))

    if not floor_cands or not ceil_cands:
        return None
    (c1, t1) = max(floor_cands, key=lambda p: p[0])
    (c2, t2) = min(ceil_cands, key=lambda p: p[0])
    if c1 >= c2:  # 理论上不会发生：候选已按 floor/ceil 严格分居
        return None
    # 合并后的有效点 UTC 随计数非倒退（写入方已校验），各项非负，// 即向下取整
    utc_ms = t1 + (counter - c1) * (t2 - t1) // (c2 - c1)
    return utc_ms, (c1, t1), (c2, t2)


def effective_range(
    points: list[tuple[int, int]], s: Optional[dict]
) -> tuple[Optional[int], Optional[int]]:
    """合并后有效校时点覆盖的计数区间 [min, max]；无任何点返回 (None, None)。"""
    lo = points[0][0] if points else None
    hi = points[-1][0] if points else None
    slo, shi = string_span(s)
    if slo is not None:
        lo = slo if lo is None else min(lo, slo)
        hi = shi if hi is None else max(hi, shi)
    return lo, hi


def _pending_reason(
    segment: str, counter: int,
    points: list[tuple[int, int]], s: Optional[dict],
) -> str:
    lo, hi = effective_range(points, s)
    if lo is None:
        return f"开机段 '{segment}' 尚无校时点，待校时"
    return (
        f"计数 {counter} 超出开机段 '{segment}' 的校时区间 "
        f"[{lo}, {hi}]，待校时"
    )


# ---------------------------------------------------------------- 合并单调性校验（不展开串）

def check_segment_monotonic(
    points: list[tuple[int, int]], s: Optional[dict]
) -> list[dict]:
    """校验合并后的有效校时点 UTC 随计数非倒退，返回违规明细（空列表即通过）。

    串自身的 UTC 步长非负，串内相邻格点不可能倒退，因此只需检查每个单点
    与其在有效序列中紧邻的前/后有效点（跳过被单点覆盖的串格点）。
    复杂度只与单点数有关，与串长无关。
    """
    violations: list[dict] = []
    seg_points = points  # 已按计数升序

    if s is None:
        for (c1, t1), (c2, t2) in zip(seg_points, seg_points[1:]):
            if t2 < t1:
                violations.append({
                    "counter": c2, "utc_ms": t2,
                    "prev_counter": c1, "prev_utc_ms": t1,
                })
        return violations

    n = s["point_count"]
    aligned = [
        k for c, _t in seg_points
        if (k := _lattice_index(s, c)) is not None
    ]

    for idx, (c, t) in enumerate(seg_points):
        # 紧邻的前一个有效点：前一个单点，或串上 c 之前最近的未覆盖格点（取计数大者）
        prev_cands: list[tuple[int, int]] = []
        if idx > 0:
            prev_cands.append(seg_points[idx - 1])
        delta = c - s["first_counter"]
        if delta > 0:
            kf = min(delta // s["counter_step"], n - 1)
            kf = _prev_free_index(aligned, kf)
            if kf is not None:
                pc, pt = _string_value(s, kf)
                if pc < c:  # 单点可能不在格点上，须严格位于 c 之前
                    prev_cands.append((pc, pt))
        if prev_cands:
            pc, pt = max(prev_cands, key=lambda p: p[0])
            if t < pt:
                violations.append({
                    "counter": c, "utc_ms": t,
                    "prev_counter": pc, "prev_utc_ms": pt,
                })

        # 紧邻的后一个有效点：后一个单点，或串上 c 之后最近的未覆盖格点（取计数小者）
        next_cands: list[tuple[int, int]] = []
        if idx + 1 < len(seg_points):
            next_cands.append(seg_points[idx + 1])
        kc0 = max(delta // s["counter_step"] + 1, 0) if delta >= 0 else 0
        if kc0 < n:
            kc = _next_free_index(aligned, kc0, n)
            if kc is not None:
                nc, nt = _string_value(s, kc)
                if nc > c:
                    next_cands.append((nc, nt))
        if next_cands:
            nc, nt = min(next_cands, key=lambda p: p[0])
            if nt < t:
                violations.append({
                    "counter": nc, "utc_ms": nt,
                    "prev_counter": c, "prev_utc_ms": t,
                })

    violations.sort(key=lambda v: (v["counter"], v["prev_counter"]))
    return violations


# ---------------------------------------------------------------- 重算与冲突

def recalc_segment(conn: sqlite3.Connection, segment: str) -> None:
    """按当前有效校时点重算某开机段全部观测的 utc_ms / 状态 / 校时依据。

    只遍历该段观测行，不遍历校时串的点；覆盖格点表只构建一次，
    行更新用 executemany 批量提交。
    """
    points = load_points(conn, segment)
    s = load_string(conn, segment)
    aligned = None
    if s is not None:
        aligned = [
            k for c, _t in points
            if (k := _lattice_index(s, c)) is not None
        ]
    rows = conn.execute(
        "SELECT obs_id, counter FROM observations WHERE boot_segment = ?",
        (segment,),
    ).fetchall()
    ok_params: list[tuple] = []
    pending_params: list[tuple] = []
    for row in rows:
        result = compute_utc(points, s, row["counter"], aligned)
        if result is None:
            pending_params.append((
                PENDING,
                _pending_reason(segment, row["counter"], points, s),
                row["obs_id"],
            ))
        else:
            utc_ms, (c_lo, t_lo), (c_hi, t_hi) = result
            ok_params.append(
                (utc_ms, OK, c_lo, t_lo, c_hi, t_hi, row["obs_id"])
            )
    conn.executemany(
        "UPDATE observations SET utc_ms = NULL, status = ?, reason = ?, "
        "cal_lo_counter = NULL, cal_lo_utc_ms = NULL, "
        "cal_hi_counter = NULL, cal_hi_utc_ms = NULL WHERE obs_id = ?",
        pending_params,
    )
    conn.executemany(
        "UPDATE observations SET utc_ms = ?, status = ?, reason = NULL, "
        "cal_lo_counter = ?, cal_lo_utc_ms = ?, "
        "cal_hi_counter = ?, cal_hi_utc_ms = ? WHERE obs_id = ?",
        ok_params,
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


def _segment_snapshot_scope(
    conn: sqlite3.Connection, segments: set[str]
) -> tuple[str, tuple]:
    """构造变更快照的 WHERE：受影响段的观测 + 当前所有冲突毫秒上的观测。

    返回 (where, params, ms_before)，ms_before 供提交后扩大快照范围，
    使冲突解除时他段观测的状态恢复也能进入变更清单。
    """
    seg_ph = ",".join("?" for _ in segments)
    seg_params = tuple(sorted(segments))
    ms_before = conflict_ms_set(conn)
    if ms_before:
        ms_ph = ",".join("?" for _ in ms_before)
        return (
            f"boot_segment IN ({seg_ph}) OR utc_ms IN ({ms_ph})",
            (*seg_params, *sorted(ms_before)),
            ms_before,
        )
    return f"boot_segment IN ({seg_ph})", seg_params, ms_before


def _finish_with_scope(
    conn: sqlite3.Connection,
    segments: set[str],
    before: dict[str, tuple],
    ms_before: set[int],
) -> list[str]:
    """重算 + 冲突刷新后，在“受影响段 ∪ 新旧冲突毫秒”范围内取前后差异。"""
    for seg in segments:
        recalc_segment(conn, seg)
    resolve_conflicts(conn)

    seg_ph = ",".join("?" for _ in segments)
    seg_params = tuple(sorted(segments))
    # 前后两批毫秒上的观测都可能翻转状态（含冲突解除后恢复的他段观测）
    ms_all = ms_before | conflict_ms_set(conn)
    if ms_all:
        ms_ph = ",".join("?" for _ in ms_all)
        where = f"boot_segment IN ({seg_ph}) OR utc_ms IN ({ms_ph})"
        params: tuple = (*seg_params, *sorted(ms_all))
    else:
        where = f"boot_segment IN ({seg_ph})"
        params = seg_params
    after = _snapshot(conn, where, params)
    return _changed_ids(before, after)


# ---------------------------------------------------------------- 观测写入

def upsert_observations(conn: sqlite3.Connection, items: list[dict]) -> list[str]:
    """分批写入观测（同 ID 覆盖原文并重算），返回时刻/状态变化的观测 ID。

    新观测可能与其他开机段落在同一 UTC 毫秒而触发冲突，
    因此写入后做全局冲突刷新，并把受波及的观测一并纳入变更清单。
    """
    if not items:
        return []
    segments = {it["boot_segment"] for it in items}
    points_by_seg = {s: load_points(conn, s) for s in segments}
    string_by_seg = {s: load_string(conn, s) for s in segments}
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
        seg = it["boot_segment"]
        computed[it["obs_id"]] = (
            it,
            compute_utc(points_by_seg[seg], string_by_seg[seg], it["counter"]),
        )

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
        seg = it["boot_segment"]
        if result is None:
            reason = _pending_reason(
                seg, it["counter"], points_by_seg[seg], string_by_seg[seg]
            )
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


# ---------------------------------------------------------------- 单点写入

def upsert_calibrations(conn: sqlite3.Connection, items: list[dict]) -> list[str]:
    """分批写入单点校时点（同段同计数替换旧值），重算受影响观测。

    单点与已有校时串合并后，任一开机段内 UTC 随计数倒退则抛
    MonotonicViolation，由调用方回滚整批，旧数据（含原串）照常供数。
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
        s = load_string(conn, seg)
        for v in check_segment_monotonic(points, s):
            v = {"boot_segment": seg, **v}
            violations.append(v)
    if violations:
        raise MonotonicViolation(violations)

    where, params, ms_before = _segment_snapshot_scope(conn, segments)
    before = _snapshot(conn, where, params)
    return _finish_with_scope(conn, segments, before, ms_before)


# ---------------------------------------------------------------- 整串写入

def upsert_calibration_string(conn: sqlite3.Connection, item: dict) -> list[str]:
    """整串提交/重报某开机段的等步长校时串，返回时刻/状态变化的观测 ID。

    - 每段至多一串：重报只替换该段上一串（单行 upsert，不逐点展开）。
    - 与已有单点合并后若 UTC 随计数倒退，抛 MonotonicViolation 整次拒绝，
      事务回滚后旧串与单点原样保留。
    """
    seg = item["boot_segment"]
    segments = {seg}
    conn.execute(
        "INSERT INTO calibration_strings (boot_segment, first_counter, counter_step, "
        "first_utc_ms, utc_step_ms, point_count) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(boot_segment) DO UPDATE SET "
        "first_counter=excluded.first_counter, counter_step=excluded.counter_step, "
        "first_utc_ms=excluded.first_utc_ms, utc_step_ms=excluded.utc_step_ms, "
        "point_count=excluded.point_count",
        (
            seg,
            item["first_counter"],
            item["counter_step"],
            item["first_utc_ms"],
            item["utc_step_ms"],
            item["point_count"],
        ),
    )

    points = load_points(conn, seg)
    s = load_string(conn, seg)
    violations = [
        {"boot_segment": seg, **v}
        for v in check_segment_monotonic(points, s)
    ]
    if violations:
        raise MonotonicViolation(violations)

    where, params, ms_before = _segment_snapshot_scope(conn, segments)
    before = _snapshot(conn, where, params)
    return _finish_with_scope(conn, segments, before, ms_before)
