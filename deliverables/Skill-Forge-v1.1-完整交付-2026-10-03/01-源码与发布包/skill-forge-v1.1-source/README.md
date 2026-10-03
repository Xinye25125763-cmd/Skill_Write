# skill-forge v1.1.0

个人版技能生产与评测工作台。当前进度：**S1 脚手架 / S2 项目与需求 / S3 技能草稿与不可变版本 / S4 任务队列与 Worker + SSE / S5 模型适配层 + API 执行器 v0 / S6 测试集与对照评测 / S7 阶段封存（备份/恢复 + 依赖锁定）** 已交付。

**范围说明**：以上是设计文档**第一阶段**的实现，不是完整个人版——原生运行器、盲评、分析者、改进轮、触发优化、安装器等第二阶段能力尚未实现（相关接口已预留）。API 执行器为 v0 子集，沙箱为路径层约束而非操作系统级隔离（详见下文工具层说明）。

## 运行

```powershell
uv sync                      # 安装依赖（首次）
uv run python launcher.py    # 一键启动：迁移 → 自检 → API + Worker → 浏览器
```

启动器流程：端口预检 → 数据目录自检 → 数据库迁移 → 删除暂存区恢复/清理 → 生成会话令牌 →
启动 uvicorn API 与 Worker（两个独立进程，令牌经环境变量注入）→ 打开浏览器
（令牌经 URL 片段下发，不入请求与日志）→ Ctrl+C 退出时同时终止两个子进程。

## 测试

```powershell
uv run pytest                # 全量测试（数量见 CI 输出，当前 135）
```

覆盖：健康探活、令牌鉴权、Origin 边界、配置覆盖、数据目录自检、迁移往返重放、
端口占用预检（S1）；项目 CRUD、需求修订与字段来源标注、确认流（S2）；
草稿读写、路径/扩展名/大小校验、四重版本校验、内容哈希冲突、版本不可变与
篡改检测（S3）；任务入队去重、租约领取与心跳续租、协作取消、进程被杀后
租约过期恢复、结构化错误、SSE 流（S4）；工具层沙箱穿越拒绝与脚本白名单、
完整工具循环（含脚本执行）、预算耗尽（token/轮次）、工具超时、模型失败、
协作取消五种退出原因、OpenAI 兼容协议序列化、agent_run 全链路（S5）；
断言评分全类型与结构错误分离、套件 CRUD 与服务端校验、对照评测端到端
（候选 vs 基线系统提示词差异验证）、执行失败与评分失败分离（含按组汇总
统计）（S6）；备份→清空→恢复→数据一致、非空目标拒绝覆盖、zip-slip 与
坏包拒绝、CLI 退出码（S7）。

## 模型适配层与 API 执行器（S5）

- 模型适配层（`app/llm/`）：能力声明（tools/streaming/json_mode/context）+
  OpenAI 兼容 HTTP 适配器，适用于一切 `/v1/chat/completions` 兼容端点。
  密钥从 `SKILLFORGE_LLM_API_KEY` 读取，应用不主动持久化。
- 工具层（`app/tools/`）：read_file / write_file / run_script 三工具，
  复用 S3 路径穿越校验 + `Path.is_relative_to` 层级检查限制在沙箱目录内；
  脚本限 .py/.sh/.ps1，60 秒超时，输出截断；错误以 JSON 回填模型上下文，
  不打断循环。注意：这是**路径层面**的约束，脚本本体运行在普通子进程中，
  并非操作系统级沙箱（无进程/网络隔离）。
- API 执行器（`app/executors/`）：自实现工具循环，token/轮次双预算，
  六种退出原因（completed / truncated / budget_exceeded / tool_timeout /
  model_failed / cancelled）均落库可区分。
- 运行接口：`POST /api/projects/{id}/runs`（202 异步，version_id 省略即
  无技能基线）→ Worker 展开版本快照进沙箱 → 工具循环 →
  `GET /api/runs/{id}` 返回轨迹、用量与沙箱最终文件。

## 测试集与对照评测（S6）

- 测试集（Suite）：用例 = id + prompt + 程序断言数组；断言四种类型：
  `file_exists` / `file_contains` / `text_contains` / `exit_reason_is`。
  `POST /api/projects/{id}/suites` 同步创建并服务端校验（未知断言类型、
  用例 ID 重复、必填字段缺失 → 422 带逐条错误）。
- 对照评测：`POST /api/projects/{id}/eval-runs`（202 + job_id）→ Worker 对
  每个用例执行两遍：候选（展开指定版本快照进沙箱）vs 基线（无技能系统
  提示词、空沙箱）→ `GET /api/eval-runs/{id}` 返回逐例结果与分组汇总。
- 执行失败与评分失败严格分离：单用例 `exit_reason != completed` →
  `exec_status=exec_failed` 且不评分（`not_scored`）；评分步骤自身失败
  （断言结构非法）→ `score_status=score_failed` 带 `score_error`，不影响
  执行状态。断言不通过是正常结果（passed=false），不是错误。
- 协作取消：未执行的用例记 skipped，已执行的保留结果。

## 备份与恢复（S7）

备份/恢复在服务停止时执行（WAL 数据库需释放句柄）。CLI：

