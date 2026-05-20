# AI Actuary 精算工作台与中间 Tool 平台开发计划（v3：合并 PR 执行版）

> **For Hermes:** Use `subagent-driven-development` or Codex workers to implement this plan PR-by-PR. Each PR must start from latest `origin/main`, keep one clear scope, pass focused tests + full suite, and only then push/create PR. This plan has incorporated the Codex architecture review feedback: freeze control-plane contracts before layering workflow/review/workspace/agent adapter semantics.

**Goal:** 将 `ai_actuary` 从“可运行 chainladder 的本地 operator prototype”演进为“面向每位精算师的 Web Console + 可扩展精算中间 Tool 平台”。

**Architecture:** 系统保持 API-first / text-contract-first：精算工具通过统一 Tool Registry 接入，任务通过 Run Control Plane 发起，结果落 Artifact/Review/Replay 合同，Agent 只做规划、解释、检查和路由，不拥有精算真值。v3 的核心修正是先冻结 `Run / Event / ArtifactRef / Review / Rerun / Store` 合同，再把后续路线压缩成 PR8-PR15 八个可执行产品切片。

**Tech Stack:** FastAPI, Pydantic/JSON Schema, local JSON registry first, filesystem artifact store first, static operator console first, optional OpenAI Agents / Hermes / Codex runtime adapters later.

---

## 0. v3 修订摘要

### 摘要

Codex 审核结论是 `approve_with_changes`：路线方向正确，但原计划需要先补关键控制面合同，避免把当前轻量 control plane 演进成语义混杂的半成品 Symphony。

### 来源

本版合并了以下判断：

- `Tool-first, not chat-first` 的方向保持不变；
- `Deterministic truth outside LLM` 的边界保持不变；
- PR8 继续先做 Tool Registry；
- PR8 之后新增一个小的 contract freeze PR；
- `tool_id` 与 `method_variant` 必须拆开；
- Review 不应修改 Run terminal status；
- Workflow contract 与 workflow execution 必须拆开；
- Store interface 应前移到 workflow/review 之前；
- API 当前只承诺 `inline` 与 `local_background`，Hermes worker/cron 是后续 adapter，不在本阶段伪实现。

### 结论

更新后的执行主线压缩为八个产品切片：

```text
PR8: Foundation Contracts and Tool Catalog
PR9: Tool-backed Run Dispatch and Input Contracts
PR10: Store Boundary and Local Adapters
PR11: Workflow Templates and Sequential Execution
PR12: Review Contract and Console Review Inbox
PR13: Per-actuary Workspace and Ownership
PR14: Agent Planner / Hermes Worker Adapter
PR15: Report Export and Operator Handoff
```

压缩原则：保留 Codex 提醒的边界，但把强相关 contract/API/UI 工作合成更像产品能力的 PR，减少纯架构 PR 数量。

---

## 1. 产品定位

### 摘要

`ai_actuary` 应定位为 **Agentic Actuarial Workbench**：精算师通过 Web Console 发起任务、检查任务、复核结果；Agent 和工具在后台围绕同一套 `case / run / event / artifact / review` contract 协作。

### 来源

用户目标是：未来开发更多精算中间 tool，覆盖精算流程，并给每个精算师一个 Web Console 来发起任务和检查任务。

### 结论

系统边界必须保持：

```text
Agent = coordinator / narrator / reviewer assistant
Actuarial Tool = calculation and diagnostic authority
Human Actuary = decision and sign-off authority
Artifact = audit and replay authority
```

不要把项目做成通用聊天机器人，也不要把 OpenAI Agents SDK / Hermes / Symphony 任一方变成业务真值源。

---

## 2. 当前基础状态

当前 `ai_actuary` 已具备 Stage 2.5 基础：

- 单机 FastAPI control plane；
- `/runs`、`/runs/{run_id}`、`/runs/{run_id}/events`、`/runs/{run_id}/rerun`；
- `/console` 与 `/console/state`；
- background run lifecycle polling；
- chainladder 作为当前 deterministic actuarial calculation path；
- artifact / replay / repeatability / batch benchmark 基础。

当前系统仍应描述为：

```text
local single-machine operator control-plane prototype
```

不是：

```text
production queue
multi-user SaaS
enterprise governance platform
full Symphony clone
```

---

## 3. 架构原则

1. **Tool-first, not chat-first：** 每一个业务能力都应沉淀为可注册、可验证、可审计的 actuarial tool，而不是隐藏在 prompt 中。

2. **Control-plane contracts first：** 在 workflow、review、workspace、agent adapter 继续扩展前，必须先冻结 `Run / RunEvent / ArtifactRef / Review / ReviewDecision / Rerun` 的语义。

3. **Run status 不等于 Review status：** Run terminal status 只表达执行结果；human approval / rejection / changes requested 是独立 Review 状态和 artifact。

4. **Tool identity 不等于 method variant：** `tool_id` 是稳定审计身份；`method_variant` 是 tool input 内部参数。旧 `method` 只作为 legacy alias。

