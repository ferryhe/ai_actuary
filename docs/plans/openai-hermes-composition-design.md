---

## 概览

本项目不采用“把 OpenAI Agents SDK 与 Hermes 合并成一个单体框架”的路线，而采用**组合式架构**：

- **CAS Core** 负责精算真值与研究资产；
- **OpenAI Agents Python** 负责规划、路由、治理决策；
- **Hermes** 负责执行、落地、审计打包与长期流程经验沉淀。

这条路线的核心价值有三点：

1. **保留 runtime-neutral core**：deterministic reserving logic、constitution、benchmark、artifact schema 不绑定任何 agent 框架。
2. **保留编排与执行解耦**：OpenAI Agents 负责“决定做什么”，Hermes 负责“把事情做完”。
3. **保留未来可比性**：后续可并行接入 Hermes / OpenClaw / 其他 runtime，对同一 benchmark 进行横向研究。

一句话定义：**OpenAI 做 planner，Hermes 做 worker，CAS Core 做 truth layer。**

---

## 设计原则

### 摘要

本项目的架构和计划必须服从研究目标，而不是服从某个 agent 框架的默认能力边界。

### 来源

CAS proposal、`docs/architecture.md`、`docs/project-plan.md` 已明确强调：
- 数值真值不能来自 LLM；
- governance 要可执行；
- workflow 要可审计、可回放；
- runtime 只是 adapter，不是业务逻辑宿主。

### 结论

以下原则固定不变：

- **Deterministic first**：准备金、诊断指标、数值引用以外部 deterministic engine 为准。
- **Governance first**：任何 narrative 或结论都必须经过 constitution 检查与 review gate。
- **Artifacts over memory**：研究事实落结构化 artifacts，不落 agent 记忆。
- **Composition over merge**：框架之间靠 contract 对接，不做深耦合融合。
- **Planner / Worker split**：规划、路由、升级决策与执行落地分层处理。

---

## 总体架构

### 摘要

采用三层结构：**CAS Core → OpenAI Planner → Hermes Workers**。

### 分层定义

#### 1. CAS Core：领域核心层

定位：系统真值层、研究资产层、可替换 runtime 的稳定内核。

建议落位：`src/reserving_workflow/`

包含内容：
- deterministic calculators
- reserving case schema
- benchmark schema
- constitution rule engine
- review trigger logic
- artifact schema
- evaluation rubric
- replay / comparison utilities

这层不依赖 OpenAI Agents，也不依赖 Hermes。

#### 2. OpenAI Planner：编排与治理层

定位：项目的大脑，负责规划、路由、阶段决策、异常升级、trace-aware orchestration。

建议落位：`workflows/agent-runtimes/openai-agents/`

包含内容：
- workflow manager agent
- routing / triage agent
- review router agent
- narrative planning logic
- planner-facing tool wrappers
- session / tracing configuration

这层不直接拥有精算真值，只调度工具与 worker。

#### 3. Hermes Workers：执行与操作层

定位：项目的施工队，负责真实执行、文件/脚本/任务落地、artifact 打包、消息通知、长期流程经验沉淀。

建议落位：`workflows/agent-runtimes/hermes-worker/`

包含内容：
- single-case worker
- benchmark-batch worker
- audit-bundle worker
- review-packet worker
- cron / replay / notification worker

Hermes 的长期价值在于：
- tool execution
- memory / skills
- cron / messaging / subagent
- 环境经验复用

### 结论

**OpenAI Agents 不做主执行器，Hermes 不做主规划器，CAS Core 不做会话代理。三层职责不能混。**

---

## 角色定义

### 摘要

角色分三类：系统角色、逻辑角色、执行角色。

### 1. 系统角色

#### CAS Core Owner
负责维护：
- deterministic calculators
- constitution
- benchmark schema
- artifact schema
- evaluation rubric

这是项目长期最值钱的资产层。

#### Experiment Operator
负责：
- 发起实验
- 查看结果
- 处理 review queue
- 审阅输出
- 决定是否纳入研究样本

### 2. OpenAI Planner 逻辑角色

