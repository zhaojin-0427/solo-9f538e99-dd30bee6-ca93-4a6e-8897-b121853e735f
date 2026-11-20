"""单浮标潮位校时 API。

浮标重启会使毫秒计数归零，本服务把「开机段 + 段内计数」换算成可信的
UTC 毫秒时间线供潮位分析：

  POST /observations   分批提交观测（ID、开机段、计数、潮位）
  POST /calibrations   分批提交单点校时（开机段、计数、UTC 毫秒）
  POST /calibration-series  按开机段提交等步长整串校时记录
  GET  /tide-levels    按 UTC 毫秒范围查可用潮位
  GET  /observations/{obs_id}  按观测 ID 查状态与原因
  GET  /docs           Swagger UI
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from . import core, db

# SQLite INTEGER 为有符号 64 位；首末值须落在非负整数范围内。
SQLITE_MAX_INT = core.SQLITE_MAX_INT
MAX_SERIES_POINTS = core.MAX_SERIES_POINTS

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
    """单点校时，或（附带步长与点数时）一条等步长整串的简写。

    - 仅给 ``counter`` / ``utc_ms``：普通单点；
    - 同时给出 ``counter_step``、``utc_step``、``count``：表示以
      ``(counter, utc_ms)`` 为首点、按步长展开 ``count`` 点的整串，
      等价于向 ``/calibrations/series`` 提交。
    """

    model_config = ConfigDict(populate_by_name=True)

    boot_segment: str = Field(min_length=1, description="开机段")
    counter: int = Field(ge=0, le=SQLITE_MAX_INT, description="首点/单点段内计数")
    utc_ms: int = Field(ge=0, le=SQLITE_MAX_INT, description="首点/单点 UTC 毫秒")
    counter_step: int | None = Field(
        default=None, gt=0, description="整串计数步长；与下两项同时给出"
    )
    utc_step: int | None = Field(
        default=None, ge=0, description="整串 UTC 毫秒步长"
    )
    count: int | None = Field(
        default=None, ge=1, le=MAX_SERIES_POINTS,
        validation_alias=AliasChoices("count", "points", "point_count"),
        description="整串点数，1..100 万",
    )

    @model_validator(mode="after")
    def _check_series_fields(self) -> "CalibrationIn":
        given = (self.counter_step, self.utc_step, self.count)
        if all(v is None for v in given):
            return self
        if any(v is None for v in given):
            raise ValueError("整串须同时给出 counter_step、utc_step 与 count")
        last_counter = self.counter + (self.count - 1) * self.counter_step
        last_utc = self.utc_ms + (self.count - 1) * self.utc_step
        if last_counter > SQLITE_MAX_INT or last_utc > SQLITE_MAX_INT:
            raise ValueError("末点计数或 UTC 毫秒超出 SQLite 非负整数范围")
        return self

    @property
    def is_series(self) -> bool:
        return self.counter_step is not None

    def as_series(self) -> dict:
        return {
            "boot_segment": self.boot_segment,
            "start_counter": self.counter,
            "start_utc_ms": self.utc_ms,
            "counter_step": self.counter_step,
            "utc_step": self.utc_step,
            "count": self.count,
        }

    def as_point(self) -> dict:
        return {
            "boot_segment": self.boot_segment,
            "counter": self.counter,
            "utc_ms": self.utc_ms,
        }


class ChangedState(BaseModel):
    obs_id: str
    utc_ms: int | None
    status: str


class BatchResult(BaseModel):
    changed_count: int = Field(description="本次提交中时刻或状态变化的观测数")
    changed: list[ChangedState] = Field(description="时刻或状态变化的观测")


class CalibrationSeriesIn(BaseModel):
    """一个开机段的等步长校时串：首点 + 两个步长 + 点数。

    串内各点满足 counter_i = start_counter + i * counter_step，
    utc_i = start_utc_ms + i * utc_step（i = 0..count-1）。
    首点也可用 counter / utc_ms 命名，点数也可用 points / point_count。
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    boot_segment: str = Field(min_length=1, description="开机段")
    start_counter: int = Field(
        ge=0, le=SQLITE_MAX_INT,
        validation_alias=AliasChoices(
            "start_counter", "first_counter", "counter"
        ),
        description="首点段内计数",
    )
    start_utc_ms: int = Field(
        ge=0, le=SQLITE_MAX_INT,
        validation_alias=AliasChoices(
            "start_utc_ms", "first_utc_ms", "utc_ms"
        ),
        description="首点 UTC 毫秒",
    )
    counter_step: int = Field(gt=0, description="计数步长，须为正")
    utc_step: int = Field(ge=0, description="UTC 毫秒步长，须非负")
    count: int = Field(
        ge=1, le=MAX_SERIES_POINTS,
        validation_alias=AliasChoices("count", "points", "point_count"),
        description="点数，1..100 万",
    )

    @model_validator(mode="after")
    def _check_last_point(self) -> "CalibrationSeriesIn":
        last_counter = self.start_counter + (self.count - 1) * self.counter_step
        last_utc = self.start_utc_ms + (self.count - 1) * self.utc_step
        if last_counter > SQLITE_MAX_INT or last_utc > SQLITE_MAX_INT:
            raise ValueError("末点计数或 UTC 毫秒超出 SQLite 非负整数范围")
        return self