5. **Run Control Plane 不复制业务逻辑：** FastAPI route 只做 transport/control-plane wrapper，业务执行仍在 operator/tool/workflow 层。

6. **Text-contract-first：** Tool metadata、input schema、output schema、workflow template、artifact manifest、review packet 都优先使用 JSON/YAML/Markdown，可被人和 agent 同时读取。

7. **Deterministic truth outside LLM：** 准备金计算、诊断指标、方法比较必须来自 deterministic tools 或显式模型，不由 LLM 直接捏造。

8. **Console is operator surface：** Web Console 是发起、观察、复核、重跑任务的操作面，不是先做复杂 BI dashboard。

9. **Execution mode 明确分层：** 当前阶段只支持 `inline` 和 `local_background`。Hermes worker、cron、external queue 属于后续 adapter，不在本阶段伪实现生产队列。

10. **One PR, one product slice：** 每个 PR 只推进一个可验收范围，不混入 opportunistic cleanup。

---

## 4. 目标用户流

### 4.1 Actuary 发起任务

```text
1. 精算师打开自己的 Web Console
2. 选择 case / tool / workflow
3. 填入必要参数或上传/选择输入数据
4. 点击 Create Run
5. Console 显示 run.accepted / run.running / tool events / terminal status
6. 精算师查看 artifact、review packet、diagnostics
7. 如有需要，点击 rerun 或 submit review decision
8. 若 review 要求重算，系统创建 follow-up run，而不是覆盖原 run
```

### 4.2 Agent 发起任务

```text
1. Agent 接收自然语言请求
2. Planner 将请求转成 machine-readable task/workflow plan
3. Agent 调用 ai_actuary API 创建 run
4. Agent 轮询 events
5. Agent 读取 artifacts/review packet
6. Agent 向精算师总结“计算结果、检查项、需要人工判断的点”
```

Agent 不直接写 case truth，不直接修改 review judgement，不把 prompt memory 当审计证据。

---

## 5. 核心对象模型

### 5.1 Tool

```json
{
  "tool_id": "chainladder",
  "name": "Chainladder Reserving",
  "version": "1.0.0",
  "category": "reserving_method",
  "description": "Deterministic reserving method backed by chainladder-python.",
  "input_schema_ref": "schemas/tools/chainladder.input.schema.json",
  "output_schema_ref": "schemas/tools/chainladder.output.schema.json",
  "capabilities": ["reserve_estimation", "triangle_projection"],
  "deterministic": true,
  "review_policy": "standard_reserving_review"
}
```

### 5.2 Tool input：拆分 `tool_id` 与 `method_variant`

正确方向：

```json
{
  "case_id": "2026Q1-auto-liability",
  "tool_id": "chainladder",
  "inputs": {
    "sample_name": "RAA",
    "method_variant": "chainladder",
    "review_threshold_origin_count": 5
  }
}
```

兼容层可以短期接受：

```json
{
  "case_id": "2026Q1-auto-liability",
  "method": "chainladder"
}
```

但内部应归一化为：

```json
{
  "tool_id": "chainladder",
  "inputs": {
    "method_variant": "chainladder"
  },
  "legacy_method_alias": "chainladder"
}
```

### 5.3 Run

Run 只表达一次执行。

```json
{
  "run_id": "api-2026Q1-auto-liability-20260506T050000Z",
  "case_id": "2026Q1-auto-liability",
  "tool_id": "chainladder",
  "execution_mode": "local_background",
  "status": "needs_review",
  "created_by": "actuary_001",
  "created_at": "2026-05-06T05:00:00Z",
  "updated_at": "2026-05-06T05:00:05Z"
}
```

Allowed `Run.status`:

```text
accepted
queued
running
completed
needs_review
failed
cancelled  # future, not required immediately
```

Terminal statuses for current phase:

```text
completed
needs_review
failed
```

不要加入：

```text
review_approved
review_rejected
changes_requested
```

这些属于 Review。

### 5.4 RunEvent

```json
{
  "event_id": "evt_001",
  "run_id": "api-...",
  "event_type": "tool.chainladder.completed",
  "occurred_at": "2026-05-06T05:00:05Z",
  "message": "Chainladder tool completed.",
  "payload": {
    "tool_id": "chainladder",
    "status": "completed"
  }
}
```

Core event types:

```text
run.accepted
run.queued
run.running
run.completed
run.needs_review
run.failed
tool.started
tool.completed
tool.failed
review.required
artifact.written
rerun.requested
```

Workflow event types later可以扩展：

```text
workflow.started
workflow.step.started
workflow.step.completed
workflow.step.failed
workflow.completed
workflow.failed
```

### 5.5 ArtifactRef

```json
{
  "artifact_id": "deterministic_result",
  "run_id": "api-...",
  "kind": "deterministic_result",
  "path": "deterministic_result.json",
  "mime_type": "application/json",
  "sha256": "optional-future",
  "created_at": "2026-05-06T05:00:05Z"
}
```

