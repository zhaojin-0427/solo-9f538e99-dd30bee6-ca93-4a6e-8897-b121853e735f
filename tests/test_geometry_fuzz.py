"""随机化 property 测试：紧凑串算法 vs 暴力展开参考实现。

把串逐点展开、被单点覆盖的剔除后得到“有效校时点”参考序列，
在大量随机配置与随机计数上比对 compute_utc / 单调性 / 区间，
确保 O(1) 几何算法与逐点语义完全一致。
"""

import random

import pytest

from app import core


def brute_effective(points, s):
    """参考实现：逐点展开串并应用单点优先，返回按计数升序的有效点列表。"""
    lattice = {}
    if s is not None:
        for k in range(s["point_count"]):
            c, t = core._string_value(s, k)
            lattice[c] = t
    for c, t in points:
        lattice[c] = t  # 单点优先覆盖
    return sorted(lattice.items())


def brute_compute(effective, counter):
    import bisect
    counters = [c for c, _ in effective]
    i = bisect.bisect_left(counters, counter)
    if i < len(effective) and counters[i] == counter:
        c, t = effective[i]
        return t, (c, t), (c, t)
    if i == 0 or i == len(effective):
        return None
    (c1, t1), (c2, t2) = effective[i - 1], effective[i]
    utc = t1 + (counter - c1) * (t2 - t1) // (c2 - c1)
    return utc, (c1, t1), (c2, t2)


def brute_monotonic_violations(effective):
    out = []
    for (c1, t1), (c2, t2) in zip(effective, effective[1:]):
        if t2 < t1:
            out.append((c2, c1))
    return out


def random_config(rng):
    s = None
    if rng.random() < 0.85:
        n = rng.randint(1, 60)
        step = rng.randint(1, 7)
        first = rng.randint(0, 30)
        utc_step = rng.choice([0, 0, 1, 2, 5])
        s = {
            "first_counter": first,
            "counter_step": step,
            "first_utc_ms": rng.randint(0, 1000),
            "utc_step_ms": utc_step,
            "point_count": n,
        }
    m = rng.randint(0, 12)
    raw: dict[int, int] = {}
    # 一半场景让 UTC 沿串时间线单调（覆盖大量合法配置），一半完全随机
    monotone = rng.random() < 0.5
    run_max = -1
    for _ in range(m):
        c = rng.randint(0, 120)
        if monotone and s is not None:
            # 取串在该计数处的线性时间线，再加非负抖动，并用运行最大值强制非递减
            k = max(0, (c - s["first_counter"]) // s["counter_step"])
            k = min(k, s["point_count"] - 1)
            candidate = s["first_utc_ms"] + k * s["utc_step_ms"] + rng.randint(0, 2)
            run_max = max(run_max, candidate)
            raw[c] = run_max
        elif monotone:
            run_max = max(run_max, 0) + rng.randint(0, 50)
            raw[c] = run_max
        else:
            raw[c] = rng.randint(0, 3000)
    return sorted(raw.items()), s


@pytest.mark.parametrize("seed", range(400))
def test_compute_utc_matches_brute_force(seed):
    rng = random.Random(seed)
    points, s = random_config(rng)
    effective = brute_effective(points, s)
    lo = effective[0][0] if effective else 0
    hi = effective[-1][0] if effective else 0

    probe = [rng.randint(lo - 20, hi + 20) for _ in range(40)]
    probe += [c for c, _ in effective]  # 所有有效点必须精确命中
    for counter in probe:
        got = core.compute_utc(points, s, counter)
        want = brute_compute(effective, counter)
        assert got == want, (seed, counter, got, want)


@pytest.mark.parametrize("seed", range(400))
def test_monotonic_check_matches_brute_force(seed):
    rng = random.Random(10_000 + seed)
    points, s = random_config(rng)
    effective = brute_effective(points, s)
    got = core.check_segment_monotonic(points, s)
    want_pairs = set(brute_monotonic_violations(effective))
    got_pairs = {(v["counter"], v["prev_counter"]) for v in got}
    assert got_pairs == want_pairs, (seed, got_pairs, want_pairs)


def test_monotonic_accepts_all_non_decreasing_configs():
    rng = random.Random(42)
    accepted = 0
    for seed in range(400):
        points, s = random_config(rng)
        effective = brute_effective(points, s)
        if not brute_monotonic_violations(effective):
            assert core.check_segment_monotonic(points, s) == []
            accepted += 1
    assert accepted > 40  # 确保确实覆盖了一批合法配置（随机分支天然多为倒退）


def test_effective_range_matches_brute_force():
    rng = random.Random(7)
    for _ in range(200):
        points, s = random_config(rng)
        effective = brute_effective(points, s)
        lo, hi = core.effective_range(points, s)
        if not effective:
            assert (lo, hi) == (None, None)
        else:
            assert (lo, hi) == (effective[0][0], effective[-1][0])