#### Workflow Manager Agent
职责：
- 接收任务请求
- 判断当前任务类型
- 调度 tool / Hermes worker
- 决定是否进入 review
- 汇总最终输出状态

#### Triage / Routing Agent
职责：
- 判断 case 是常规、异常、缺数据还是需升级
- 选择运行路径：baseline / governed / replay / review-only

#### Review Router Agent
职责：
- 解释触发 review 的原因
- 生成 review packet
- 决定通知对象与升级级别

#### Narrative Planner Agent
职责：
- 决定 narrative 需要哪些上下文
- 约束 narrative 输出结构
- 控制不是“写得漂亮”，而是“写得可核对”

### 3. Hermes 执行角色

#### 主 Hermes（长期存在）
职责：
- 沉淀 skills
- 记住环境经验
- 记住流程经验
- 维护复用型 execution knowledge

它记的是**方法**，不是研究真值。

#### Ephemeral Case Worker
职责：
- 跑单个 reserving case
- 调 deterministic tools
- 收集上下文
- 生成 audit bundle
- 返回结构化结果

#### Ephemeral Benchmark Worker
职责：
- 批量跑 benchmark
- 记录 run manifests
- 回填对比结果
- 归档 batch artifacts

#### Ephemeral Review Worker
职责：
- 打包 review packet
- 推送通知
- 收集 reviewer feedback
- 回写 review artifacts

### 结论

**长期只保留一个主 Hermes；执行时按任务临时起 worker。** 不建议一开始养多个长期专家 Hermes。

---

## 为什么采用“一个主 Hermes + 多个临时 worker”

### 摘要

这不是折中方案，而是最适合研究型系统的方案。

### 原因

1. **统一经验沉淀**：主 Hermes 统一沉淀项目级 skills 和环境知识，避免多个长期 agent 产生记忆漂移。
2. **避免记忆污染**：benchmark 批跑、review 打包、单 case 调试彼此上下文不同，适合短命 worker。
3. **便于扩展并发**：benchmark 任务天然可并行，临时 worker 比长期专家更易横向扩容。
4. **保证研究可复现**：事实落 artifact，经验落主 Hermes，执行落 worker，边界清楚。

### 结论

项目的可复制性来自**externalized artifacts + stable contracts**，不是来自“某个 agent 学会了很多东西”。

---

## 工作流定义

### 摘要

整个系统采用单 case workflow 与 batch workflow 两条主线。

### A. 单 case governed workflow

1. **Intake**
   - 输入 case_id、triangle 数据、metadata、运行配置
   - OpenAI Workflow Manager 接收请求

2. **Triage**
   - Routing Agent 判断任务模式：
     - direct baseline
     - retrieval baseline
     - governed workflow
     - replay
     - review-only

3. **Dispatch**
   - Planner 调用 `run_case_worker` 类工具
   - 启动 Hermes Ephemeral Case Worker

4. **Deterministic Execution**
   - Hermes worker 调用 CAS Core calculators
   - 生成 deterministic outputs、diagnostics、validation snapshot

5. **Context Assembly**
   - Hermes worker / planner tool 读取 benchmark notes、constitution refs、policy context

6. **Narrative Drafting**
   - OpenAI Planner 侧生成结构化 narrative 草稿
   - 输出必须引用 deterministic outputs，不允许自由发明数值

7. **Constitution Check**
   - CAS Core rule engine 执行 hard constraints、soft guidance、review triggers

8. **Review Gate**
   - 若触发 review，Planner 调用 Hermes Review Worker 生成 packet 并通知
   - 若不触发 review，继续进入 final packaging

9. **Artifact Packaging**
   - Hermes worker 生成 run artifact bundle
   - 包含输入、deterministic result、context、draft、checks、review、final output

10. **Return**
   - OpenAI Planner 汇总为结构化 run result
   - 写入实验记录 / 文档 / dashboard

### B. Batch benchmark workflow

1. 载入 benchmark case list
2. Planner 为每个 case 生成任务
3. 分发给多个 Hermes Benchmark Workers
4. 回收 run artifacts
5. 按 rubric 打分
6. 汇总 repeatability / failure modes / escalation quality
7. 输出 comparison report