Artifact 是审计证据。Registry 可以索引 artifact，但不能替代 artifact。

### 5.6 Review

Review 是独立治理对象。

```json
{
  "review_id": "review_001",
  "run_id": "api-...",
  "case_id": "2026Q1-auto-liability",
  "status": "pending",
  "reason_codes": ["origin_count_below_threshold"],
  "assigned_to": null,
  "created_at": "2026-05-06T05:00:06Z"
}
```

Allowed `Review.status`:

```text
not_required
pending
approved
rejected
changes_requested
superseded
```

### 5.7 ReviewDecision

```json
{
  "decision_id": "decision_001",
  "review_id": "review_001",
  "run_id": "api-...",
  "decision": "changes_requested",
  "comment": "Please rerun with updated threshold and attach bridge analysis.",
  "decided_by": "senior_actuary_001",
  "decided_at": "2026-05-06T05:10:00Z",
  "follow_up_run_id": null
}
```

Review decision 必须写 artifact，例如：

```text
review_decision.json
review_decision.md
```

如果 decision 要求重算，应创建新的 follow-up run，不覆盖原 run。

### 5.8 Rerun

Rerun 总是产生新的 `run_id`。

```json
{
  "source_run_id": "api-original",
  "new_run_id": "api-rerun-001",
  "reason": "manual_rerun",
  "requested_by": "actuary_001"
}
```

原 run 保留，new run 通过 metadata 链接回 source run。

---

## 6. Storage 与 Evidence 边界

### 6.1 Registry

Registry 是 operational index：

```text
run_id
case_id
status
artifact_root
summary
created_at / updated_at
status_history
```

Registry 不是审计证据源。它可以丢失后从 artifacts 部分重建，但不应该反过来。

### 6.2 Artifact Store

Artifact Store 是 evidence source：

```text
case_input.json
validated_input.json
tool_result.json
deterministic_result.json
run_manifest.json
review_packet.json
review_decision.json
narrative.md
trace.json
```

### 6.3 Store interface 的目标

在进入 workflow/review/workspace 之前，先定义接口：

```python
class RunStore:
    def create_run(...): ...
    def update_run_status(...): ...
    def get_run(...): ...
    def list_runs(...): ...
    def append_event(...): ...

class ArtifactStore:
    def write_artifact(...): ...
    def read_artifact(...): ...
    def list_artifacts(...): ...

class ReviewStore:
    def create_review(...): ...
    def submit_decision(...): ...
    def get_review(...): ...
```

PR11 只定义 boundary，不强制上 SQLite/Postgres/S3。

---

## 7. Execution Mode Contract

当前阶段允许：

```text
inline
local_background
```

语义：

| mode | 说明 | 当前支持 |
|---|---|---|
| `inline` | API 请求内同步执行，适合短任务和测试 | 是 |
| `local_background` | FastAPI BackgroundTasks 本地执行，适合 prototype polling | 是 |
| `hermes_worker` | Hermes 作为外部 worker 执行长任务 | 后续 |
| `external_queue` | RQ/Celery/Temporal 等正式队列 | 后续 |
| `scheduled_cron` | 定时任务触发 run/workflow | 后续 |

约束：

- PR12b 的 sequential workflow 可以先跑在 `inline/local_background`；
- 不做失败恢复、分布式锁、worker leasing、streaming bus；
- 长耗时、多用户、生产级任务必须等 `hermes_worker` 或 external queue adapter；
- API 进程不是最终执行平台。

---

## 8. Hermes / OpenAI Agents / Symphony 边界

### 8.1 Hermes Agent

Hermes 负责：

```text
tool execution
long-running worker
skills / SOP
cron / scheduler
repo automation
Feishu/WeChat gateway
环境经验与流程记忆
```

Hermes 不负责：

```text
case truth
精算结论
review judgement
artifact evidence
```

Hermes memory 只能存 SOP、环境习惯、排障经验；任何 case facts、数字、review judgement、report inputs 都必须落 artifact。

### 8.2 OpenAI Agents SDK

OpenAI Agents SDK 负责：

```text
planner
handoff
guardrails
session
tracing
API tool wrappers
```

OpenAI Agents SDK 不负责：

```text
run registry
artifact store
review state machine
deterministic numeric truth
```

### 8.3 Symphony-lite

只借鉴 Symphony 的控制面语义：

```text
run
status
event
artifact
review
operator console
```

不要引入完整 Symphony 的：

```text
workspace orchestration
复杂 worker lifecycle
streaming fabric
full operator platform
```

### 8.4 ai_actuary 最小控制面合同

最小合同固定为：

```text
CreateRun
GetRun
ListEvents
GetArtifacts
GetReview
SubmitReviewDecision
Rerun
```

---

## 9. PR 总览（v3：合并 PR 执行版）

### 摘要

