"""等步长校时串：校验、换算、单点优先/插入、整串替换、单调拒绝与不展开。"""

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
        conn.execute("DELETE FROM calibration_series")
    yield


def post_obs(items):
    r = client.post("/observations", json={"observations": items})
    assert r.status_code == 200, r.text
    return r.json()


def post_cal(items, expect=200):
    r = client.post("/calibrations", json={"calibrations": items})
    assert r.status_code == expect, r.text
    return r.json()


def post_series(items, expect=200):
    r = client.post("/calibrations/series", json={"series": items})
    assert r.status_code == expect, r.text
    return r.json()


def series(**kw):
    base = {
        "boot_segment": "A",
        "start_counter": 1000,
        "start_utc_ms": 10000,
        "counter_step": 500,
        "utc_step": 300,
        "count": 3,
    }
    base.update(kw)
    return base


def get_obs(obs_id):
    r = client.get(f"/observations/{obs_id}")
    assert r.status_code == 200, r.text
    return r.json()


def tide(start, end):
    r = client.get("/tide-levels", params={"start_ms": start, "end_ms": end})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 校验

@pytest.mark.parametrize("field,value", [
    ("counter_step", 0),
    ("counter_step", -1),
    ("utc_step", -1),
    ("count", 0),
    ("count", 1_000_001),
    ("start_counter", -1),
    ("start_utc_ms", -1),
])
def test_series_field_validation(field, value):
    r = client.post("/calibrations/series", json={"series": [series(**{field: value})]})
    assert r.status_code == 422, r.text


def test_series_last_point_must_fit_sqlite_int():
    # 末点 UTC 超出 64 位有符号整数上界
    r = client.post("/calibrations/series", json={"series": [
        series(start_utc_ms=core.SQLITE_MAX_INT - 10, utc_step=1, count=100),
    ]})
    assert r.status_code == 422, r.text
    # 首末在范围内则接受（末点恰为 2**63-1）
    body = post_series([series(start_utc_ms=core.SQLITE_MAX_INT - 2, utc_step=1, count=3)])
    assert body["changed_count"] == 0


def test_empty_batch_rejected():
    r = client.post("/calibrations/series", json={"series": []})
    assert r.status_code == 422


# ---------------------------------------------------------------- 换算

def test_series_interpolation_basis_and_floor():
    post_series([series()])  # A: 1000->10000, 1500->10300, 2000->10600
    post_obs([
        {"obs_id": "hit0", "boot_segment": "A", "counter": 1000, "tide_level": 1.0},
        {"obs_id": "between", "boot_segment": "A", "counter": 1001, "tide_level": 2.0},
        {"obs_id": "hit1", "boot_segment": "A", "counter": 1500, "tide_level": 3.0},
        {"obs_id": "float", "boot_segment": "A", "counter": 1750, "tide_level": 4.0},
        {"obs_id": "hi", "boot_segment": "A", "counter": 2000, "tide_level": 5.0},
        {"obs_id": "below", "boot_segment": "A", "counter": 999, "tide_level": 6.0},
        {"obs_id": "above", "boot_segment": "A", "counter": 2001, "tide_level": 7.0},
    ])
    assert get_obs("hit0")["utc_ms"] == 10000
    # 10000 + 1*300//500 = 10000 + 0 -> 向下取整
    assert get_obs("between")["utc_ms"] == 10000
    assert get_obs("hit1")["utc_ms"] == 10300
    floor = get_obs("float")
    assert floor["utc_ms"] == 10300 + 250 * 300 // 500  # 10450
    assert floor["calibration"] == {
        "lo": {"counter": 1500, "utc_ms": 10300},
        "hi": {"counter": 2000, "utc_ms": 10600},
    }
    assert get_obs("hi")["utc_ms"] == 10600
    for oid in ("below", "above"):
        body = get_obs(oid)
        assert body["status"] == "pending_calibration"
        assert body["utc_ms"] is None and body["calibration"] is None