class CalibrationSeriesBatch(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    series: list[CalibrationSeriesIn] = Field(
        min_length=1, validation_alias=AliasChoices("series", "calibration_series")
    )
    calibrations: list["CalibrationIn"] = Field(
        default_factory=list, description="同一批可附带单点（先落串，单点优先）"
    )


class CalibrationBatch(BaseModel):
    """单点与整串可同批提交；至少给出其中一种。"""

    model_config = ConfigDict(populate_by_name=True)

    calibrations: list[CalibrationIn] = Field(
        default_factory=list, validation_alias=AliasChoices("calibrations", "points")
    )
    series: list[CalibrationSeriesIn] = Field(
        default_factory=list,
        validation_alias=AliasChoices("series", "calibration_series"),
    )

    @model_validator(mode="after")
    def _require_any(self) -> "CalibrationBatch":
        if not self.calibrations and not self.series:
            raise ValueError("须至少提交一个校时点或一条整串")
        return self


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

def _submit_batch(conn, calibration_items: list[dict], series_items: list[dict]) -> BatchResult:
    """单事务内写入单点/整串、校验单调性、重算并返回原格式回执。

    任一步失败（含 409 倒退）由事务整体回滚，原校时资料与供数不受影响。
    """
    with db.lock():
        with db.transaction(conn):
            try:
                ids = core.apply_calibration_submission(
                    conn,
                    calibration_items=calibration_items,
                    series_items=series_items,
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


def _split(items) -> tuple[list[dict], list[dict]]:
    """把 CalibrationIn 列表拆成 (单点, 整串)。"""
    points = [c.as_point() for c in items if not c.is_series]
    series = [c.as_series() for c in items if c.is_series]
    return points, series


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
    """写入单点校时点（可选同批携带整串）；并重算受影响观测。

    - 同段同计数的单点替换旧值；单点在同计数上优先于等步长串，
      也可插在串内两点之间；
    - 若合并后任一开机段内 UTC 随计数倒退，则整批拒绝（409），旧数据照常供数；
    - 响应格式与单点、整串提交一致，列出时刻或状态变化的观测 ID。
    """
    points, shorthand = _split(batch.calibrations)
    series = [s.model_dump() for s in batch.series] + shorthand
    return _submit_batch(db.get_conn(), points, series)


@app.post(
    "/calibrations/series",
    response_model=BatchResult,
    summary="按开机段提交等步长整串校时记录",
)
def submit_calibration_series(batch: CalibrationSeriesBatch) -> BatchResult:
    """整串提交授时记录（首点计数/UTC 毫秒 + 两个步长 + 点数），回执格式不变。

    - 计数步长为正、UTC 毫秒步长非负，每串 1～100 万点，首末值须在
      SQLite 非负整数范围内；
    - 每段重报只替换该段上一串；
    - 旧库已有与 /calibrations 后补的单点在同计数上优先，也可插在串内两点之间；
    - 合并后若任一开机段 UTC 随计数倒退，整次拒绝（409），旧资料照常供数。

    校验、落库与重算均按「首点 + 步长 + 点数」算术处理，不逐点展开整串。
    """
    points, shorthand = _split(batch.calibrations)
    series = [s.model_dump() for s in batch.series] + shorthand
    return _submit_batch(db.get_conn(), points, series)


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
