"""等步长校时串：整串提交、紧凑存储、单点优先/插值、整次拒绝、重报替换、
跨段冲突、重启持久化与百万点不展开。"""

import os
import time

import pytest
from fastapi.testclient import TestClient

from app import core, db
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_tables():
    conn = db.get_conn()
    with db.lock():
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM calibration_points")
        conn.execute("DELETE FROM calibration_strings")
    yield


# ---------------------------------------------------------------- 工具

def post_string(payload, expect=200):
    r = client.post("/calibration-strings", json=payload)
    assert r.status_code == expect, r.text
    return r.json()


def post_cal(items, expect=200):
    r = client.post("/calibrations", json={"calibrations": items})
    assert r.status_code == expect, r.text
    return r.json()


def post_obs(items):
    r = client.post("/observations", json={"observations": items})
    assert r.status_code == 200, r.text
    return r.json()


def get_obs(obs_id):
    return client.get(f"/observations/{obs_id}")


def changed_ids(resp):
    return {c["obs_id"] for c in resp["changed"]}


def tide(start, end):
    r = client.get("/tide-levels", params={"start_ms": start, "end_ms": end})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 基础换算

def test_string_basic_interpolation_and_exact_lattice():
    # 串：counter 0,100,...,1000（11 点）；utc 1000,1010,...,1100
    post_string({
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 1000, "utc_step_ms": 10, "point_count": 11,
    })
    resp = post_obs([
        {"obs_id": "head", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "tail", "boot_segment": "A", "counter": 1000, "tide_level": 2.0},
        {"obs_id": "mid", "boot_segment": "A", "counter": 550, "tide_level": 3.0},
        {"obs_id": "floor", "boot_segment": "A", "counter": 101, "tide_level": 4.0},
    ])
    assert changed_ids(resp) == {"head", "tail", "mid", "floor"}
    assert get_obs("head").json()["utc_ms"] == 1000
    assert get_obs("tail").json()["utc_ms"] == 1100
    # 550 落在格点 500(1050) 与 600(1060) 之间：1050 + 50*10/100 = 1055
    mid = get_obs("mid").json()
    assert mid["utc_ms"] == 1055
    assert mid["calibration"] == {
        "lo": {"counter": 500, "utc_ms": 1050},
        "hi": {"counter": 600, "utc_ms": 1060},
    }
    # 101：1000 + 101*10/100 = 1010.1 -> 向下取整 1010
    floor = get_obs("floor").json()
    assert floor["utc_ms"] == 1010
    assert floor["calibration"] == {
        "lo": {"counter": 100, "utc_ms": 1010},
        "hi": {"counter": 200, "utc_ms": 1020},
    }


def test_string_outside_range_is_pending():
    post_string({
        "boot_segment": "A", "first_counter": 100, "counter_step": 10,
        "first_utc_ms": 5000, "utc_step_ms": 1, "point_count": 5,
    })  # counter 区间 [100, 140]
    post_obs([
        {"obs_id": "lo", "boot_segment": "A", "counter": 99, "tide_level": 1.0},
        {"obs_id": "hi", "boot_segment": "A", "counter": 141, "tide_level": 2.0},
        {"obs_id": "ok", "boot_segment": "A", "counter": 120, "tide_level": 3.0},
    ])
    for oid in ("lo", "hi"):
        body = get_obs(oid).json()
        assert body["status"] == "pending_calibration"
        assert body["utc_ms"] is None and body["calibration"] is None
        assert "[100, 140]" in body["reason"]
    assert get_obs("ok").json()["status"] == "ok"
    assert get_obs("ok").json()["utc_ms"] == 5002


def test_string_with_zero_utc_step_is_flat_timeline():
    post_string({
        "boot_segment": "A", "first_counter": 0, "counter_step": 10,
        "first_utc_ms": 7000, "utc_step_ms": 0, "point_count": 4,
    })
    post_obs([{"obs_id": "p", "boot_segment": "A", "counter": 25, "tide_level": 1.0}])
    body = get_obs("p").json()
    assert body["utc_ms"] == 7000 and body["status"] == "ok"