v3 不再按 PR8、PR8.5、PR9、PR10、PR11、PR12a、PR12b、PR13a、PR13b、PR14、PR15、PR16 这种细粒度推进，而是压缩成 PR8-PR15 八个产品切片。每个 PR 仍保持单一主范围，但避免过多纯 contract PR。

### 合并映射

| 新 PR | 合并来源 | 主题 | 核心价值 |
|---|---|---|---|
| PR8 | PR8 + PR8.5 | Foundation Contracts and Tool Catalog | 固定词汇表，注册 `chainladder`，冻结核心控制面合同 |
| PR9 | PR9 + PR10 | Tool-backed Run Dispatch and Input Contracts | `/runs` 正式按 tool/input contract 执行，写 `validated_input.json` |
| PR10 | PR11 | Store Boundary and Local Adapters | 为 workflow/review/workspace 防返工 |
| PR11 | PR12a + PR12b | Workflow Templates and Sequential Execution | 单工具升级为顺序多步骤工作流 |
| PR12 | PR13a + PR13b | Review Contract and Console Review Inbox | Web Console 可查看待复核并提交 decision |
| PR13 | PR14 | Per-actuary Workspace and Ownership | 每个精算师自己的 console 原型 |
| PR14 | PR15 | Agent Planner / Hermes Worker Adapter | Agent 通过公开 API 操作工作台 |
| PR15 | PR16 | Report Export and Operator Handoff | 阶段成果可导出、可交接 |

### 推荐执行顺序

```text
PR8  Foundation Contracts and Tool Catalog
PR9  Tool-backed Run Dispatch and Input Contracts
PR10 Store Boundary and Local Adapters
PR11 Workflow Templates and Sequential Execution
PR12 Review Contract and Console Review Inbox
PR13 Per-actuary Workspace and Ownership
PR14 Agent Planner / Hermes Worker Adapter
PR15 Report Export and Operator Handoff
```

---

# PR8: Foundation Contracts and Tool Catalog

**Goal:** 建立下一阶段的基础词汇表：注册第一个 actuarial tool，并冻结 `Run / RunEvent / ArtifactRef / Review / ReviewDecision / Rerun` 的核心控制面合同。

**Branch:** `feat/foundation-contracts-tool-catalog`

## Scope

- 新增 tool metadata model；
- 新增 in-process `ToolRegistry`；
- 注册 `chainladder` 作为第一个 builtin tool；
- 新增 `GET /tools`；
- 新增 `GET /tools/{tool_id}`；
- Console create-run form 的 tool selector 从 catalog 渲染；
- 新增 `contracts/control_plane.py`；
- 固定 `Run.status`、`RunEvent.type`、`ArtifactRef`、`Review.status`、`ReviewDecision`、`Rerun` 语义；
- 新增 `docs/contracts/control-plane.md`；
- 更新 README / architecture / roadmap docs。

## Non-goals

- 不改真正 run dispatch；
- 不改 operator execution；
- 不写 `validated_input.json`；
- 不做 workflow；
- 不做 review decision API。

## Files

- Create: `src/reserving_workflow/tools/__init__.py`
- Create: `src/reserving_workflow/tools/schemas.py`
- Create: `src/reserving_workflow/tools/registry.py`
- Create: `src/reserving_workflow/tools/catalog.py`
- Create: `src/reserving_workflow/tools/builtin.py`
- Create: `src/reserving_workflow/contracts/__init__.py`
- Create: `src/reserving_workflow/contracts/control_plane.py`
- Create: `docs/contracts/control-plane.md`
- Modify: `src/reserving_workflow/api/app.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/plans/actuarial-workbench-tool-console-roadmap.md`
- Test: `tests/test_tool_registry.py`
- Test: `tests/test_control_plane_contracts.py`
- Test: `tests/test_api_control_plane.py`

## Tasks

1. Write failing tests for valid/invalid `ActuarialToolMetadata`.
2. Implement tool metadata schema with safe `tool_id` validation.
3. Implement `ToolRegistry` with `register_tool`, `get_tool`, `list_tools`, duplicate rejection, unknown id error.
4. Register builtin `chainladder` metadata.
5. Add `/tools` and `/tools/{tool_id}` endpoints with 404 for unknown tool.
6. Update console JS to fetch `/tools` and render selector; fallback visibly to `chainladder` if fetch fails.
7. Write failing tests for `RunStatus`, `RunEvent`, `ArtifactRef`, `Review`, `ReviewDecision`, `Rerun` semantics.
8. Implement control-plane contract models.
9. Document that run status does not include review decisions, and rerun always creates a new run.
10. Run focused tests, full suite, and API smoke.

## Acceptance Criteria

- `GET /tools` returns `chainladder`.
- `GET /tools/chainladder` returns deterministic metadata.
- Console create-run form can render tool selector.
- Contract tests prove review statuses are separate from run statuses.
- Rerun semantics are documented as new-run-only.
- No execution behavior changed.

---

# PR9: Tool-backed Run Dispatch and Input Contracts