def test_single_point_series_anchors_only_that_counter():
    post_series([series(count=1, start_utc_ms=7777)])
    post_obs([
        {"obs_id": "hit", "boot_segment": "A", "counter": 1000, "tide_level": 1.0},
        {"obs_id": "miss", "boot_segment": "A", "counter": 1001, "tide_level": 2.0},
    ])
    assert get_obs("hit")["status"] == "ok"
    assert get_obs("hit")["utc_ms"] == 7777
    assert get_obs("miss")["status"] == "pending_calibration"


def test_series_equals_pointwise_calibration():
    """同段串的换算结果与逐点存入完全一致。"""
    pts = [{"boot_segment": "B", "counter": 1000 + 500 * i, "utc_ms": 10000 + 300 * i}
           for i in range(4)]
    post_cal(pts)
    post_series([series(boot_segment="C")])  # 只有 3 点；改用 4 点
    post_series([series(boot_segment="C", count=4)])

    counters = [999, 1000, 1001, 1249, 1500, 2010, 2500, 2501]
    obs = []
    for i, c in enumerate(counters):
        obs.append({"obs_id": f"b{i}", "boot_segment": "B", "counter": c, "tide_level": 1.0})
        obs.append({"obs_id": f"c{i}", "boot_segment": "C", "counter": c, "tide_level": 2.0})
    post_obs(obs)
    for i in range(len(counters)):
        b, c = get_obs(f"b{i}"), get_obs(f"c{i}")
        assert (b["utc_ms"], b["status"], b["calibration"]) == (
            c["utc_ms"], c["status"], c["calibration"]
        ), (counters[i], b, c)


# ---------------------------------------------------------------- 单点优先与插入

def test_explicit_point_on_same_counter_wins():
    post_series([series()])
    post_obs([{"obs_id": "mid", "boot_segment": "A", "counter": 1500, "tide_level": 1.0}])
    assert get_obs("mid")["utc_ms"] == 10300

    # 同计数单点优先；其值须仍落在两侧串点之间，否则整批按倒退拒绝
    resp = post_cal([{"boot_segment": "A", "counter": 1500, "utc_ms": 10350}], expect=200)
    assert {c["obs_id"] for c in resp["changed"]} == {"mid"}
    body = get_obs("mid")
    assert body["utc_ms"] == 10350
    assert body["calibration"] == {
        "lo": {"counter": 1500, "utc_ms": 10350},
        "hi": {"counter": 1500, "utc_ms": 10350},
    }
    # 相邻区间改用该单点作边界
    post_obs([{"obs_id": "after", "boot_segment": "A", "counter": 1600, "tide_level": 1.0}])
    assert get_obs("after")["calibration"]["lo"] == {"counter": 1500, "utc_ms": 10350}

    # 超出右界的单点使段内 UTC 倒退 -> 整批拒绝，原串保留
    post_cal([{"boot_segment": "A", "counter": 1500, "utc_ms": 99999}], expect=409)
    assert get_obs("mid")["utc_ms"] == 10350


def test_explicit_point_between_series_points_is_used():
    post_series([series()])
    post_obs([{"obs_id": "x", "boot_segment": "A", "counter": 1250, "tide_level": 1.0}])
    assert get_obs("x")["utc_ms"] == 10000 + 250 * 300 // 500  # 10150

    # 插在串内 1000 与 1500 之间（UTC 须不小于左界、不大于右界才不倒退）
    resp = post_cal([{"boot_segment": "A", "counter": 1250, "utc_ms": 10150}])
    # 同值命中不改变观测时刻，回执无需列出
    assert {c["obs_id"] for c in resp["changed"]} == set()
    body = get_obs("x")
    assert body["utc_ms"] == 10150
    assert body["calibration"] == {
        "lo": {"counter": 1250, "utc_ms": 10150},
        "hi": {"counter": 1250, "utc_ms": 10150},
    }
    # 其右侧区间从该单点起算
    post_obs([{"obs_id": "y", "boot_segment": "A", "counter": 1375, "tide_level": 1.0}])
    assert get_obs("y")["calibration"]["lo"] == {"counter": 1250, "utc_ms": 10150}
    assert get_obs("y")["calibration"]["hi"] == {"counter": 1500, "utc_ms": 10300}