```powershell
uv run python -m app.backup create --out backup.zip     # 导出数据目录快照
uv run python -m app.backup info backup.zip             # 只读查看备份信息
uv run python -m app.backup restore backup.zip --data-dir data  # 恢复到空目录
```

语义与安全要点：

- 数据库经 SQLite backup API 取一致快照（WAL 下直接复制文件不安全），
  `projects/` 下所有数据文件一并归档；备份包内含 manifest（schema 版本、时间）。
- 恢复目标是**空目录**：非空目录拒绝覆盖（不静默销毁既有数据）；
  zip 路径全部校验（zip-slip 防护）；恢复前对库跑 `PRAGMA quick_check`。
- 备份记录创建时的 alembic schema 版本；旧备份恢复到新版本后执行
  `uv run alembic upgrade head` 即可升级到当前 schema。

## 依赖锁定（S7）

- Python 依赖锁定于 `uv.lock`（`uv sync --locked` 可精确复现环境，
  `uv lock --check` 验证一致性）。
- 前端依赖锁定于 `frontend/package-lock.json`（`npm ci` 按锁定安装）。
- 模型提供商密钥不入库不入包：仅从环境变量
  `SKILLFORGE_LLM_API_KEY` / `SKILLFORGE_LLM_BASE_URL` / `SKILLFORGE_LLM_MODEL` 读取。

## 前端

```powershell
cd frontend
npm install
npm run build     # 产物 frontend/dist，由后端静态托管
npm run dev       # 开发模式，/api 代理到 127.0.0.1:8710
```

版本创建与运行提交均为异步任务：前端提交后轮询 `/api/jobs/{id}` 直到终态；
运行面板展示退出原因、逐步轨迹（含工具参数/输出/耗时/tokens）与沙箱文件；
评测面板提供测试集创建（JSON 表单 + 服务端校验错误回显）、对照评测发起与
历史，结果页按“候选/基线”分组汇总（断言通过数、执行失败数、评分失败数），
逐例表格中执行状态与评分状态分列展示。

## 任务队列与 Worker（S4）

- 版本创建走 `POST /projects/{id}/skills/versions` → `202 {job_id}`，由 Worker
  进程异步执行；终端状态（succeeded/failed/cancelled/interrupted）不可变。
- 租约 + 心跳：Worker 以条件 UPDATE 领取任务，独立线程以独立数据库会话持续续租，并检查 worker 归属；
  Worker 被杀后，租约过期的 running 任务由下一次启动恢复为 interrupted。
- 协作取消：`POST /jobs/{id}/cancel` 置位 cancel_requested，处理器在边界检查后
  以 cancelled 终态退出。
- 幂等去重：`(type, idempotency_key)` 唯一，重复提交返回同一 job。
- SSE：`GET /api/events/stream?token=…`（EventSource 无法带自定义头，令牌走
  查询参数），支持 Last-Event-ID 断线续传；`SKILLFORGE_SSE_MAX_SECONDS`
  控制单连接最大存活（默认 3600 秒）。

## 目录约定

- `data/`：数据目录（数据库、版本快照与运行产物），**源码更新不得覆盖**
- `app/`：后端（api / core / models / services / llm / tools / executors / workers）
- `app/llm/`：模型适配层（能力声明 + OpenAI 兼容提供商）
- `app/tools/`：工具注册表与沙箱路径解析
- `app/executors/`：执行器接口、退出原因枚举与 API 执行器 v0
- `app/services/scoring.py`：程序断言评分（断言不通过 ≠ 评分失败）
- `app/services/backup.py` + `app/backup.py`：备份导出/恢复服务与 CLI（S7）
- `app/workers/handlers/`：任务处理器注册表，新增任务类型只需在此注册
- `migrations/`：Alembic 迁移，手写交付（0001–0007）
- `frontend/`：React 19 + TS + Vite

## 配置

`config.yaml`：`server.host`（默认 127.0.0.1，勿改为 0.0.0.0）、`server.port`、
`data_dir`。环境变量 `SKILLFORGE_DATA_DIR` / `SKILLFORGE_PORT` / `SKILLFORGE_CONFIG`
可覆盖；`SKILLFORGE_TOKEN`（会话令牌）、`SKILLFORGE_LEASE_SECONDS`（任务租约，
默认 60）、`SKILLFORGE_POLL_SECONDS`（Worker 空闲轮询，默认 0.5）、
`SKILLFORGE_SSE_MAX_SECONDS`（SSE 连接最大存活）供测试与调优。

模型提供商环境变量（S5，运行 agent 任务前须设置）：

- `SKILLFORGE_LLM_BASE_URL`：OpenAI 兼容端点，如 `https://api.deepseek.com/v1`
- `SKILLFORGE_LLM_MODEL`：模型名，如 `deepseek-chat`
- `SKILLFORGE_LLM_API_KEY`：API 密钥（从环境变量读取，应用不主动持久化）

## v1.1 本次交付

保持现有功能，补上并发删除保护、文件恢复对账、独立心跳、终态条件更新、预算/取消工具拦截和预算详情。前端dist已重新构建。

- [修改与验证报告](docs/v1.1修改与验证报告.md)
- [前置skill适配评估](docs/前置skill适配评估.md)

可作为创建skill的需求与验收准备工具；自动生成、宿主原生触发验证、盲评、自动迭代和安装仍是待实现能力，不能从平台名称已登记推导为原生平台适配已完成。