**Goal:** 将 `POST /runs` 从 legacy `method` 过渡到 `tool_id + inputs`，并为 `chainladder` 写入标准化 input/output schema 与 `validated_input.json` artifact。

**Branch:** `feat/tool-dispatch-input-contracts`

## Scope

- `RunCreateRequest` 增加 `tool_id` 和 `inputs`；
- 保留 `method` 作为 legacy alias；
- 内部归一化为 `tool_id="chainladder"` + `inputs.method_variant`；
- unknown tool 返回 400；
- `chainladder` input/output schema；
- 新增 `ToolInvocation` / `ValidatedToolInput`；
- 执行前写 `validated_input.json`；
- artifact manifest includes `validated_input.json`；
- Console create-run 发送 `tool_id` / `inputs`。

## Non-goals

- 不做真实文件上传；
- 不做多 tool workflow；
- 不做 review decision；
- 不切换存储。

## Files

- Create: `schemas/tools/chainladder.input.schema.json`
- Create: `schemas/tools/chainladder.output.schema.json`
- Create: `src/reserving_workflow/tools/invocation.py`
- Modify: `src/reserving_workflow/api/app.py`
- Modify: `src/reserving_workflow/operator_entrypoint.py`
- Modify: `src/reserving_workflow/artifacts/manifest.py` if needed
- Modify: `src/reserving_workflow/runtime/run_registry.py` if needed for normalized metadata
- Test: `tests/test_tool_input_contracts.py`
- Test: `tests/test_api_control_plane.py`
- Test: `tests/test_operator_entrypoint.py`

## Tasks

1. Extend request tests for `tool_id="chainladder"`, legacy `method="chainladder"`, and unknown tool.
2. Add `_normalize_tool_request(request, registry)` helper.
3. Map legacy `method` to `tool_id` and `inputs.method_variant`.
4. Preserve `sample_name` and `review_threshold_origin_count` in normalized inputs.
5. Define chainladder input and output schemas.
6. Add tool input validation before calculation.
7. Write `validated_input.json` and reference it from manifest.
8. Update console create-run payload.
9. Run focused tests, full suite, API/console smoke if changed surface is visible.

## Acceptance Criteria

- Old `method="chainladder"` still works.
- New `tool_id="chainladder"` works.
- Unknown tool fails cleanly.
- Internal contract uses `tool_id` + `inputs.method_variant`.
- Successful run writes `validated_input.json`.
- Artifact manifest references validated input.

---

# PR10: Store Boundary and Local Adapters

**Goal:** 在 workflow/review/workspace 前定义存储边界，避免把 JSON registry 硬扩成半数据库。

**Branch:** `feat/store-boundary-local-adapters`

## Scope

- 定义 `RunStore`、`ArtifactStore`、`ReviewStore` protocols/interfaces；
- 适配现有 JSON registry 和 filesystem artifacts；
- 定义 review store 的 local placeholder/artifact-backed adapter；
- 不切换 SQLite/Postgres/S3；
- 不改变 API 外部行为。

## Files

- Create: `src/reserving_workflow/storage/__init__.py`
- Create: `src/reserving_workflow/storage/interfaces.py`
- Create: `src/reserving_workflow/storage/local.py`
- Test: `tests/test_storage_interfaces.py`
- Modify: `docs/architecture.md`

## Tasks

1. Add interface tests with fake/local tmp implementations.
2. Define minimal `RunStore`: `create_run`, `update_run_status`, `get_run`, `list_runs`, `append_event`.
3. Define minimal `ArtifactStore`: `write_artifact`, `read_artifact`, `list_artifacts`.
4. Define minimal `ReviewStore`: `create_review`, `submit_decision`, `get_review`.
5. Implement `LocalRunStore` over current JSON registry.
6. Implement `LocalArtifactStore` over current filesystem artifact helpers.
7. Implement minimal `LocalReviewStore` without product UI exposure yet.
8. Update architecture docs: registry is operational index; artifacts/decisions are evidence source.

## Acceptance Criteria

- Existing API behavior unchanged.
- Store interface tests pass.
- No DB/S3 dependencies added.
- Later workflow/review PRs have a stable local adapter to build on.

---

# PR11: Workflow Templates and Sequential Execution

**Goal:** 支持最小 workflow template catalog 与顺序 workflow execution，让一次 run 可以包含多个 tool step，但仍保持 prototype 执行模式。

**Branch:** `feat/workflow-template-sequential-execution`

## Scope

- 新增 `WorkflowTemplate` schema；
- 注册 `basic_reserve_review` template；
- 新增 `GET /workflows`；
- 新增 `GET /workflows/{workflow_id}`；
- `POST /runs` 可接受 `workflow_id`；
- 最小顺序 runner；
- step events 与 step artifact refs；
- 只支持 `inline/local_background`。

## Non-goals

- 不做并行 DAG；
- 不做 retry policy；
- 不做 worker leasing；
- 不做 queue；
- 不做 websocket/SSE；
- 不做 workflow builder UI。

## Files