def test_single_point_string():
    post_string({
        "boot_segment": "A", "first_counter": 500, "counter_step": 1,
        "first_utc_ms": 9000, "utc_step_ms": 0, "point_count": 1,
    })
    post_obs([
        {"obs_id": "hit", "boot_segment": "A", "counter": 500, "tide_level": 1.0},
        {"obs_id": "miss", "boot_segment": "A", "counter": 501, "tide_level": 2.0},
    ])
    assert get_obs("hit").json()["utc_ms"] == 9000
    assert get_obs("miss").json()["status"] == "pending_calibration"


# ---------------------------------------------------------------- 输入校验

def test_string_validation_rules():
    base = {
        "boot_segment": "A", "first_counter": 0, "counter_step": 10,
        "first_utc_ms": 1000, "utc_step_ms": 1, "point_count": 3,
    }
    bad = [
        {"counter_step": 0},                       # 计数步长必须为正
        {"counter_step": -1},
        {"utc_step_ms": -1},                       # UTC 步长非负
        {"point_count": 0},                        # 点数 >= 1
        {"point_count": 1_000_001},                # 点数 <= 100 万
        {"first_counter": -1},
        {"first_utc_ms": -1},
        {"boot_segment": ""},
    ]
    for override in bad:
        payload = {**base, **override}
        r = client.post("/calibration-strings", json=payload)
        assert r.status_code == 422, (override, r.text)

    # 末值超出 SQLite 非负整数范围 -> 422
    overflow = {
        "boot_segment": "A", "first_counter": 2**63 - 1, "counter_step": 1,
        "first_utc_ms": 0, "utc_step_ms": 0, "point_count": 2,
    }
    assert client.post("/calibration-strings", json=overflow).status_code == 422
    overflow_utc = {
        "boot_segment": "A", "first_counter": 0, "counter_step": 1,
        "first_utc_ms": 2**63 - 1, "utc_step_ms": 1, "point_count": 2,
    }
    assert client.post("/calibration-strings", json=overflow_utc).status_code == 422

    # 首末值恰在上界（单点串）合法
    ok = {
        "boot_segment": "A", "first_counter": 2**63 - 1, "counter_step": 1,
        "first_utc_ms": 2**63 - 1, "utc_step_ms": 0, "point_count": 1,
    }
    assert client.post("/calibration-strings", json=ok).status_code == 200


# ---------------------------------------------------------------- 重报替换

def test_resubmit_replaces_previous_string():
    post_string({
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 1000, "utc_step_ms": 10, "point_count": 11,
    })
    post_obs([{"obs_id": "x", "boot_segment": "A", "counter": 500, "tide_level": 1.0}])
    assert get_obs("x").json()["utc_ms"] == 1050

    # 重报：不同斜率的新串替换上一串
    resp = post_string({
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 2000, "utc_step_ms": 20, "point_count": 11,
    })
    assert changed_ids(resp) == {"x"}
    assert resp["changed"][0]["utc_ms"] == 2100
    assert get_obs("x").json()["utc_ms"] == 2100

    # 库里每段仍只有一行串
    conn = db.get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM calibration_strings WHERE boot_segment = 'A'"
    ).fetchone()["n"]
    assert n == 1

    # 再报一个更短的串，旧串区间外的点回到待校时
    resp = post_string({
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 2000, "utc_step_ms": 20, "point_count": 3,
    })  # 只覆盖 [0, 200]
    assert changed_ids(resp) == {"x"}
    assert get_obs("x").json()["status"] == "pending_calibration"


def test_identical_string_resubmission_reports_no_change():
    payload = {
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 1000, "utc_step_ms": 10, "point_count": 5,
    }
    post_string(payload)
    post_obs([{"obs_id": "q", "boot_segment": "A", "counter": 250, "tide_level": 1.0}])
    assert changed_ids(post_string(payload)) == set()


