# ADK 操作手册（中文）

怎么配置和使用 AI Actuary 的 **ADK Developer Web** 开发面、它是什么、边界在哪里。
[English](adk-operations-manual.md)

---

## 1. 这个面是什么

Google ADK（Agent Development Kit）Developer Web 是一个**仅用于开发**的控制面助手，与 Operator Console 并行运行：

- 它是一个聊天入口，用来查看回环控制面，并在明确确认后启动一个受限 workflow；
- 它调用的是与控制台相同的控制面 API，但工具集**更窄**；
- 它不写业务决策：review 决策、直接工具运行、旧版 replay/benchmark 端点都不在它的能力范围内。

| | Operator Console（8000） | ADK Developer Web（8001） |
| --- | --- | --- |
| 使用者 | 精算师 / 操作员 | 开发者 |
| 创建受治理 run | 可以 | 只能起两个已发布的 Chainladder workflow |
| Review 决策 | 可以 | **不可以** |
| 报告导出 | 可以 | 仅受限的 debug 工具 |
| 产物路径 / registry 内部 | 会展示 | 刻意隐藏 |

ADK agent 刻意不是第二个真相来源：数字仍然来自确定性核心，控制台才是正式操作面。

---

## 2. 前置条件与安装

- Python **3.11**（ADK Developer Web 固定要求 3.11）
- `google-adk==2.7.1`
- 解释器旁存在 `adk` 可执行文件（或在 `PATH` 上）

```bash
pip install -e '.[dev,adk-dev]'
python -c "import importlib.metadata as m; print(m.version('google-adk'))"   # 必须输出 2.7.1
```

缺少这些依赖时，启动器仍会启动 API/控制台，只是把 ADK 组件标记为不可用，不会悄悄降级操作面。

---

## 3. 启动、停止、端口

```bash
python scripts/run_local_workbench.py                 # API 8000 + ADK 8001
python scripts/run_local_workbench.py --disable-adk   # 只启控制台
python scripts/run_local_workbench.py --api-port 8123 --adk-port 8124
python scripts/run_local_workbench.py --no-adk-web    # 启 ADK 后端，但不自动开浏览器
```

- ADK Developer Web：`http://127.0.0.1:8001/`
- 应用端点：`/list-apps`、`/apps/ai_actuary_developer/app-info`、`/dev/apps/ai_actuary_developer/build_graph`
- ADK 状态根目录：`tmp/adk_work`（按会话分目录）
- 启动器诊断日志：`tmp/local-workbench-diagnostics/launcher.jsonl`

冷启动约 30–40 秒。启动器拥有两个进程（Windows 上用 Job Object），关掉启动器即停止子进程；手动重启前先查
`Get-NetTCPConnection -State Listen -LocalPort 8000,8001`。

---

## 4. 配置

| 变量 | 含义 | 默认值 | 是否需重启 |
| --- | --- | --- | --- |
| `AI_ACTUARY_ADK_MODEL` | ADK 聊天 agent 使用的模型 | `gpt-5.6-luna` | 需要（import 时读取） |
| `AI_ACTUARY_ADK_CREDENTIAL` | ADK 执行客户端的能力凭证 | 启动器生成 | 需要 |
| `AI_ACTUARY_ADK_URL` | ADK Developer Web 的回环地址 | `http://127.0.0.1:8001` | 需要 |
| `DEEPSEEK_API_KEY` | 模型 id 以 `deepseek/` 开头时必需（走 litellm 路由） | — | 需要 |
| `GOOGLE_API_KEY` | 使用 Gemini 模型 id 时必需 | — | 需要 |
| `OPENAI_API_KEY` | 使用 OpenAI 兼容模型 id 时必需 | — | 需要 |
| `AI_ACTUARY_BROWSER_SMOKE_RUNNER=1` | 把 agent 换成无模型的确定性协议 agent（浏览器冒烟用） | 未设置 | 需要 |

模型 id 示例：