def test_explicit_points_can_extend_beyond_series():
    post_series([series()])
    post_obs([
        {"obs_id": "low", "boot_segment": "A", "counter": 500, "tide_level": 1.0},
        {"obs_id": "high", "boot_segment": "A", "counter": 3000, "tide_level": 2.0},
    ])
    assert get_obs("low")["status"] == "pending_calibration"
    assert get_obs("high")["status"] == "pending_calibration"

    resp = post_cal([
        {"boot_segment": "A", "counter": 500, "utc_ms": 0},
        {"boot_segment": "A", "counter": 3000, "utc_ms": 12000},
    ])
    assert {c["obs_id"] for c in resp["changed"]} == {"low", "high"}
    assert get_obs("low")["status"] == "ok" and get_obs("low")["utc_ms"] == 0
    assert get_obs("high")["status"] == "ok" and get_obs("high")["utc_ms"] == 12000


# ---------------------------------------------------------------- 整串替换

def test_resubmission_replaces_series_and_recalculates():
    post_series([series()])
    post_obs([{"obs_id": "mid", "boot_segment": "A", "counter": 1500, "tide_level": 1.0}])
    assert get_obs("mid")["utc_ms"] == 10300

    resp = post_series([series(start_utc_ms=20000, utc_step=1000)])
    assert {c["obs_id"] for c in resp["changed"]} == {"mid"}
    assert get_obs("mid")["utc_ms"] == 21000

    # 只保留一行（不逐点展开）
    conn = db.get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM calibration_series WHERE boot_segment='A'"
    ).fetchone()["n"]
    assert n == 1


def test_replace_series_can_shrink_range_and_pend_observations():
    post_series([series(count=5)])  # 1000..3000
    post_obs([{"obs_id": "far", "boot_segment": "A", "counter": 3000, "tide_level": 1.0}])
    assert get_obs("far")["status"] == "ok"

    resp = post_series([series(count=2)])  # 1000..1500
    assert {c["obs_id"] for c in resp["changed"]} == {"far"}
    assert get_obs("far")["status"] == "pending_calibration"


# ---------------------------------------------------------------- 单调拒绝

def test_backwards_merge_between_series_and_point_is_rejected():
    post_series([series()])  # 1000->10000, 1500->10300, 2000->10600
    post_obs([{"obs_id": "o", "boot_segment": "A", "counter": 1750, "tide_level": 1.0}])
    before = get_obs("o")

    resp = post_cal([{"boot_segment": "A", "counter": 1750, "utc_ms": 9000}], expect=409)
    assert "整批拒绝" in resp["detail"]["message"]
    # 9000 低于左界 1500->10300，构成倒退
    assert resp["detail"]["violations"] == [{
        "boot_segment": "A", "counter": 1750, "utc_ms": 9000,
        "prev_counter": 1500, "prev_utc_ms": 10300,
    }]
    # 回滚：单点未落库，旧串与观测照常
    after = get_obs("o")
    assert after["utc_ms"] == before["utc_ms"] and after["calibration"] == before["calibration"]


def test_new_series_backwards_vs_explicit_point_is_rejected():
    post_cal([{"boot_segment": "A", "counter": 1500, "utc_ms": 500}])
    post_obs([{"obs_id": "o", "boot_segment": "A", "counter": 1500, "tide_level": 1.0}])

    # 合并后：1000->500, 1500->500（单点覆盖串点）, 2000->500，不倒退
    resp = post_series([
        series(start_counter=1000, start_utc_ms=500, counter_step=500, utc_step=0, count=3)
    ], expect=200)
    assert resp["changed_count"] == 0  # o 在 1500 处仍是 500
    assert get_obs("o")["calibration"] == {
        "lo": {"counter": 1500, "utc_ms": 500},
        "hi": {"counter": 1500, "utc_ms": 500},
    }

    # 换成 1000->0, 1500->500（单点）, 2000->0：2000 处低于单点 500 -> 倒退
    resp = post_series([
        series(start_counter=1000, start_utc_ms=0, counter_step=500, utc_step=0, count=3)
    ], expect=409)
    assert "整批拒绝" in resp["detail"]["message"]
    assert get_obs("o")["utc_ms"] == 500
    # 最后一次成功提交的串仍在: 2000 -> 500
    post_obs([{"obs_id": "p", "boot_segment": "A", "counter": 2000, "tide_level": 1.0}])
    assert get_obs("p")["utc_ms"] == 500


