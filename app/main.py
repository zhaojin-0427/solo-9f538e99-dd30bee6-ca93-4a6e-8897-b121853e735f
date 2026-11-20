"""单浮标潮位校时 API。

浮标重启会使毫秒计数归零，本服务把「开机段 + 段内计数」换算成可信的
UTC 毫秒时间线供潮位分析：

  POST /observations   分批提交观测（ID、开机段、计数、潮位）
  POST /calibrations   分批提交校时点（开机段、计数、UTC 毫秒）
  GET  /tide-levels    按 UTC 毫秒范围查可用潮位
  GET  /observations/{obs_id}  按观测 ID 查状态与原因
  GET  /docs           Swagger UI
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import core, db

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
