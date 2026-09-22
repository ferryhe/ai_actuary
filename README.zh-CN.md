# AI Actuary（中文说明）

[English](README.md) · 中文 · [中文操作手册](docs/operations-manual.zh-CN.md) · [中文 ADK 手册](docs/adk-operations-manual.zh-CN.md)

AI Actuary 是一个本地运行的 **智能精算工作台（Agentic Actuarial Workbench）** 原型。它的运行原则只有一句：

**智能体负责规划与解释，精算工具负责算数，人类精算师负责决策，产物负责审计与复算。**

详细流程、每一步交接、review 与 AI review 怎么产生，见 [中文操作手册](docs/operations-manual.zh-CN.md)；ADK 开发面见 [中文 ADK 手册](docs/adk-operations-manual.zh-CN.md)。

---

## 项目定位：四个角色

| 角色 | 组件 | 是否调用模型 | 负责什么 |
| --- | --- | --- | --- |
| **编排器 Orchestrator**（planner） | `workflows/agent-runtimes/openai-agents/`（OpenAI Agents SDK） | 会 | 选择路由、产出受限的执行计划 `AgentExecutionPlan`、调用 worker 边界、汇总受治理的最终输出 |
| **执行器 Executor**（Hermes worker） | `workflows/agent-runtimes/hermes-worker/` | 有一个叙述模型位，**默认开启**，只改措辞 | 执行精算工具链、打包产物、生成 review packet、提供 replay / batch 边界 |
| **审阅器 Reviewer**（AI review） | `src/reserving_workflow/review/ai_reviewer.py` | 会，且**只在 run 处于 `needs_review` 时** | 只读确定性 review packet，提示人类该关注什么 |
| **人类精算师** | Operator Console | — | 批准 / 拒绝 / 要求修改，拥有最终决策权 |

边界是稳定的：**数字真相来自确定性核心，规划来自编排器，执行与打包来自执行器，决策属于人类。** AI reviewer 只给建议，永远不能改动任何数字。

---

## 模型分工：planner 与 executor 独立配置

| 模型位 | 环境变量 | `.env.sample` 默认值 | 何时读取 |
| --- | --- | --- | --- |
| 编排器 planner | `AI_ACTUARY_PLANNER_MODEL`（配合 `OPENAI_API_KEY`，可选 `OPENAI_BASE_URL`） | `gpt-5.6-luna` | 模块 import 时 → **改后必须重启** |
| 执行器 executor | `AI_ACTUARY_NARRATIVE_ENABLED`（`1` 开 / `0` 仅模板）+ `AI_ACTUARY_NARRATIVE_MODEL`、`AI_ACTUARY_NARRATIVE_BASE_URL`、`AI_ACTUARY_NARRATIVE_API_KEY`——**只改措辞** | `1`（默认开启；`0` 回到确定性模板） | 每次调用时 → **改 `.env` 不用重启** |
| 审阅器 reviewer | `AI_ACTUARY_REVIEW_MODEL`、`AI_ACTUARY_REVIEW_BASE_URL`、`AI_ACTUARY_REVIEW_API_KEY` | `deepseek-flash`，端点 `https://api.deepseek.com/v1` | 每次调用时 → **改 `.env` 不用重启** |
| ADK 开发面聊天 | `AI_ACTUARY_ADK_MODEL`（`deepseek/` 前缀走 litellm） | `gemini-2.5-flash` | agent import 时 |

reviewer 只要求是 OpenAI 兼容端点，因此**规划与审阅可以同时使用不同厂商、不同模型**（例如 planner 用 `gpt-5.6-luna`、reviewer 用 `deepseek-flash`）。关键区别：planner 的变量在进程启动时固化，reviewer 的变量每次调用实时读取。

首次运行前要知道两件事：

- **审阅槽**：`.env.sample` 把它指向 `deepseek-flash`，因此需要 `DEEPSEEK_API_KEY`。源码默认值是 `gpt-5.6-luna`；只有当某个槽的模型 id 或端点包含 `deepseek` 时才会回退到 `DEEPSEEK_API_KEY`，否则使用 `OPENAI_API_KEY`。
- **叙述槽**：默认开启，即每次 run 都会调用模型改写措辞。想要完全确定性，设 `AI_ACTUARY_NARRATIVE_ENABLED=0`（或 CLI `--narrative-model off`），详见下文「叙述生成」小节。