def test_series_internal_backwards_impossible_by_validation():
    # utc_step >= 0 且 counter_step > 0，故串内天然单调
    assert client.post("/calibrations/series", json={"series": [
        series(utc_step=-1),
    ]}).status_code == 422


# ---------------------------------------------------------------- 冲突

def test_conflicts_match_pointwise_semantics():
    post_series([series(boot_segment="S", start_counter=0, start_utc_ms=1000,
                        counter_step=10, utc_step=0, count=3)])
    post_obs([{"obs_id": "s1", "boot_segment": "S", "counter": 10, "tide_level": 1.0}])
    assert get_obs("s1")["status"] == "ok"

    # 另一段单点落在同一毫秒 -> 双方冲突，且都进变更清单
    post_cal([{"boot_segment": "T", "counter": 0, "utc_ms": 1000}])
    resp = post_obs([{"obs_id": "t1", "boot_segment": "T", "counter": 0, "tide_level": 2.0}])
    assert {c["obs_id"] for c in resp["changed"]} == {"s1", "t1"}
    assert get_obs("s1")["status"] == "conflict"
    assert get_obs("t1")["status"] == "conflict"
    assert tide(0, 99999)["count"] == 0

    # 替换整串解除冲突
    resp = post_series([series(boot_segment="S", start_counter=0, start_utc_ms=5000,
                               counter_step=10, utc_step=0, count=3)])
    assert {c["obs_id"] for c in resp["changed"]} == {"s1", "t1"}
    assert get_obs("s1")["status"] == "ok"
    assert get_obs("t1")["status"] == "ok"


# ---------------------------------------------------------------- 不展开整串

