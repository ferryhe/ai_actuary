# Codex Step-by-Step Development Prompts

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