- Create: `src/reserving_workflow/workflows/__init__.py`
- Create: `src/reserving_workflow/workflows/schemas.py`
- Create: `src/reserving_workflow/workflows/catalog.py`
- Create: `src/reserving_workflow/workflows/builtin.py`
- Create: `src/reserving_workflow/orchestration/sequential_runner.py`
- Modify: `src/reserving_workflow/api/app.py`
- Modify: `src/reserving_workflow/contracts/control_plane.py`
- Test: `tests/test_workflow_catalog.py`
- Test: `tests/test_workflow_execution.py`
- Test: `tests/test_api_control_plane.py`

## Tasks

1. Define workflow template schema with safe `workflow_id`, version, steps, required/optional flags.
2. Add catalog tests and endpoints for list/get workflow templates.
3. Register `basic_reserve_review` with real `chainladder` step and optional/future steps if needed.
4. Add `workflow_id` to run request while preserving plain `tool_id` runs.
5. Implement sequential runner over tool registry/store interfaces.
6. Emit workflow and step events: `workflow.started`, `workflow.step.started`, `workflow.step.completed`, `workflow.step.failed`, `workflow.completed`, `workflow.failed`.
7. Stop on failed required step; skip unavailable optional step with explicit event.
8. Add tests for single-step workflow and optional skipped step.
9. Run full validation.

## Acceptance Criteria

- `GET /workflows` and `GET /workflows/{workflow_id}` work.
- Single-tool run still works.
- Workflow run emits step events.
- Optional future steps do not break current flow.
- No production queue added.

---

# PR12: Review Contract and Console Review Inbox

**Goal:** 把 review 从 packet metadata 升级为独立治理对象，并在 Web Console 中支持查看待复核与提交 decision。

**Branch:** `feat/review-contract-console-inbox`

## Scope

- Review object；
- ReviewDecision object；
- `GET /runs/{run_id}/review` or compatible endpoint；
- `POST /reviews/{review_id}/decision`；
- `review_decision.json` / `review_decision.md` artifact；
- `/console/state` 增加 review inbox payload；
- `/console` 展示 pending reviews；
- Console decision form。

## Non-goals

- 不做 SSO；
- 不做复杂权限；
- 不做审批流引擎；
- 不把 `approved/rejected/changes_requested` 写入 run terminal status。

## Files

- Modify: `src/reserving_workflow/contracts/control_plane.py`
- Create: `src/reserving_workflow/review/__init__.py`
- Create: `src/reserving_workflow/review/store.py`
- Modify: `src/reserving_workflow/api/app.py`
- Test: `tests/test_review_contract.py`
- Test: `tests/test_api_control_plane.py`
- Possibly Create: `tests/test_console_review_inbox.py`

## Tasks

1. Add tests proving run status rejects review-specific values.
2. Implement/extend Review and ReviewDecision models.
3. Implement review retrieval API.
4. Implement decision submission API with enum validation.
5. Write decision JSON/Markdown artifacts.
6. Ensure decision does not mutate run terminal status.
7. Add review inbox payload to `/console/state`.
8. Add console UI for pending reviews and decision form.
9. Handle API errors gracefully in console.
10. Run focused tests, full suite, and browser smoke for decision flow.

## Acceptance Criteria

- Review decision creates independent artifact.
- Run status remains execution-only.
- Pending review visible in console.
- Decision submission refreshes review panel/inbox.
- Browser console has no JS errors.

---

# PR13: Per-actuary Workspace and Ownership

**Goal:** 引入每个精算师自己的 lightweight workspace/run ownership，为未来团队使用打基础。

**Branch:** `feat/per-actuary-workspace-ownership`

## Scope

- `operator_id`；
- `workspace_id`；
- `created_by`；
- run ownership；
- console workspace filter；
- review `assigned_to` prototype；
- default single-user fallback。

## Non-goals

- 不做 SSO；
- 不做 OAuth；
- 不做企业多租户；
- 不做计费；
- 不做完整 RBAC。

## Files

- Modify: `src/reserving_workflow/contracts/control_plane.py`
- Modify: `src/reserving_workflow/api/app.py`
- Modify: `src/reserving_workflow/storage/local.py`
- Test: `tests/test_workspace_console.py`
- Modify: `docs/architecture.md`

## Tasks

1. Add `operator_id` and `workspace_id` request/default handling.
2. Record `created_by` and `workspace_id` on runs.
3. Add run listing filters by owner/workspace.
4. Add console filter/query/header/mock identity support.
5. Add prototype review assignment field.
6. Preserve default single-user behavior.
7. Run API/console tests and browser smoke.

## Acceptance Criteria

- A run records `created_by` and `workspace_id`.
- Console can filter by current prototype operator.
- Existing single-user default still works.
- Review assignment remains lightweight and non-RBAC.

---

# PR14: Agent Planner / Hermes Worker Adapter

**Goal:** Agent 通过 task/workflow contract 发起和检查 run；OpenAI Agents SDK / Hermes / Codex 都作为 runtime adapter，而不是业务真值源。

