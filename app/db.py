"""SQLite 连接、模式与事务助手。

设计：单进程单连接 + 进程级写锁。所有写路径在 BEGIN IMMEDIATE ...
COMMIT 的单个事务中执行，因此原文、校时依据、分析供数状态同次生效；
查询与（崩溃后）重启都不会看到半次重算的中间态。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager

DEFAULT_DB_PATH = "./buoy.db"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS calibration_points (
    boot_segment TEXT NOT NULL,      -- 开机段
    counter      INTEGER NOT NULL,   -- 段内毫秒计数
    utc_ms       INTEGER NOT NULL,   -- 对应 UTC 毫秒
    PRIMARY KEY (boot_segment, counter)
);

-- 等步长校时串：每个开机段至多一串，整串只占一行，不逐点展开。
CREATE TABLE IF NOT EXISTS calibration_series (
    boot_segment  TEXT PRIMARY KEY,
    start_counter INTEGER NOT NULL,  -- 首点段内计数
    start_utc_ms  INTEGER NOT NULL,  -- 首点 UTC 毫秒
    counter_step  INTEGER NOT NULL,  -- 计数步长，> 0
    utc_step      INTEGER NOT NULL,  -- UTC 毫秒步长，>= 0
    count         INTEGER NOT NULL   -- 点数，1..1_000_000
);

CREATE TABLE IF NOT EXISTS observations (
    obs_id         TEXT PRIMARY KEY, -- 观测 ID
    boot_segment   TEXT NOT NULL,    -- 原文：开机段
    counter        INTEGER NOT NULL, -- 原文：段内计数
    tide_level     REAL NOT NULL,    -- 原文：潮位
    utc_ms         INTEGER,          -- 换算结果；NULL 表示待校时
    status         TEXT NOT NULL,    -- ok / pending_calibration / conflict
    reason         TEXT,             -- 状态原因
    cal_lo_counter INTEGER,          -- 校时依据：下界校时点
    cal_lo_utc_ms  INTEGER,
    cal_hi_counter INTEGER,          -- 校时依据：上界校时点
    cal_hi_utc_ms  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_observations_utc_ms ON observations(utc_ms);
CREATE INDEX IF NOT EXISTS idx_observations_segment ON observations(boot_segment);
"""


def get_conn() -> sqlite3.Connection:
    """返回进程内共享连接（惰性创建并建表）。"""
    global _conn
    if _conn is None:
        path = os.environ.get("BUOY_DB", DEFAULT_DB_PATH)
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        _conn = conn
    return _conn


def lock() -> threading.RLock:
    """读写共用的进程级锁：写事务进行中，查询不会读到中间态。"""
    return _lock


@contextmanager
def transaction(conn: sqlite3.Connection):
    """单事务包裹一批写入与重算；异常即整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def reset_for_tests() -> None:
    """测试用：丢弃当前连接（下次 get_conn 重新建库）。"""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
