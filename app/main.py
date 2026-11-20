"""单浮标潮位校时 API。

浮标重启会使毫秒计数归零，本服务把「开机段 + 段内计数」换算成可信的
UTC 毫秒时间线供潮位分析：

  POST /observations         分批提交观测（ID、开机段、计数、潮位）
  POST /calibrations         分批提交单点校时点（开机段、计数、UTC 毫秒）
  POST /calibration-strings  整串提交等步长校时记录（首点 + 两个步长 + 点数）
  GET  /tide-levels          按 UTC 毫秒范围查可用潮位
  GET  /observations/{obs_id}  按观测 ID 查状态与原因
  GET  /docs                 Swagger UI

等步长串以一行算术序列存储、绝不逐点展开（单串可达 100 万点）；
单点与串同计数时单点优先，合并后 UTC 随计数倒退则整次拒绝。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from . import core, db
from .core import MAX_STRING_POINTS

app = FastAPI(
    title="单浮标潮位校时服务",
    description=__doc__,
    version="1.0.0",
)


# ---------------------------------------------------------------- 模型

class ObservationIn(BaseModel):
    obs_id: str = Field(min_length=1, description="观测 ID")
    boot_segment: str = Field(min_length=1, description="开机段")
    counter: int = Field(ge=0, description="段内毫秒计数")
    tide_level: float = Field(description="潮位")


class ObservationBatch(BaseModel):
    observations: list[ObservationIn] = Field(min_length=1)


class CalibrationIn(BaseModel):
    boot_segment: str = Field(min_length=1, description="开机段")
    counter: int = Field(ge=0, description="段内毫秒计数")
    utc_ms: int = Field(ge=0, description="对应的 UTC 毫秒")


class CalibrationBatch(BaseModel):
    calibrations: list[CalibrationIn] = Field(min_length=1)


# SQLite 非负整数（64 位有符号）上界
_SQLITE_MAX_INT = 2**63 - 1


class CalibrationStringIn(BaseModel):
    """等步长校时串：长航次返港后整串回传，第 k 点为
    (first_counter + k*counter_step, first_utc_ms + k*utc_step_ms)。"""

    boot_segment: str = Field(min_length=1, description="开机段（一段一串，重报替换上一串）")
    first_counter: int = Field(ge=0, description="首点的段内计数")
    counter_step: int = Field(gt=0, description="计数整数步长（正）")
    first_utc_ms: int = Field(ge=0, description="首点 UTC 毫秒")
    utc_step_ms: int = Field(ge=0, description="UTC 毫秒整数步长（非负）")
    point_count: int = Field(ge=1, le=MAX_STRING_POINTS, description="点数（1～100 万）")

    @model_validator(mode="after")
    def _endpoints_fit_sqlite_nonnegative_integer(self) -> "CalibrationStringIn":
        last_counter = self.first_counter + (self.point_count - 1) * self.counter_step
        last_utc_ms = self.first_utc_ms + (self.point_count - 1) * self.utc_step_ms
        if last_counter > _SQLITE_MAX_INT:
            raise ValueError("末点计数超出 SQLite 非负整数范围")
        if last_utc_ms > _SQLITE_MAX_INT:
            raise ValueError("末点 UTC 毫秒超出 SQLite 非负整数范围")
        return self


class ChangedState(BaseModel):
    obs_id: str
    utc_ms: int | None
    status: str


class BatchResult(BaseModel):
    changed_count: int = Field(description="本次提交中时刻或状态变化的观测数")
    changed: list[ChangedState] = Field(description="时刻或状态变化的观测")


class TidePoint(BaseModel):
    obs_id: str
    utc_ms: int
    tide_level: float


class TideQueryResult(BaseModel):
    count: int
    items: list[TidePoint]


class CalibrationEndpoint(BaseModel):
    counter: int
    utc_ms: int


class CalibrationBasis(BaseModel):
    lo: CalibrationEndpoint = Field(description="下界校时点")
    hi: CalibrationEndpoint = Field(description="上界校时点")


class ObservationDetail(BaseModel):
    obs_id: str
    boot_segment: str
    counter: int
    tide_level: float
    utc_ms: int | None
    status: str = Field(description="ok / pending_calibration / conflict")
    reason: str | None
    calibration: CalibrationBasis | None = Field(description="校时依据；待校时为 null")


# ---------------------------------------------------------------- 提交

@app.post("/observations", response_model=BatchResult, summary="分批提交观测")
def submit_observations(batch: ObservationBatch) -> BatchResult:
    """写入观测原文并按当前校时点换算；同一 obs_id 重复提交会覆盖原文并重算。

    响应列出本次提交中时刻或状态发生变化的观测 ID（含新观测，
    以及因新观测落入同一 UTC 毫秒而被卷入/解除冲突的观测）。
    """
    conn = db.get_conn()
    with db.lock():
        with db.transaction(conn):
            ids = core.upsert_observations(
                conn, [o.model_dump() for o in batch.observations]
            )
            changed = core.fetch_states(conn, ids)
    return BatchResult(changed_count=len(changed), changed=changed)


@app.post("/calibrations", response_model=BatchResult, summary="分批提交校时点")
def submit_calibrations(batch: CalibrationBatch) -> BatchResult:
    """写入校时点；同段同计数的新校时点替换旧值，并重算受影响观测。

    若合并后任一开机段内 UTC 随计数倒退，则整批拒绝（409），旧数据照常供数。
    响应列出本次提交中时刻或状态发生变化的观测 ID。
    """
    conn = db.get_conn()
    with db.lock():
        with db.transaction(conn):
            try:
                ids = core.upsert_calibrations(
                    conn, [c.model_dump() for c in batch.calibrations]
                )
            except core.MonotonicViolation as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "校时点使段内 UTC 倒退，整批拒绝，旧数据照常供数",
                        "violations": exc.violations,
                    },
                )
            changed = core.fetch_states(conn, ids)
    return BatchResult(changed_count=len(changed), changed=changed)


@app.post(
    "/calibration-strings",
    response_model=BatchResult,
    summary="整串提交等步长校时记录",
)
def submit_calibration_string(item: CalibrationStringIn) -> BatchResult:
    """按开机段整串提交等步长授时记录（首点段内计数与 UTC 毫秒、两者各自的
    整数步长及点数）。串以算术序列紧凑存储，**不逐点展开**。

    - 每开机段至多一串：重报只替换该段上一串；每串 1～100 万点，
      计数步长为正、UTC 毫秒步长非负，首末值均须在 SQLite 非负整数范围内。
    - 旧库已有与 `/calibrations` 后补的单点在同一计数上优先，也可补在串内
      两点之间；合并后若 UTC 随计数倒退，整次拒绝（409）并回滚，
      旧校时资料和供数照常可查，重启后仍以最后一次成功提交为准。
    - 响应与另外两个提交接口同格式，列出时刻或状态发生变化的观测 ID。
    """
    conn = db.get_conn()
    with db.lock():
        with db.transaction(conn):
            try:
                ids = core.upsert_calibration_string(conn, item.model_dump())
            except core.MonotonicViolation as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "校时串与已有校时点合并后 UTC 倒退，整次拒绝，旧数据照常供数",
                        "violations": exc.violations,
                    },
                )
            changed = core.fetch_states(conn, ids)
    return BatchResult(changed_count=len(changed), changed=changed)


# ---------------------------------------------------------------- 查询

@app.get("/tide-levels", response_model=TideQueryResult, summary="按 UTC 范围查可用潮位")
def tide_levels(
    start_ms: int = Query(ge=0, description="起始 UTC 毫秒（含）"),
    end_ms: int = Query(ge=0, description="结束 UTC 毫秒（含）"),
) -> TideQueryResult:
    """只返回已校时且无冲突（status=ok）的观测，按 UTC 毫秒升序。"""
    if end_ms < start_ms:
        raise HTTPException(status_code=422, detail="end_ms 不得小于 start_ms")
    conn = db.get_conn()
    with db.lock():
        rows = conn.execute(
            "SELECT obs_id, utc_ms, tide_level FROM observations "
            "WHERE status = ? AND utc_ms BETWEEN ? AND ? "
            "ORDER BY utc_ms, obs_id",
            (core.OK, start_ms, end_ms),
        ).fetchall()
    items = [
        TidePoint(obs_id=r["obs_id"], utc_ms=r["utc_ms"], tide_level=r["tide_level"])
        for r in rows
    ]
    return TideQueryResult(count=len(items), items=items)


@app.get(
    "/observations/{obs_id}",
    response_model=ObservationDetail,
    summary="按观测 ID 查状态和原因",
)
def get_observation(obs_id: str) -> ObservationDetail:
    conn = db.get_conn()
    with db.lock():
        row = conn.execute(
            "SELECT * FROM observations WHERE obs_id = ?", (obs_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"观测 '{obs_id}' 不存在")
    calibration = None
    if row["cal_lo_counter"] is not None:
        calibration = CalibrationBasis(
            lo=CalibrationEndpoint(
                counter=row["cal_lo_counter"], utc_ms=row["cal_lo_utc_ms"]
            ),
            hi=CalibrationEndpoint(
                counter=row["cal_hi_counter"], utc_ms=row["cal_hi_utc_ms"]
            ),
        )
    return ObservationDetail(
        obs_id=row["obs_id"],
        boot_segment=row["boot_segment"],
        counter=row["counter"],
        tide_level=row["tide_level"],
        utc_ms=row["utc_ms"],
        status=row["status"],
        reason=row["reason"],
        calibration=calibration,
    )


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "up"}