**Branch:** `feat/agent-planner-hermes-worker-adapter`

## Scope

- agent-facing task plan schema；
- OpenAI Agents SDK planner wrapper；
- Hermes worker/control-plane client；
- create run；
- poll events；
- read artifacts；
- read review；
- summarize result。

## Non-goals

- Agent 不改 deterministic result；
- Agent 不提交 human review judgement；
- Agent 不直接写 artifact store；
- Agent 不绕过 public API。

## Files

- Create: `workflows/agent-runtimes/openai-agents/planner_adapter.py`
- Create: `workflows/agent-runtimes/hermes-worker/control_plane_client.py`
- Create: `docs/agent-adapter-contract.md`
- Test: adapter tests with fake API client

## Tasks

1. Define agent plan schema: `case_id`, `tool_id` or `workflow_id`, `objective`, `inputs`.
2. Implement OpenAI planner adapter with fakeable client boundary.
3. Implement Hermes worker control-plane client with public API calls only.
4. Add tests for create-run, poll-events, read-artifacts, read-review.
5. Add docs explaining OpenAI/Hermes/Symphony boundaries.
6. Ensure no adapter writes case truth or review judgement.

## Acceptance Criteria

- Agent adapter calls public API contract only.
- Planner output is JSON-serializable and testable.
- Hermes worker client can monitor a run through public endpoints.
- Docs clearly state neither adapter owns case truth.

---

# PR15: Report Export and Operator Handoff

**Goal:** 基于 deterministic artifacts + review decisions 生成可交接 memo/export/sign-off handoff。

**Branch:** `feat/report-export-operator-handoff`

## Scope

- `operator_handoff.md`；
- `reserve_summary.md` or `.json`；
- export API；
- export CLI；
- console export link；
- source artifact refs；
- review decision refs。

## Non-goals

- 不做最终生产合规认证；
- 不做 PDF 美化；
- 不做 BI dashboard；
- 不补造缺失数值。

## Files

- Create: `src/reserving_workflow/reports/__init__.py`
- Create: `src/reserving_workflow/reports/export.py`
- Create: `scripts/export_run_report.py`
- Modify: `src/reserving_workflow/api/app.py`
- Test: `tests/test_report_export.py`
- Modify: `README.md`
- Modify: `docs/architecture.md`

## Tasks

1. Define report export input from run id/artifact root.
2. Load deterministic result, review packet, review decision, manifest refs.
3. Generate Markdown handoff without inventing missing numeric facts.
4. Generate JSON summary with source artifact refs.
5. Add CLI wrapper.
6. Add API endpoint and console export action/link.
7. Add tests for missing artifacts and no-fabrication behavior.
8. Run full suite and operator smoke.

## Acceptance Criteria

- Report distinguishes execution status from review status.
- Export includes source run id and artifact references.
- CLI/API both work.
- Missing facts are surfaced as missing, not filled by LLM.

---

## 10. 子 Agent / Codex 执行编排

### Controller 职责

Hermes controller 负责：

```text
1. 每个 PR 从最新 origin/main 创建新分支
2. 把当前 PR scope 交给 Codex CLI worker 实现
3. 监控 worker 输出，不直接在主上下文里随手改大范围代码
4. worker 完成后运行 focused tests + full suite + API/console smoke
5. 必要时派独立 review worker 做 code review
6. 只修确认成立的问题
7. commit / push / create PR
8. 约 15 分钟后检查 CI 和 review/Copilot comments
9. 判定 comment 是否正确，只修正确的
10. 修完后重新测试、commit、push
11. CI/review 通过后 squash merge
12. sync main，再开始下一 PR
```

### Worker 类型

| Worker | 工具 | 职责 |
|---|---|---|
| Implementer | Codex CLI | 实现当前 PR scope，写测试，跑 focused tests |
| Spec Reviewer | Codex CLI or Hermes subagent | 对照本计划检查是否漏做/越界 |
| Quality Reviewer | Codex CLI or Hermes subagent | 检查代码质量、安全、回归风险 |
| PR Comment Handler | Hermes controller + optional Codex | 拉取 comments，逐条判断，只修正确项 |

### 并发原则

默认 **一个实现 PR 一个 worker**，不要同时开多个 PR 的实现 worker，因为每个 PR 都必须基于最新 `origin/main`。可以并行的只有：

```text
- 当前 PR 实现完成后的独立 review worker
- 当前 PR comments 的只读分析 worker
```

不要并行实现 PR8 和 PR9，否则 PR9 很容易基于尚未合并的 contract 变动返工。

### 10 分钟进度汇报节奏

每 10 分钟汇报一次：

```text
当前 PR / 分支
worker 状态
已改文件
已跑测试
当前 blocker
下一个动作
PR URL / merge 状态（如已有）
```

### 15 分钟 PR follow-up

每个 PR 创建后约 15 分钟：