# ---------------------------------------------------------------- 单点优先与串内插点

def test_single_point_overrides_string_at_same_counter():
    post_string({
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 1000, "utc_step_ms": 10, "point_count": 11,
    })
    post_obs([{"obs_id": "g", "boot_segment": "A", "counter": 500, "tide_level": 1.0}])
    assert get_obs("g").json()["utc_ms"] == 1050

    # 后补单点覆盖串上 500 这个格点（原值 1050 -> 新值 1055）
    resp = post_cal([{"boot_segment": "A", "counter": 500, "utc_ms": 1055}])
    assert changed_ids(resp) == {"g"}
    body = get_obs("g").json()
    assert body["utc_ms"] == 1055
    # 详情中的下界校时点必须与逐点存入的有效点一致（取单点值）
    assert body["calibration"]["lo"] == {"counter": 500, "utc_ms": 1055}
    assert body["calibration"]["hi"] == {"counter": 500, "utc_ms": 1055}

    # 相邻格点间的换算以被覆盖后的有效点为界
    post_obs([{"obs_id": "between", "boot_segment": "A", "counter": 550, "tide_level": 2.0}])
    b = get_obs("between").json()
    # lo=单点(500,1055), hi=串格点(600,1060)：1055 + 50*5/100 = 1057.5 -> 1057
    assert b["utc_ms"] == 1057
    assert b["calibration"] == {
        "lo": {"counter": 500, "utc_ms": 1055},
        "hi": {"counter": 600, "utc_ms": 1060},
    }


def test_single_point_between_string_lattice_points():
    post_string({
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 1000, "utc_step_ms": 10, "point_count": 11,
    })
    # 单点补在串内两格点 100 与 200 之间
    post_cal([{"boot_segment": "A", "counter": 150, "utc_ms": 1012}])
    post_obs([
        {"obs_id": "a", "boot_segment": "A", "counter": 125, "tide_level": 1.0},
        {"obs_id": "b", "boot_segment": "A", "counter": 175, "tide_level": 2.0},
    ])
    # 125：lo=(100,1010) 串点, hi=(150,1012) 单点 -> 1010 + 25*2/50 = 1011
    a = get_obs("a").json()
    assert a["utc_ms"] == 1011
    assert a["calibration"] == {
        "lo": {"counter": 100, "utc_ms": 1010},
        "hi": {"counter": 150, "utc_ms": 1012},
    }
    # 175：lo=(150,1012) 单点, hi=(200,1020) 串点 -> 1012 + 25*8/50 = 1016
    b = get_obs("b").json()
    assert b["utc_ms"] == 1016
    assert b["calibration"] == {
        "lo": {"counter": 150, "utc_ms": 1012},
        "hi": {"counter": 200, "utc_ms": 1020},
    }


def test_legacy_single_points_still_work_alongside_string():
    # 旧库已有单点（先提交），再提交串：合并换算
    post_cal([{"boot_segment": "A", "counter": 0, "utc_ms": 1000}])
    post_string({
        "boot_segment": "A", "first_counter": 100, "counter_step": 100,
        "first_utc_ms": 1020, "utc_step_ms": 10, "point_count": 3,
    })  # 100->1020, 200->1030, 300->1040
    post_obs([{"obs_id": "m", "boot_segment": "A", "counter": 50, "tide_level": 1.0}])
    # lo=单点(0,1000), hi=串点(100,1020) -> 1000 + 50*20/100 = 1010
    body = get_obs("m").json()
    assert body["utc_ms"] == 1010
    assert body["calibration"] == {
        "lo": {"counter": 0, "utc_ms": 1000},
        "hi": {"counter": 100, "utc_ms": 1020},
    }


# ---------------------------------------------------------------- 整次拒绝（UTC 倒退）

