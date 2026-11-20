# 单浮标潮位校时服务

浮标每次重启都会把毫秒计数归零，潮位分析却需要一条可信的 UTC 时间线。
本服务（FastAPI + SQLite）接收「开机段 + 段内计数」的观测与校时点，
把计数线性换算成 UTC 毫秒，并维护观测的可用状态供分析查询。

长航次返港后的等步长授时记录可以**整串回传**：服务以一行算术序列紧凑
存储（首点 + 两个整数步长 + 点数），校验、落库与重算都不逐点展开，
单串 100 万点也始终只占一行。

## 快速开始

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互式文档在 http://localhost:8000/docs
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行测试
```

数据默认存放在命名卷 `buoy-data` 的 `/data/buoy.db`（可用环境变量
`BUOY_DB` 覆盖）。SQLite 为单写者模型，容器以单进程运行。

## 概念与规则

- **开机段（boot_segment）**：一次开机周期。计数只在段内单调，重启归零。
- **单点校时点（calibration point）**：`(开机段, 计数) -> UTC 毫秒`。
  同段同计数重复提交时新值替换旧值。
- **等步长校时串（calibration string）**：每开机段至多一串，第 k 点为
  `(first_counter + k·counter_step, first_utc_ms + k·utc_step_ms)`，
  `0 ≤ k < point_count`。重报只替换该段上一串。
- **观测（observation）**：`(观测 ID, 开机段, 计数, 潮位)`。
  同一观测 ID 重复提交会覆盖原文并重算。

### 单点与校时串的合并

两类校时点合并后共同构成一段的有效校时点：

- **单点优先**：旧库已有与 `/calibrations` 后补的单点若与串上某点同计数，
  串点被覆盖、以单点 UTC 为准；单点也可补在串内两个格点之间，把一个
  串区间拆成两段。
- 串上任意计数的 UTC 由算术公式 O(1) 求出，定位相邻有效点用整除 + 二分，
  不随串长增长；存储与重算都**不逐点展开整串**。
- **整次拒绝**：合并后若任一段内 UTC 随计数倒退，整次提交以 `409` 拒绝
  并回滚——旧串、旧单点与观测供数都保持原样，重启后仍以最后一次成功
  提交为准。串自身的 UTC 步长非负，串内不会倒退，故只需校验单点与
  相邻有效点的关系，成本只与单点数有关。

### 校时换算

- 计数落在相邻两个**有效**校时点之间时线性换算并**向下取整**：
  `utc = t_lo + (c - c_lo) * (t_hi - t_lo) // (c_hi - c_lo)`。
- 恰好命中有效校时点时直接取该点 UTC；只有一个校时点时仅该计数可换算。
- 计数落在本段校时区间之外：保留原文，状态置 `pending_calibration`（待校时）。
- 观测详情中的上下界校时点、跨段冲突判定均与逐点存入的有效校时点一致。

### 观测状态机

| 状态 | 含义 | 是否供分析 |
|---|---|---|
| `ok` | 已校时且无冲突 | ✅ |
| `pending_calibration` | 计数在校时区间外，待校时 | ❌ |
| `conflict` | 不同开机段落在同一 UTC 毫秒 | ❌ |

- **补传/更正校时点**会重算受影响观测（区间扩大可让待校时观测转为可用）。
- **冲突**：同一 UTC 毫秒被 ≥2 个不同开机段的观测占用时，这些观测全部
  置为 `conflict` 不供分析；更正校时点或覆盖观测解除冲突后自动恢复。
  同一开机段内多条观测共用同一毫秒不算冲突。
- **整批拒绝**：校时点合并后若使任一段内 UTC 随计数倒退，整个校时批次
  以 `409` 拒绝并回滚，旧数据照常供数。

### 原子性

每批提交（原文 + 校时点 + 重算 + 冲突刷新）都在**单个 SQLite 事务**中
完成，并由进程级锁串行化读写：原文、校时依据、分析供数同次生效，
查询和（崩溃后的）重启都不会看到半次重算的中间态。

## API

均可在 `/docs` 中直接调用。

### `POST /observations` — 分批提交观测

```json
{
  "observations": [
    {"obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500, "tide_level": 2.34}
  ]
}
```

### `POST /calibrations` — 分批提交单点校时点

```json
{
  "calibrations": [
    {"boot_segment": "boot-1", "counter": 1000, "utc_ms": 1700000000000},
    {"boot_segment": "boot-1", "counter": 2000, "utc_ms": 1700000001000}
  ]
}
```

### `POST /calibration-strings` — 整串提交等步长校时记录

按开机段提交首点的段内计数与 UTC 毫秒、两者各自的整数步长及点数：

```json
{
  "boot_segment": "boot-1",
  "first_counter": 0,
  "counter_step": 1000,
  "first_utc_ms": 1700000000000,
  "utc_step_ms": 1000,
  "point_count": 1000000
}
```

约束：`counter_step` 为正整数，`utc_step_ms` 为非负整数，`point_count`
为 1～100 万；首末计数与首末 UTC 毫秒均须在 SQLite 非负整数
（0 ～ 2^63−1）范围内。每段至多一串，重报只替换该段上一串。
合并后 UTC 随计数倒退时返回 `409` 并整次回滚。

三个提交接口的响应都是同一格式的**变更回执**，列出本次提交中时刻或
状态发生变化的观测 ID（含新观测，以及被卷入/解除冲突的其他观测）：

```json
{
  "changed_count": 1,
  "changed": [{"obs_id": "obs-1", "utc_ms": 1700000000500, "status": "ok"}]
}
```

### `GET /tide-levels?start_ms=...&end_ms=...` — 按 UTC 范围查可用潮位

只返回 `status=ok` 的观测，区间含端点，按 UTC 毫秒升序。

### `GET /observations/{obs_id}` — 按观测 ID 查状态和原因

返回原文、换算结果、状态、原因与校时依据（用于换算的上下界校时点）：

```json
{
  "obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500,
  "tide_level": 2.34, "utc_ms": 1700000000500, "status": "ok",
  "reason": null,
  "calibration": {
    "lo": {"counter": 1000, "utc_ms": 1700000000000},
    "hi": {"counter": 2000, "utc_ms": 1700000001000}
  }
}
```

### 其他

- `GET /health` — 健康检查
- `GET /docs` — Swagger UI；`GET /openapi.json` — OpenAPI 模式

## 目录结构

```
app/
  main.py   # FastAPI 路由与请求/响应模型
  core.py   # 串的 O(1) 几何、合并单调性校验、换算、重算、冲突检测、变更快照
  db.py     # SQLite 连接、模式（含 calibration_strings 紧凑串表）、事务与锁
tests/
  test_api.py                  # 基线端到端测试
  test_calibration_strings.py  # 整串提交、替换、单点优先、拒绝、持久化、百万点
  test_geometry_fuzz.py        # 紧凑算法对照暴力展开的随机化 property 测试
Dockerfile
docker-compose.yml
```