---

## 当前能力

- 基于 `chainladder-python` 的确定性准备金计算
- 受治理的单案例执行（planner 与 Hermes worker 边界清晰）
- **两条独立计算链路**：准备金（Chainladder）与实际/预期研究（Experience study）
- 本地 JSON run registry、rerun、replay、repeatability
- FastAPI 控制面：runs / tools / workflows / reviews / artifacts / replay / report export
- `/console` 轻量操作台：建 run、轮询事件、查看产物与 review、提交决策、rerun、导出报告
- 工具目录内置 `chainladder` 与 `minimax_experience_study_tool`
- 独立 review 契约与 review 决策产物
- **experience study 走同一套 constitution 引擎**，可按诊断指标阈值触发评审
- **可配置 AI review**：`ai_review.json` / `ai_review.md`，控制台以 "AI guidance" 渲染
- 跨仓库 `actuarial-reserving.v1` schema / fixture 兼容包

仍不在范围内：生产级队列与流式编排、认证 / SSO / 多租户、对象存储或数据库审计、生产前端构建、其余经验研究模型及其对比报告。

---

## 安装

要求 Python 3.11+：

```bash
git clone git@github.com:ferryhe/ai_actuary.git
cd ai_actuary
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

把 `.env.sample` 复制为 `.env` 并填入需要的密钥（不要提交 `.env`）：

```powershell
Copy-Item .env.sample .env
```

只用 API 的话 `pip install -e '.[api]'` 即可；`dev` 额外包含 pytest 等开发依赖。

---

## 快速开始

```bash
python scripts/run_local_workbench.py
```

- Operator Console / API：`http://127.0.0.1:8000/console`
- ADK Developer Web（可选）：`http://127.0.0.1:8001`

控制台首次打开是锁定状态：点 **Request launcher handoff**，把页面显示的 handoff ID 粘贴回启动器终端即可解锁（该 ID 不是凭证）。

handoff 流程在整个进程生命周期内都可用，因此 Operator 会话过期后可以重新发起解锁，无需重启工作台；只有直接 token 交换（`/auth/operator/bootstrap`）是一次性的。轮换 operator-console 凭据会关闭整条 bootstrap 通道（直接交换与 handoff 铸造都不可用）直到进程结束：轮换时传入新的 bootstrap token 可重新武装，否则需重启工作台。该撤销仅存在于内存，不能替代更换落地的 token。

健康检查：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/health/preflight
```

---

## 两条独立计算链路

| | 链路 A：准备金（Chainladder） | 链路 B：实际/预期研究（Experience study） |
| --- | --- | --- |
| 入口 | `POST /runs` with `tool_id=chainladder`，或 `scripts/run_governed_case.py` | `POST /runs` with `tool_id=minimax_experience_study_tool` |
| 是否经过 planner | 是（OpenAI planner 决定路由） | 否（直接执行已注册的模型工具） |
| 计算 | `chainladder` 三角 → 进展因子 → IBNR | 分组 actual / expected（含 Poisson 精确区间与可信度标记） |
| 产出 | `deterministic_result.json`（IBNR、诊断指标） | `deterministic_result.json`（8 条分组结果 + diagnostics） |
| 评审触发 | `origin_count` 阈值 | 诊断阈值（零分母 / 低可信度 / A-E 上限），可覆盖 |

两条链路都写同一套产物契约，都交给同一个 constitution 引擎判定，因此评审与导出的行为一致。

---

## Review 与 AI review（简述）

```text
pass            -> completed
review_required -> needs_review（生成 review packet，可选 AI review）
fail            -> failed（硬性约束被破坏）
```

- **触发是确定性的**：`diagnostics[metric] > threshold`，或必需产物缺失。
- **reviewer 是怎么来的**：run 进入 `needs_review` 后，执行器写出 `review_packet.json` / `review_packet.md`；随后独立的 reviewer 模型被调起，写出 `ai_review.json` / `ai_review.md`，并把 `ai_review` 与 `ai_suggestion` 回写进 packet，控制台显示为 **AI guidance**。
- **失败不阻断**：模型调用失败只记录 `status=failed` 与错误信息，run 状态与数字都不受影响。

### 叙述生成（第四个模型位，默认开启）

默认就是 `AI_ACTUARY_NARRATIVE_ENABLED=1`：每次 run 都会让模型改写 `narrative_draft.json` 的**措辞**。数字仍然是确定性的：`cited_values` 从确定性结果拷贝，生成文本里的每个数字都必须能在 run 证据中找到，否则静默回退到模板。想要完全不调模型，设为 `0`（或 CLI 用 `--narrative-model off`）。详见[操作手册第 8 节](docs/operations-manual.zh-CN.md#8-可选模型位执行器侧叙述生成)。

---

## 关键 API 路由

```text
GET  /health                      GET  /console/state
GET  /console                     GET  /tools            GET  /workflows
POST /runs                        GET  /runs             GET  /runs/{run_id}
GET  /runs/{run_id}/events        POST /runs/{run_id}/rerun
GET  /runs/{run_id}/artifacts     GET  /runs/{run_id}/results
GET  /runs/{run_id}/review-packet GET  /runs/{run_id}/review
POST /runs/{run_id}/report-export
GET  /reviews                     POST /reviews/{review_id}/decision
POST /replay                      POST /repeatability     POST /benchmarks/batch
```

run 状态：`accepted, queued, running, completed, needs_review, failed`
review 决策：`approved, rejected, changes_requested`

---

## 常用 CLI

```bash
# 1) 受治理单案例
python scripts/run_governed_case.py --case-id demo-case --artifact-dir ./tmp/demo-case --registry-path ./tmp/run-registry.json