### 结论

**Planner 负责决策和收口，Hermes 负责执行和打包，CAS Core 负责判断什么算对。**

---

## 关键接口与契约

### 摘要

如果接口不先定，后面组合式架构一定会失控。

### 核心任务接口：Planner → Hermes Worker

```json
{
  "task_id": "uuid",
  "task_kind": "run_case|run_batch|build_review_packet|replay_case",
  "case_ref": "case_001",
  "objective": "Run governed reserving workflow and return artifacts",
  "inputs": {
    "case_path": "benchmarks/cases/...",
    "mode": "governed",
    "model_profile": "planner-default"
  },
  "allowed_actions": [
    "calculator_call",
    "context_retrieval",
    "artifact_write",
    "notification"
  ],
  "required_artifacts": [
    "deterministic_result.json",
    "constitution_check.json",
    "run_manifest.json"
  ],
  "escalation_policy": {
    "review_on_hard_fail": true,
    "review_on_trigger": true
  },
  "success_criteria": {
    "artifact_complete": true,
    "numeric_consistent": true
  }
}
```

### Worker → Planner 返回接口

```json
{
  "task_id": "uuid",
  "status": "completed|failed|needs_review",
  "summary": "short status",
  "artifact_paths": {
    "run_manifest": "...",
    "deterministic_result": "...",
    "constitution_check": "..."
  },
  "metrics": {
    "duration_sec": 0,
    "tool_calls": 0
  },
  "review_reason": ["threshold_crossed"],
  "errors": []
}
```

### 结论

所有跨层协作都必须经过**结构化 task contract**，不能主要靠自然语言临场解释。

---

## 记忆与状态策略

### 摘要

Hermes 可以有记忆，但项目事实不能寄存在 Hermes 记忆里。

### Hermes 应该记住的内容

- 环境经验
- 常用命令
- 文档与汇报偏好
- review 习惯
- benchmark 执行套路
- 常见故障处理
- 审计打包技能

### Hermes 不应成为主存储的内容

- reserving case 真值
- 每次 run 的最终结果
- benchmark score
- constitution check 正式记录
- reviewer 最终判定
- 可发表实验样本

### 正确落位

- **Memory / skills**：Hermes
- **Research truth / outputs**：repo + artifact store + database
- **Sessions / tracing**：OpenAI Planner + artifact links

### 结论

**Hermes 记方法，系统记事实。**

---

## 目录与代码落位建议

### 摘要

代码必须从第一天就按三层架构落位，否则后面无法保持组合式边界。

### 建议目录

```text
src/reserving_workflow/
  schemas/
  calculators/
  constitution/
  retrieval/
  artifacts/
  review/
  evaluation/

workflows/agent-runtimes/
  openai-agents/
    agents.py
    tools.py
    runner.py
    routing.py
    tracing.py
    config.py
  hermes-worker/
    task_contracts.py
    case_worker.py
    batch_worker.py
    review_worker.py
    artifact_packager.py
    notifier.py

benchmarks/
  cases/
  rubrics/
  runners/

infra/
  queue/
  storage/
  configs/

tests/
  unit/
  integration/
  regression/
```

### 结论

**不要把 constitution、calculator、benchmark logic 写进 OpenAI agent 文件或 Hermes worker prompt 里。** 它们必须属于 CAS Core。

---

## 分阶段项目计划

## Phase 0：架构收口与合同先行

### 目标

先把 contract、schema、目录和边界定住，不急着做花哨 agent。

### 输出

- task contract v1
- run artifact schema v1
- directory layout
- minimal workflow spec
- decision record：为何采用组合式方案

### 验收

- planner / worker / core 三层职责写清楚
- 单 case 输入输出 schema 能跑通 mock

---

## Phase 1：单 case MVP

### 目标

做出一个完整、可审计的 governed reserving 单 case 流程。

### 输出

- deterministic calculator wrapper
- case worker
- planner tool wrapper
- constitution check v1
- run artifact bundle
- one synthetic case end-to-end demo