def test_string_that_goes_backwards_against_single_point_is_rejected():
    post_cal([{"boot_segment": "A", "counter": 0, "utc_ms": 5000}])
    # 串首点 100->4000，相对单点 (0,5000) 倒退
    r = client.post("/calibration-strings", json={
        "boot_segment": "A", "first_counter": 100, "counter_step": 10,
        "first_utc_ms": 4000, "utc_step_ms": 1, "point_count": 3,
    })
    assert r.status_code == 409
    body = r.json()["detail"]
    assert "整次拒绝" in body["message"]
    assert body["violations"][0]["boot_segment"] == "A"

    # 旧单点照常供数，串未落库
    post_obs([{"obs_id": "z", "boot_segment": "A", "counter": 0, "tide_level": 1.0}])
    assert get_obs("z").json()["utc_ms"] == 5000
    conn = db.get_conn()
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM calibration_strings"
    ).fetchone()["n"] == 0


def test_string_tail_backwards_against_single_point_is_rejected():
    # 串自身步长非负不会倒退；但串末点相对中间的单点倒退
    post_cal([{"boot_segment": "A", "counter": 150, "utc_ms": 9999}])
    r = client.post("/calibration-strings", json={
        "boot_segment": "A", "first_counter": 100, "counter_step": 10,
        "first_utc_ms": 1000, "utc_step_ms": 1, "point_count": 11,
    })  # 末点 counter=200 -> utc=1010 < 单点 150->9999
    assert r.status_code == 409
    v = r.json()["detail"]["violations"]
    assert any(x["prev_counter"] == 150 and x["counter"] == 160 for x in v)


def test_backwards_single_point_rejected_keeps_string_intact():
    post_string({
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 1000, "utc_step_ms": 10, "point_count": 5,
    })
    post_obs([{"obs_id": "g", "boot_segment": "A", "counter": 200, "tide_level": 1.0}])
    assert get_obs("g").json()["utc_ms"] == 1020

    # 后补倒退单点 -> 整批拒绝，串与观测不变
    r = client.post("/calibrations", json={"calibrations": [
        {"boot_segment": "A", "counter": 150, "utc_ms": 500},
    ]})
    assert r.status_code == 409
    assert get_obs("g").json()["utc_ms"] == 1020
    assert tide(0, 100000)["count"] == 1


def test_rejected_string_rolls_back_even_when_replacing():
    payload = {
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 1000, "utc_step_ms": 10, "point_count": 5,
    }
    post_string(payload)
    # 串内两格点之间的合法单点（1005 介于串点 1000 与 1010 之间，不倒退）
    post_cal([{"boot_segment": "A", "counter": 50, "utc_ms": 1005}])
    post_obs([{"obs_id": "g", "boot_segment": "A", "counter": 200, "tide_level": 1.0}])
    old_utc = get_obs("g").json()["utc_ms"]

    # 重报串与既有单点冲突 -> 拒绝，旧串仍是最后一次成功提交
    r = client.post("/calibration-strings", json={
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 3000, "utc_step_ms": 10, "point_count": 5,
    })
    assert r.status_code == 409
    assert get_obs("g").json()["utc_ms"] == old_utc
    conn = db.get_conn()
    row = conn.execute(
        "SELECT first_utc_ms FROM calibration_strings WHERE boot_segment = 'A'"
    ).fetchone()
    assert row["first_utc_ms"] == 1000


# ---------------------------------------------------------------- 跨段冲突

def test_string_recalc_can_trigger_and_resolve_cross_segment_conflict():
    post_cal([{"boot_segment": "B", "counter": 0, "utc_ms": 1050}])
    post_obs([{"obs_id": "b1", "boot_segment": "B", "counter": 0, "tide_level": 9.0}])

    post_string({
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 1000, "utc_step_ms": 10, "point_count": 11,
    })
    post_obs([{"obs_id": "a1", "boot_segment": "A", "counter": 500, "tide_level": 1.0}])
    assert get_obs("a1").json()["status"] == "conflict"
    assert get_obs("b1").json()["status"] == "conflict"
    assert tide(0, 100000)["count"] == 0

    # 重报串移开时间线 -> 双方恢复
    resp = post_string({
        "boot_segment": "A", "first_counter": 0, "counter_step": 100,
        "first_utc_ms": 2000, "utc_step_ms": 10, "point_count": 11,
    })
    assert changed_ids(resp) == {"a1", "b1"}
    assert get_obs("a1").json()["status"] == "ok"
    assert get_obs("b1").json()["status"] == "ok"


