# 单浮标潮位校时服务

浮标每次重启都会把毫秒计数归零，潮位分析却需要一条可信的 UTC 时间线。
本服务（FastAPI + SQLite）接收「开机段 + 段内计数」的观测与校时点，
把计数线性换算成 UTC 毫秒，并维护观测的可用状态供分析查询。

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
- **校时点（calibration point）**：`(开机段, 计数) -> UTC 毫秒`。
  同段同计数重复提交时新值替换旧值。
- **观测（observation）**：`(观测 ID, 开机段, 计数, 潮位)`。
  同一观测 ID 重复提交会覆盖原文并重算。

### 校时换算

- 计数落在相邻两个校时点之间时线性换算并**向下取整**：
  `utc = t_lo + (c - c_lo) * (t_hi - t_lo) // (c_hi - c_lo)`。
- 恰好命中校时点时直接取该点 UTC；只有一个校时点时仅该计数可换算。
- 计数落在本段校时区间之外：保留原文，状态置 `pending_calibration`（待校时）。

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

### 等步长校时串

长航次返港后，等步长的授时记录可整串回传，无需逐点提交：

- 每个开机段至多一串，只记录 **首点**（段内计数、UTC 毫秒）、
  **计数步长**与 **UTC 毫秒步长**、**点数**；库中各段只存一行，不逐点展开。
- 约束：计数步长 `> 0`，UTC 毫秒步长 `>= 0`，每串 `1～1_000_000` 点，
  首末值均须落在 SQLite 非负整数（`0 .. 2^63-1`）范围内。
- **重报只替换该段上一串**。
- **单点优先**：旧库已有与 `/calibrations` 后补的单点在同一计数上覆盖串点，
  也可以插在串内两点之间；换算、上下界与冲突都按合并后的有效校时点序列
  计算，与“把整串逐点存入”完全一致。
- 合并后的 UTC 随计数倒退（单点越界、串替换换序等）→ **整次拒绝（409）**，
  原校时资料与供数照常可查。
- 校验、落库与重算都按「首点 + 步长 + 点数」做常数次算术，**不逐点展开整串**。

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

### `POST /calibrations` — 分批提交校时点

```json
{
  "calibrations": [
    {"boot_segment": "boot-1", "counter": 1000, "utc_ms": 1700000000000},
    {"boot_segment": "boot-1", "counter": 2000, "utc_ms": 1700000001000}
  ]
}
```

单点条目若附带 `counter_step`、`utc_step`、`count`，则表示以该
`counter`/`utc_ms` 为首点的整串（简写形式），等价于提交到
`/calibrations/series`。

### `POST /calibrations/series` — 按开机段提交等步长整串

```json
{
  "series": [
    {
      "boot_segment": "boot-1",
      "start_counter": 0,
      "start_utc_ms": 1700000000000,
      "counter_step": 1000,
      "utc_step": 1000,
      "count": 3600000
    }
  ]
}
```

串内第 `i` 点（`i = 0..count-1`）为
`counter = start_counter + i * counter_step`、
`utc_ms = start_utc_ms + i * utc_step`。
首点字段也接受 `counter` / `utc_ms` 命名，点数接受 `points` / `point_count`。
两个校时接口都可同批附带 `calibrations` 与 `series`，在同一个事务里合并生效。
整串校验失败或与已有单点合并后倒退返回 `422` / `409`，原校时资料与供数不受影响。

三个提交接口的响应都会列出**本次提交中时刻或状态发生变化的观测 ID**
（含新观测，以及被卷入/解除冲突的其他观测），格式一致：

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
  core.py   # 校时换算、批量写入、重算、冲突检测、变更快照
  db.py     # SQLite 连接、模式、事务与锁
tests/
  test_api.py
  test_series.py
Dockerfile
docker-compose.yml
```