### 验收

- 单 case 能完整生成 deterministic result、narrative、checks、review decision、manifest
- numeric consistency 检查可自动执行

---

## Phase 2：review 与治理闭环

### 目标

把 constitution 从文档变成执行规则，把 review 从“人工补充”变成系统化阶段。

### 输出

- hard constraints engine
- soft guidance checks
- review trigger rules
- review worker
- reviewer feedback schema

### 验收

- hard fail 能阻断输出
- 触发 review 的 case 能自动打包与回写

---

## Phase 3：benchmark 批量实验

### 目标

把系统从 demo 变成研究平台。

### 输出

- benchmark runner
- batch worker
- baseline runners
- rubric scoring pipeline
- comparison report

### 验收

- 同一批 cases 能对比 baseline / governed workflow
- 可输出 repeatability 与 escalation quality 结果

---

## Phase 4：长期记忆与流程进化

### 目标

补强 Hermes 的长期执行价值，但不污染研究真值层。

### 输出

- skillized audit packaging
- replay / rerun workflow
- benchmark 回归任务
- reviewer preference capture
- periodic cron reporting

### 验收

- Hermes 可复用稳定流程经验
- 不影响 artifact truth 的结构化管理

---

## 风险与控制

### 主要风险

1. **职责混淆**：Planner 与 worker 边界被 prompt 模糊化。
2. **真值漂移**：narrative 偷偷替代 deterministic outputs。
3. **研究不可复现**：关键状态散落在会话里，未结构化落盘。
4. **记忆污染**：把 benchmark 批跑细节混入长期 agent 记忆。
5. **过早多 agent 化**：为了炫技拆太多长期角色，维护成本升高。

### 控制手段

- 所有关键输出结构化落 artifact
- hard constraints 独立于 LLM 执行
- 主 Hermes 只沉淀流程经验
- worker 默认临时、短命、可回放
- 任何新角色先写 contract，再写 prompt

---

## Definition of Done

当以下条件满足时，可认为该项目第一阶段“完成”：

- 有稳定的三层架构实现：CAS Core / OpenAI Planner / Hermes Workers
- 单 case governed workflow 可运行、可审计、可 review
- benchmark 批量 workflow 可对比 baseline
- hard constraints 与 review triggers 可执行
- 所有研究事实可回放、可复现、可归档
- Hermes 已沉淀必要执行 skills，但未成为研究事实单点依赖

---

## 给 Codex 的分步骤提示词

以下提示词按顺序执行。每一步都要求 Codex：
- 先读现有代码与文档；
- 只做当前步范围；
- 写测试；
- 运行相关测试；
- 输出修改摘要、文件列表、未解决问题；
- 不擅自扩大范围。

### Prompt 1：建立三层目录与基础 schema

你在 `CAS_Constitutional_AI_Workflow` 仓库中工作。请按“CAS Core / OpenAI Planner / Hermes Worker”三层架构建立最小目录骨架与基础 schema，但不要实现复杂业务逻辑。目标是让项目具备后续开发的清晰落位。

要求：
- 在 `src/reserving_workflow/` 下创建 `schemas/`, `artifacts/`, `constitution/`, `review/`, `evaluation/` 的最小模块骨架。
- 在 `workflows/agent-runtimes/openai-agents/` 下创建 `agents.py`, `tools.py`, `runner.py`, `routing.py`, `config.py`。
- 在 `workflows/agent-runtimes/hermes-worker/` 下创建 `task_contracts.py`, `case_worker.py`, `batch_worker.py`, `review_worker.py`, `artifact_packager.py`。
- 定义最小 Pydantic schema：`ReservingCaseInput`, `DeterministicReserveResult`, `NarrativeDraft`, `ConstitutionCheckResult`, `ReviewDecision`, `RunArtifactManifest`, `WorkerTask`, `WorkerResult`。
- 补充最小单元测试，验证 schema 创建与序列化。
- 更新必要 README 或 docs，使三层职责清楚。
- 不要实现真实 calculator，不要接 OpenAI API，不要接 Hermes CLI。