def test_million_point_series_is_not_expanded():
    n = 1_000_000
    conn = db.get_conn()
    with db.lock():
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM calibration_points")
        conn.execute("DELETE FROM calibration_series")

    started = time.monotonic()
    resp = post_series([series(count=n, start_counter=0, counter_step=1,
                               start_utc_ms=0, utc_step=1)])
    elapsed = time.monotonic() - started
    assert resp["changed_count"] == 0
    assert elapsed < 5.0

    # 库中只有一行，绝不逐点写入
    assert conn.execute("SELECT COUNT(*) AS n FROM calibration_series").fetchone()["n"] == 1

    # 首末点及中间点均可按算术换算
    post_obs([
        {"obs_id": "first", "boot_segment": "A", "counter": 0, "tide_level": 1.0},
        {"obs_id": "mid", "boot_segment": "A", "counter": n // 2, "tide_level": 2.0},
        {"obs_id": "last", "boot_segment": "A", "counter": n - 1, "tide_level": 3.0},
    ])
    assert get_obs("first")["utc_ms"] == 0
    assert get_obs("mid")["utc_ms"] == n // 2
    assert get_obs("last")["utc_ms"] == n - 1

    # 末点后一位待校时
    post_obs([{"obs_id": "past", "boot_segment": "A", "counter": n, "tide_level": 4.0}])
    assert get_obs("past")["status"] == "pending_calibration"


def test_single_point_on_million_series_is_o1_and_wins():
    n = 1_000_000
    post_series([series(count=n, start_counter=0, counter_step=1,
                        start_utc_ms=0, utc_step=1)])
    started = time.monotonic()
    post_cal([{"boot_segment": "A", "counter": n // 3, "utc_ms": n // 3}])
    assert time.monotonic() - started < 5.0
    post_obs([{"obs_id": "hit", "boot_segment": "A", "counter": n // 3, "tide_level": 1.0}])
    assert get_obs("hit")["utc_ms"] == n // 3


# ---------------------------------------------------------------- 回执与查询

def test_series_receipt_keeps_original_shape():
    body = post_series([series()])
    assert set(body) == {"changed_count", "changed"}
    assert body == {"changed_count": 0, "changed": []}

    post_obs([{"obs_id": "a", "boot_segment": "A", "counter": 1500, "tide_level": 1.0}])
    body = post_series([series(start_utc_ms=1, utc_step=1)])
    assert set(body) == {"changed_count", "changed"}
    assert body["changed"] == [{"obs_id": "a", "utc_ms": 2, "status": "ok"}]
    # 串点 1500 处 UTC 由 10300 变为 2 -> 观测随之更新


def test_series_then_late_single_point_receipt_shape_matches_batch_result():
    post_series([series()])
    post_obs([{"obs_id": "a", "boot_segment": "A", "counter": 2000, "tide_level": 1.0}])
    # 同计数单点替换串点；值须落在邻域内才不倒退
    body = post_cal([{"boot_segment": "A", "counter": 2000, "utc_ms": 10650}])
    assert set(body) == {"changed_count", "changed"}
    assert body["changed"] == [{"obs_id": "a", "utc_ms": 10650, "status": "ok"}]


def test_openapi_lists_series_endpoint():
    paths = client.get("/openapi.json").json()["paths"]
    assert "/calibrations/series" in paths


# ---------------------------------------------------------------- 单点接口的整串简写

def test_calibrations_endpoint_accepts_series_shorthand():
    resp = post_cal([{
        "boot_segment": "A", "counter": 1000, "utc_ms": 10000,
        "counter_step": 500, "utc_step": 300, "count": 3,
    }])
    assert set(resp) == {"changed_count", "changed"}
    post_obs([
        {"obs_id": "lo", "boot_segment": "A", "counter": 1000, "tide_level": 1.0},
        {"obs_id": "mid", "boot_segment": "A", "counter": 1500, "tide_level": 2.0},
        {"obs_id": "hi", "boot_segment": "A", "counter": 2000, "tide_level": 3.0},
    ])
    assert [get_obs(o)["utc_ms"] for o in ("lo", "mid", "hi")] == [10000, 10300, 10600]
    # 落库仍是一行，不逐点展开
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) AS n FROM calibration_series").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM calibration_points").fetchone()["n"] == 0


def test_calibrations_series_shorthand_requires_all_three_fields():
    r = client.post("/calibrations", json={"calibrations": [{
        "boot_segment": "A", "counter": 0, "utc_ms": 0, "counter_step": 1, "count": 5,
    }]})
    assert r.status_code == 422
    r = client.post("/calibrations", json={"calibrations": [{
        "boot_segment": "A", "counter": 0, "utc_ms": 0, "utc_step": 1,
    }]})
    assert r.status_code == 422


def test_mixed_batch_point_wins_over_series_in_same_request():
    # 同批：串 1000->10000 + 单点 1500->10300（与串内点同值），单点优先落库
    resp = post_cal([
        {"boot_segment": "A", "counter": 1000, "utc_ms": 10000,
         "counter_step": 500, "utc_step": 300, "count": 3},
        {"boot_segment": "A", "counter": 1500, "utc_ms": 10300},
    ])
    assert set(resp) == {"changed_count", "changed"}
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) AS n FROM calibration_series").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM calibration_points").fetchone()["n"] == 1
    post_obs([{"obs_id": "p", "boot_segment": "A", "counter": 1500, "tide_level": 1.0}])
    assert get_obs("p")["calibration"] == {
        "lo": {"counter": 1500, "utc_ms": 10300},
        "hi": {"counter": 1500, "utc_ms": 10300},
    }


def test_mixed_batch_rejects_atomically_on_backwards():
    post_cal([{"boot_segment": "A", "counter": 2000, "utc_ms": 5000}])
    r = client.post("/calibrations", json={"calibrations": [
        {"boot_segment": "A", "counter": 1000, "utc_ms": 10000,
         "counter_step": 500, "utc_step": 300, "count": 3},
        {"boot_segment": "A", "counter": 2000, "utc_ms": 1},
    ]})
    assert r.status_code == 409, r.text
    # 整批回滚：串未落库，原单点仍在
    conn = db.get_conn()
    assert conn.execute("SELECT COUNT(*) AS n FROM calibration_series").fetchone()["n"] == 0
    post_obs([{"obs_id": "p", "boot_segment": "A", "counter": 2000, "tide_level": 1.0}])
    assert get_obs("p")["utc_ms"] == 5000


def test_docs_exposes_series_fields_on_calibrations():
    schema = client.get("/openapi.json").json()["components"]["schemas"]["CalibrationIn"]
    for field in ("counter_step", "utc_step", "count"):
        assert field in schema["properties"]