# 2) 强制触发评审（origin_count 阈值）
python scripts/run_governed_case.py --case-id review-case --artifact-dir ./tmp/review-case --registry-path ./tmp/run-registry.json --review-threshold-origin-count 5

# 3) 列表 / 查看 / 复跑
python scripts/list_runs.py --registry-path ./tmp/run-registry.json
python scripts/show_run.py --registry-path ./tmp/run-registry.json --run-id <run_id>
python scripts/rerun_case.py --registry-path ./tmp/run-registry.json --run-id <run_id> --artifact-dir ./tmp/rerun-case

# 4) 导出交接报告
python scripts/export_run_report.py --registry-path ./tmp/run-registry.json --run-id <run_id> --review-store-dir ./tmp/reviews

# 5) 复算与可重复性
python scripts/replay_case.py --manifest-path ./tmp/demo-case/run_manifest.json
python scripts/compare_repeatability.py --manifest-path ./tmp/repeat-a/run_manifest.json --manifest-path ./tmp/repeat-b/run_manifest.json
```

---

## 产物模型

```text
case_input.json / validated_input.json
deterministic_result.json
narrative_draft.json
constitution_check.json
review_packet.json + review_packet.md   # 需要评审时
ai_review.json    + ai_review.md        # AI review 生成时
review_decision.json + review_decision.md
operator_handoff.md / reserve_summary.json / reserve_summary.md
run_manifest.json
```

先看 `run_manifest.json`（run 级索引）。registry 只是运行索引，产物才是审计证据。

---

## 开发与验证

```bash
python -m pytest tests -q
python -m pytest tests/test_control_plane_capabilities.py -q
```

改了控制台（HTML/JS）后必须真正起服务在浏览器里点一遍，`TestClient` 无法覆盖 UI 回归。

---

## 人与系统的职责

**人类精算师**：选定案例、目标与可接受假设；审阅确定性输出与治理包；提交批准/拒绝/修改意见；对业务使用签字。

**智能体/系统**：把请求转成受限的工具或工作流计划；只调用公开 API/CLI 而不直接改内部文件；运行确定性工具与治理检查；写产物与 manifest；基于证据总结而不编造缺失事实。

---

## 文档索引

- [中文操作手册](docs/operations-manual.zh-CN.md) — 端到端流程、配置、两条计算链路、review 与 AI review
- [Operations Manual (EN)](docs/operations-manual.md)
- [中文 ADK 手册](docs/adk-operations-manual.zh-CN.md)
- [ADK Operations Manual (EN)](docs/adk-operations-manual.md)
- [架构说明](docs/architecture.md)、[控制面契约](docs/contracts/control-plane.md)、[交接报告契约](docs/operator_handoff.md)
- [ADK 本地工作台](docs/adk-local-workbench.md)、[ADK Workflow Lab](docs/adk-workflow-lab.md)