完成后输出：
1. 修改文件列表
2. 测试命令与结果
3. 下一步建议

### Prompt 2：实现 deterministic calculator boundary

基于现有仓库，实现 deterministic reserving calculator boundary，只做可测试的最小版本，不做复杂精算算法扩展。

要求：
- 在 `src/reserving_workflow/calculators/` 中加入最小 calculator interface。
- 提供一个可重复、可测试的 mock reserving calculator，输入 case data，输出 `DeterministicReserveResult`。
- 输出字段至少包括 reserve summary、关键 diagnostics、validation-ready metadata。
- 保持 calculator 与任何 agent runtime 解耦。
- 为 calculator 写单元测试，覆盖正常输入、缺字段、非法值。
- 如果已有相关结构，优先复用，不要重复造轮子。

完成后输出：
1. 修改文件列表
2. 测试命令与结果
3. 当前 deterministic boundary 还缺什么

### Prompt 3：实现 constitution rule engine v1

请在现有三层架构上实现 constitution rule engine v1，重点是 hard constraints 和 review triggers，不要做花哨 prompt engineering。

要求：
- 在 `src/reserving_workflow/constitution/` 中实现规则执行入口。
- 至少支持以下规则：
  - narrative 中引用的关键 numeric values 必须与 deterministic result 对齐；
  - 缺关键输入时失败；
  - 当 diagnostics 超阈值时触发 review；
  - artifact 不完整时触发 review 或 fail。
- 输出 `ConstitutionCheckResult`，区分 pass / fail / review_required。
- 写单元测试覆盖 pass、hard fail、review trigger 三种情况。
- 不把规则写死在 OpenAI agent prompt 中，规则必须在 core 层。

完成后输出：
1. 修改文件列表
2. 测试命令与结果
3. 还可扩展的 rule points

### Prompt 4：实现 Hermes worker contract 与 case worker 最小闭环

请实现 Hermes worker 侧的最小闭环，但先用普通 Python callable / local adapter 模拟，不直接依赖 Hermes CLI。目标是先把 worker contract 跑通。

要求：
- 在 `workflows/agent-runtimes/hermes-worker/task_contracts.py` 中完善 `WorkerTask` 与 `WorkerResult`。
- 在 `case_worker.py` 中实现 `run_case_worker(task)`：
  - 读取 `WorkerTask`
  - 调用 deterministic calculator
  - 生成最小 narrative placeholder 或 structured draft stub
  - 执行 constitution check
  - 生成 `RunArtifactManifest`
  - 返回 `WorkerResult`
- 在 `artifact_packager.py` 中实现最小 artifact 打包逻辑。
- 写 integration-style tests，验证单个 task 可以完成整个 worker 闭环。
- 不直接接真实 OpenAI，不接真实 Hermes；先把 contract 层跑通。

完成后输出：
1. 修改文件列表
2. 测试命令与结果
3. 真实 Hermes 接入前还缺什么

### Prompt 5：实现 OpenAI planner tool wrapper 与 runner skeleton

请在 `workflows/agent-runtimes/openai-agents/` 中实现 planner-facing runner skeleton 和 tool wrapper，但先保持可离线测试，不要求真实调用 OpenAI。

要求：
- 在 `tools.py` 中封装对 case worker 的调用接口，例如 `run_case_worker_tool`。
- 在 `routing.py` 中实现最小 triage logic：baseline / governed / review-only 三种分支。
- 在 `runner.py` 中实现 planner workflow skeleton：intake → route → dispatch worker → collect result。
- `agents.py` 先定义角色配置占位，不要求完整 prompt。
- 为 runner 与 routing 写测试。
- 保持 planner 与 worker 解耦，planner 只通过 contract 和工具调用 worker。

完成后输出：
1. 修改文件列表
2. 测试命令与结果
3. 真实 OpenAI Agents SDK 接入点说明

### Prompt 6：接入真实 OpenAI Agents SDK 最小 governed workflow

