# skill-forge Mac 迁移与使用指南

> 适用场景：把 Windows 上开发好的 skill-forge 搬到自己的 Mac 上日常使用。
> 全部代码已按跨平台实现（pathlib / urlsplit / shutil.which("bash")），
> Mac 上无需改任何代码。

## 一、要拷贝什么（以及不要拷贝什么）

### ✅ 需要拷贝

```
skill-forge/
├── app/                  # 后端源码
├── migrations/           # Alembic 迁移脚本
├── tests/                # 测试（可选，日常使用不需要）
├── frontend/
│   ├── src/  tests/  index.html
│   ├── package.json  package-lock.json  vite.config.ts  tsconfig.json
│   └── dist/             # 已构建产物；Mac 上也可重新构建（见下）
├── launcher.py           # 一键启动器
├── alembic.ini
├── pyproject.toml
├── uv.lock               # Python 依赖版本与 PyPI 工件锁定，务必带上
├── config.yaml           # 配置（data_dir 等相对路径会自动锚定到项目根）
├── README.md
├── docs/                 # 内部技术说明 + 用户使用指南
├── S8-拼接说明.md
└── 发布-审计修复与Mac迁移/   # 本文件夹
```

### 🔁 可选拷贝（看你是否要带走已有数据）

```
data/                    # 你的项目、技能版本、评测记录（SQLite + 文件树）
```

> 想带走 Windows 上的数据 → 用备份命令导出（见第四节），Mac 上恢复，
> 比直接拷 `data/` 目录更稳（SQLite WAL 下直接复制文件不安全）。

### ❌ 不要拷贝

```
.venv/                   # Windows 的虚拟环境，Mac 无法使用
frontend/node_modules/   # 含平台相关的二进制（esbuild 等），Mac 上重装
frontend/dist/assets/*.js 中的 .map 缓存等无所谓，dist 本身可带可不带
__pycache__/  data-test/  # 临时产物
```

## 二、Mac 环境准备

### 1. 安装 uv（依赖管理 + Python）

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

项目要求 **Python ≥ 3.12**；`uv.lock` 锁定 Python 包版本与工件，不锁 Python 解释器版本。uv 可下载/管理符合项目要求的 Python，无需预先安装系统 Python。

### 2. 安装 Node.js（前端构建需要，运行不需要）

- dist 已构建好 → 运行时**不需要** Node；
- 想改前端代码或运行前端测试 → 装 Node ≥ 22.12（`brew install node`）；当前 Vitest 5 使用该版本下限。

### 3. 模型 API（S5 起执行/评测必需）

准备一个 OpenAI 兼容端点的密钥（OpenAI / DeepSeek / Moonshot / 本地
vLLM 等任何 `/v1/chat/completions` 兼容服务均可）。

### 4. 可选：PowerShell（只有 .ps1 脚本需要）

run_script 支持 `.py` / `.sh` / `.ps1`。Mac 上 `.py` 走 uv 管的 Python、
`.sh` 走系统 bash，开箱即用；只有技能里要跑 `.ps1` 才需要
`brew install powershell`。**日常可忽略。**

## 三、安装与启动

```bash
cd ~/skill-forge            # 你放代码的位置

# 1. 后端依赖（按 uv.lock）
uv sync --locked

# 2. 前端依赖（仅当你要改前端/重新构建时）
cd frontend && npm ci && npm run build && cd ..

# 3. 一键启动
uv run python launcher.py
```

启动器自动完成：端口预检 → 数据目录自检 → 数据库迁移（首次自动建库）→
生成会话令牌 → 启动 API + Worker 两个进程 → **自动打开浏览器**
（令牌经 URL 片段 `#token=…` 下发，不进请求与日志）→ `Ctrl+C` 一并退出。

> `webbrowser.open` 在 macOS 会调默认浏览器，无需任何改动。
> 打不开就手动访问终端里打印的 `http://127.0.0.1:8710/#token=…`。

### 模型配置（环境变量）

```bash
export SKILLFORGE_LLM_BASE_URL="https://api.deepseek.com/v1"   # 你的端点
export SKILLFORGE_LLM_MODEL="deepseek-chat"
export SKILLFORGE_LLM_API_KEY="sk-…"
uv run python launcher.py
```

密钥可在当前终端临时输入；若写进 `~/.zshrc` 会明文落盘，请自行选择。不配也能启动——项目/技能/版本管理全部可用，
只是「运行」和「对照评测」会被拒绝（错误码 `provider_not_configured`，
任务会立刻干净地失败，不会挂死）。

环境变量必须在启动器运行的同一个终端中设置；启动器会把它们传给 API 与 Worker。仅设置在另一个终端、IDE 或 Codex 会话里的变量不会自动传递。

可选裁判模型配置：`SKILLFORGE_JUDGE_LLM_BASE_URL`、`SKILLFORGE_JUDGE_LLM_MODEL`、`SKILLFORGE_JUDGE_LLM_API_KEY`。URL 和 MODEL 必须同时设置才启用；否则整体回落主模型。