```text
1. gh pr view
2. gh pr checks
3. gh api pulls comments
4. gh api graphql reviewThreads
5. 逐条判断 comment 是否成立
6. 只修确认成立的问题
7. focused tests -> full suite
8. push fixes
9. resolve truly fixed threads
10. green 后 merge
```

---

## 11. 中间 Tool 发展路线

### 第一批 tool candidates

```text
reserving:
- chainladder
- bornhuetter_ferguson
- cape_cod
- tail_factor_selection
- ldf_selection

validation:
- triangle_validation
- data_reconciliation
- exposure_validation
- large_loss_check

diagnostics:
- outlier_detection
- calendar_year_effect_check
- development_pattern_stability
- prior_period_bridge

reporting/governance:
- review_packet_generator
- reserve_summary_generator
- assumption_traceability_check
- documentation_completeness_check
```

### Tool 接入准则

每个 tool 必须有：

```text
tool metadata
input schema
output schema
validation tests
artifact outputs
docs / README
review triggers if relevant
```

每个 tool 不应该：

```text
直接写 console UI
直接依赖 LLM 得出数值
绕过 artifact contract
绕过 review contract
```

---

## 12. 验证策略

每个 PR 至少运行：

```bash
python -m pytest tests -q
```

涉及 API/console 的 PR 还应运行：

```bash
python - <<'PY'
from fastapi.testclient import TestClient
from reserving_workflow.api.app import create_app
client = TestClient(create_app())
assert client.get('/health').json()['ok'] is True
print('api_smoke_ok')
PY
```

涉及 console 关键交互的 PR，应本地起服务并用浏览器实点：

```bash
python -m uvicorn 'reserving_workflow.api.app:create_app' --factory --host 127.0.0.1 --port 8000
```

然后检查：

```text
/health
/console
/console/state
browser console JS errors
create run
poll events
rerun
review decision if relevant
```

---

## 13. 主要风险与反模式

### 架构风险

- 没冻结 control-plane contract 就继续加 workflow/review/workspace；
- 把 run status 和 review status 混在一起；
- 把 `tool_id` 和 `method_variant` 混在一起；
- 把 workflow execution 写进 CAS core，导致 runtime 耦合；
- 过早把 JSON registry 扩成半数据库。

### 产品风险

- Web Console 变成 dashboard，而不是 operator console；
- 每个精算师 workspace 过早做成企业多租户系统；
- Agent 看起来聪明，但审计链条断裂；
- 报告导出补造缺失数值。

### 合规/审计风险

- 无法区分“计算完成”和“人工批准”；
- Review decision 不落 artifact；
- Validated input 不落 artifact；
- Rerun 覆盖 source run；
- Memory 存 case facts 或 review judgement。

### 运营风险

- 多步任务继续挂在 FastAPI BackgroundTasks 中假装生产队列；
- 没有清楚区分 inline/local_background/hermes_worker；
- Agent adapter 直接调用内部函数而不是公开 API contract。

---

## 14. 推荐执行顺序

```text
1. PR8  Foundation Contracts and Tool Catalog
2. PR9  Tool-backed Run Dispatch and Input Contracts
3. PR10 Store Boundary and Local Adapters
4. PR11 Workflow Templates and Sequential Execution
5. PR12 Review Contract and Console Review Inbox
6. PR13 Per-actuary Workspace and Ownership
7. PR14 Agent Planner / Hermes Worker Adapter
8. PR15 Report Export and Operator Handoff
```

这个顺序的核心逻辑是：

```text
Tool + control-plane vocabulary
  -> Run dispatch + input evidence
  -> Store boundary
  -> Workflow
  -> Review
  -> Workspace
  -> Agent adapter
  -> Report export
```

---

## 15. 对未来实现 Agentic Actuarial Workbench 的判断

如果按 v3 路线推进，`ai_actuary` 会自然长成：

```text
Actuary Web Console
  -> CreateRun / GetRun / ListEvents / GetArtifacts / GetReview / Rerun
  -> Tool Registry
  -> Tool Input Contracts
  -> Workflow Template Catalog
  -> Local/Worker Execution Adapter
  -> Artifact / Review / Replay / Export
  -> Agent Planner / Hermes Worker / OpenAI SDK Wrapper
```

这比“AI API 包一个聊天窗口”更稳，因为：

- 精算 tool 是可测的；
- inputs 是可验证的；
- results 是可复现的；
- review 是可审计的；
- Agent 是可替换 adapter；
- Console 是 operator surface；
- Hermes/OpenAI/Symphony 都只作为合适层位的能力来源，不反客为主。

---

## 16. 文档状态

- 本地 Markdown：`docs/plans/actuarial-workbench-tool-console-roadmap.md`
- 飞书文档：`https://www.feishu.cn/docx/RCE7deyyVo4ZPAxKDeYcGTZzn9e`
- 当前版本：v3，已合并 Codex architecture review 的关键修改建议，并将后续路线压缩为 PR8-PR15 八个产品切片。