现在请把 planner skeleton 接到真实 OpenAI Agents SDK，但只做最小 governed workflow，不扩展到多模型和复杂 handoff。

要求：
- 用 OpenAI Agents SDK 定义最小 `Workflow Manager Agent`。
- 通过 tool wrapper 调用 case worker。
- 让 agent 输出结构化 narrative draft 或结构化 summary，而不是自由散文。
- 保持 numeric truth 来自 worker / deterministic result，不允许 agent 自行生成关键数值。
- 加入最小 tracing 配置与运行示例。
- 写最小 integration test 或 smoke test（必要时可 mock OpenAI 调用）。
- 更新文档说明如何配置运行。

完成后输出：
1. 修改文件列表
2. 测试命令与结果
3. 当前 OpenAI planner 的局限与下一步建议

### Prompt 7：实现 review worker 与 review packet workflow

请实现 review worker 与 review packet workflow，让系统在 review trigger 时不只是报错，而是进入可操作流程。

要求：
- 在 `review_worker.py` 中实现 review packet 生成。
- review packet 至少包含：case summary、deterministic outputs、failed checks / triggered rules、draft narrative、artifact links。
- planner 在 `review_required` 时调用 review worker。
- 支持输出 reviewer-friendly JSON 或 markdown packet。
- 写测试覆盖 review flow。
- 不做复杂消息平台接入，先把 packet 生成为本地 artifact。

完成后输出：
1. 修改文件列表
2. 测试命令与结果
3. 若后接 Hermes messaging / Feishu，推荐从哪里接

### Prompt 8：实现 benchmark batch runner 与对比基线

请把系统从单 case 扩展到 benchmark 批量运行，并加入最小 baseline 对比能力。

要求：
- 在 `benchmarks/runners/` 中实现 batch runner。
- 支持遍历多个 benchmark cases。
- 至少支持两种运行模式：`baseline_prompt` 与 `governed_workflow`。
- 回收每个 case 的 artifact manifest 与结果 summary。
- 在 core 层加入最小 scoring / comparison utility。
- 写测试或 smoke runner，验证 batch pipeline 可运行。
- 输出一个最小 comparison report artifact。

完成后输出：
1. 修改文件列表
2. 测试命令与结果
3. 当前 benchmark pipeline 缺少哪些研究级能力

### Prompt 9：补齐 artifact store、replay、repeatability hooks

请补齐研究项目必需的 artifact / replay / repeatability hooks，使系统可以更好支持 CAS 研究复现。

要求：
- 完善 `RunArtifactManifest` 与 artifact 存储结构。
- 提供 replay utility：基于已有 artifacts 重放一个 case。
- 提供 repeatability helper：同一 case 多次运行结果的摘要对比。
- 保持实现简洁，不引入重型基础设施。
- 写测试覆盖 manifest、replay、repeatability 的最小行为。

完成后输出：
1. 修改文件列表
2. 测试命令与结果
3. 还需哪些能力才能支持正式实验归档

### Prompt 10：文档收口与开发者交接

请对整个项目当前实现做一次文档收口，目标是让新开发者和未来的 Codex / Hermes worker 都能接手继续开发。

要求：
- 更新 `README.md`、`docs/architecture.md`、`docs/project-plan.md` 或新增必要 docs。
- 清楚写明三层架构：CAS Core / OpenAI Planner / Hermes Workers。
- 写清楚如何跑单 case、如何跑 batch、如何查看 artifacts、如何触发 review flow。
- 列出当前已完成范围、未完成范围、下一阶段建议。
- 补齐必要测试并执行。
- 不顺手重构无关代码。

完成后输出：
1. 修改文件列表
2. 测试命令与结果
3. 项目当前完成度评估

---

## 最后判断

这个项目最优的路线不是“选择 OpenAI 或 Hermes”，而是：

- 用 **CAS Core** 固定精算与研究真值；
- 用 **OpenAI Agents Python** 固定治理型规划层；
- 用 **Hermes** 固定执行、复用与流程操作层；
- 用 **结构化 contract + artifact** 固定系统边界。

这就是这类研究型精算 agent 项目的正确组合方式。