```bash
AI_ACTUARY_ADK_MODEL=gpt-5.6-luna                 # OpenAI 兼容
AI_ACTUARY_ADK_MODEL=gemini-2.5-flash             # 需要 GOOGLE_API_KEY
AI_ACTUARY_ADK_MODEL=deepseek/deepseek-flash       # 走 litellm 路由，需要 DEEPSEEK_API_KEY
```

这是与运行时角色**相互独立的模型位**（见[操作手册](operations-manual.zh-CN.md)）：ADK 聊天模型不会碰到任何 run 结果，而 `AI_ACTUARY_PLANNER_MODEL`（规划）与 `AI_ACTUARY_REVIEW_MODEL`（审阅）才会。

---

## 5. agent 能调用什么

**只读工具（12 个）**

`get_health`、`get_preflight`、`list_tools`、`get_tool`、`list_workflows`、
`get_workflow`、`list_runs`、`get_run`、`get_run_events`、`get_run_artifacts`、
`get_run_review_snapshot`、`get_artifact_projection`

**执行工具（4 个）**

`start_workflow_run`、`wait_run`、`get_run_status`、`summarize_run`

**调试工具（7 个）**

`rerun_run`、`replay_run`、`compare_repeatability`、`run_bounded_benchmark`、
`export_run_report`、`get_debug_operation_status`、`wait_debug_operation`

执行与调试工具只对**受信任的 run ID**生效，并且局限在 `adk-development` 工作区内。

---

## 6. 执行规则

- 只允许启动两个已批准的 workflow：`chainladder-basic` 与 `chainladder-validated`（`ALLOWED_ADK_WORKFLOWS`）。
- 每次启动都必须经过 **ADK 工具确认**，agent 不得擅自调用。
- 每次确认后的启动都带 **idempotency key**，重试不会产生重复 run。
- run 在隔离的 `adk-development` 工作区内执行。
- 轮询超时**不等于取消**：agent 不得声称业务 run 被取消。

**明确禁止**（由 agent 指令强制）：直接工具运行、旧版基于路径的 replay/benchmark/repeatability/report API、提交 review 决策，以及泄露文件路径、registry 内部、产物根目录、凭证、密钥或原始异常。

---

## 7. 常见用法示例

```text
控制面健康吗？
列出已发布的 workflow，并展示 Chainladder workflow。
为 case adk-smoke-001 启动 chainladder-basic 并等待结果。
总结 run <run_id> 并列出它的产物。
复算 run <run_id>，并与 <run_id_b> 比较可重复性。
```

agent 只能依据工具证据回答。若请求超出批准范围，它应当直接说明，而不是自行变通。

---

## 8. 无模型冒烟模式

设置 `AI_ACTUARY_BROWSER_SMOKE_RUNNER=1` 后，模型驱动的 agent 会被替换为 `BrowserSmokeAgent`：一个确定性 ADK 协议 agent，不调用任何模型即可发出确认事件与 workflow 调用。没有任何模型凭证时，用它跑浏览器冒烟测试。

---

## 9. 排障

| 现象 | 检查 |
| --- | --- |
| ADK 组件显示不可用 | Python 3.11 上安装了 `google-adk==2.7.1`，且存在 `adk` 可执行文件 |
| `incompatible_adk` | 用 `pip install -e '.[dev,adk-dev]'` 重新安装 |
| 端口冲突 | 启动器会拒绝被占用的端口；用 `--adk-port` 换端口 |
| agent 没有可用模型 | 按 `AI_ACTUARY_ADK_MODEL` 设置对应的 API key |
| 需要留存证据 | 查看 `tmp/local-workbench-diagnostics/launcher.jsonl`、`tmp/adk_work/state/...` |

---

## 相关文档

- [操作手册（中文）](operations-manual.zh-CN.md) — 运行时流程、review、AI reviewer 配置
- [ADK 本地工作台](adk-local-workbench.md) — 现行本地运行手册、打包与回滚
- [ADK Workflow Lab](adk-workflow-lab.md) — 受限 workflow 执行面
- [架构说明](architecture.md) · [控制面契约](contracts/control-plane.md)