# ---------------------------------------------------------------- 持久化

def test_string_survives_connection_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    os.environ["BUOY_DB"] = str(db_path)
    db.reset_for_tests()
    try:
        conn = db.get_conn()
        with db.lock(), db.transaction(conn):
            core.upsert_calibration_string(conn, {
                "boot_segment": "A", "first_counter": 0, "counter_step": 100,
                "first_utc_ms": 1000, "utc_step_ms": 10, "point_count": 11,
            })
            core.upsert_observations(conn, [
                {"obs_id": "g", "boot_segment": "A", "counter": 300, "tide_level": 1.0},
            ])

        # 模拟重启：丢弃连接后重新打开同一文件
        db.reset_for_tests()
        conn2 = db.get_conn()
        points = core.load_points(conn2, "A")
        s = core.load_string(conn2, "A")
        assert points == [] and s["point_count"] == 11 and s["counter_step"] == 100
        result = core.compute_utc(points, s, 350)
        assert result is not None and result[0] == 1035
    finally:
        db.reset_for_tests()
        os.environ["BUOY_DB"] = ":memory:"
        db.get_conn()  # 恢复内存库，避免污染后续测试


# ---------------------------------------------------------------- 百万点不展开

def test_million_point_string_is_compact_and_fast():
    n = 1_000_000
    payload = {
        "boot_segment": "BIG", "first_counter": 0, "counter_step": 1000,
        "first_utc_ms": 1_700_000_000_000, "utc_step_ms": 1000, "point_count": n,
    }
    # 先挂一个观测（此时待校时），整串提交后应进入变更回执
    post_obs([{"obs_id": "end", "boot_segment": "BIG",
               "counter": 999_999_000, "tide_level": 1.0}])
    t0 = time.perf_counter()
    resp = post_string(payload)
    submit_ms = (time.perf_counter() - t0) * 1000

    # 存储始终只有一行，单点表无 BIG 段数据
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM calibration_points WHERE boot_segment = 'BIG'"
    ).fetchone()["n"]
    assert rows == 0
    srow = conn.execute(
        "SELECT * FROM calibration_strings WHERE boot_segment = 'BIG'"
    ).fetchone()
    assert srow["point_count"] == n

    # 末点换算：counter=999_999_000 -> utc=首值 + 999_999*1000
    assert get_obs("end").json()["utc_ms"] == 1_700_000_000_000 + 999_999_000

    # 全程耗时与串长无关（远小于逐点插入百万行的量级）
    assert submit_ms < 2000
    assert resp["changed_count"] == 1


def test_million_point_string_with_dense_single_overrides_stays_fast():
    # 2000 个连续覆盖串格点的单点：验证 free-index 跳格不会退化成逐点扫描
    n = 1_000_000
    post_string({
        "boot_segment": "BIG", "first_counter": 0, "counter_step": 1,
        "first_utc_ms": 0, "utc_step_ms": 1, "point_count": n,
    })
    singles = [
        {"boot_segment": "BIG", "counter": c, "utc_ms": c}
        for c in range(1000, 3000)
    ]
    t0 = time.perf_counter()
    post_cal(singles)
    elapsed = time.perf_counter() - t0
    assert elapsed < 3.0

    conn = db.get_conn()
    points = core.load_points(conn, "BIG")
    s = core.load_string(conn, "BIG")
    # 覆盖 run 之后的格点仍能正确取为有效锚点
    result = core.compute_utc(points, s, 3050)
    assert result is not None and result[0] == 3050
    # run 之前的格点不受影响
    result = core.compute_utc(points, s, 500)
    assert result is not None and result[0] == 500