### 常用环境变量一览

| 变量 | 默认 | 说明 |
|------|------|------|
| `SKILLFORGE_LLM_BASE_URL` / `_MODEL` / `_API_KEY` | 空 | 模型端点/模型名/密钥（应用不主动持久化密钥） |
| `SKILLFORGE_PORT` | 8710 | API 端口 |
| `SKILLFORGE_DATA_DIR` | `data`（锚定到项目根） | 数据目录（相对路径自动锚定，不受启动 cwd 影响） |
| `SKILLFORGE_CONFIG` | `config.yaml` | 配置文件路径 |
| `SKILLFORGE_LEASE_SECONDS` | 60 | Worker 任务租约 |
| `SKILLFORGE_RECOVER_SECONDS` | =租约 | stale 任务清扫周期（宕机任务会被标记 interrupted 而非永久 running） |
| `SKILLFORGE_SSE_MAX_SECONDS` | 3600 | SSE 流最长时长 |

## 四、数据迁移（Windows → Mac）

**在 Windows 上（服务停止时）导出：**

```powershell
uv run python -m app.backup create --out skillforge-backup.zip
```

**拷 `skillforge-backup.zip` 到 Mac，恢复：**

```bash
# 先确认 Mac 上的 data/ 是空的（恢复拒绝覆盖非空目录，防误毁）
uv run python -m app.backup restore skillforge-backup.zip --data-dir data

# 查看备份内容（只读）
uv run python -m app.backup info skillforge-backup.zip
```

备份走 SQLite backup API（WAL 一致快照），恢复检查包标识、路径合法性与数据库 quick_check。当前备份没有逐文件哈希或签名验证，不能宣称防篡改。备份和恢复前应停止 API 与 Worker。

## 五、日常使用速览

详细流程见 `docs/用户使用指南.md`，一句话版：

1. 建项目 → 写需求（brief）→ 确认；
2. 编辑技能草稿（SKILL.md + 附属文件，网页上直接写）→ 快照成
   **不可变版本**（四重校验 + 内容哈希）；
3. 「运行」：选版本 + 输入任务提示词 → 看 AI 装配技能后在沙箱里的
   完整轨迹（工具调用、用量、沙箱最终文件）；
4. 建测试集（用例 + 程序断言）→ 「对照评测」：候选版本 vs 无技能
   基线，逐例执行 + 断言评分 + 分组汇总，看技能到底带来了多少提升；
5. 阶段封存：备份 zip 归档，换机器按第四节恢复。

### 沙箱须知（重要，读一下）

沙箱是**路径层约束**：AI 的文件读写被限制在
`data/projects/{项目}/runs/{运行}/sandbox/` 内，脚本限 `.py/.sh/.ps1`、
60 秒超时、输出截断。但它**不是操作系统级隔离**——脚本以普通子进程
运行，理论上可访问网络。所以：只对你自己写的、或审查过的技能内容
点「运行」。

## 六、在 Mac 上验证安装（可选）

```bash
uv run python -m pytest -ra
cd frontend && npm ci
npm run typecheck
npx vitest run
npm run build
```

最近一次完整验证为后端 **273 passed、1 skipped**，前端 **5 passed**，类型检查和生产构建通过。跳过项是 `None` Origin 占位测试。用例可在 macOS 运行；含 `.sh` 脚本的技能 smoke 测试会使用系统 shell。

## 七、常见问题

| 现象 | 原因与解法 |
|------|-----------|
| 启动报端口占用 | 换 `SKILLFORGE_PORT=8318 uv run python launcher.py` |
| 「运行」立刻失败 `provider_not_configured` | 三个 `SKILLFORGE_LLM_*` 环境变量没配全 |
| 评测里某用例 `exec_failed` | 该用例执行出了意外异常（模型/网络抖动等），**不会拖垮其余用例**，重跑评测即可 |
| 退出原因显示「输出截断」 | finish_reason=length，调大运行的 token 预算再试 |
| 退出原因显示「预算耗尽」 | token/轮次双预算触顶，重跑时在 payload 里调大 `max_tokens_total` / `max_rounds` |
| 任务卡在 running | 不会发生了——Worker 周期清扫过期租约（interrupted/lease_expired），等一个租约周期即可 |
| 换了目录启动后数据"不见了" | 不会发生了——data_dir 相对路径锚定项目根；若还不对，检查 `SKILLFORGE_DATA_DIR` 是否被误设 |
| `npm ci` 报平台二进制错误 | node_modules 是 Windows 的，删掉重装：`rm -rf node_modules && npm ci` |
| `.ps1` 脚本跑不了 | `brew install powershell`，或把技能里的脚本改成 `.sh`/`.py` |

---

**版本对应**：本指南对应审计修复后的代码（F01–F10 全部修复，
测试 125 项全绿）。修复明细见同目录《审计修复报告-F01至F10.md》。
