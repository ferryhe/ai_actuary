# ai_actuary 工具化拆分与 ai_interface 编排集成多步 PR 计划

> **For Hermes:** Use `actuarial-ai-project-workflows` + `writing-plans` + `subagent-driven-development` to implement this plan task-by-task. Keep each PR narrow, contract-first, test-first, and verify with focused tests plus broader suite before PR-visible changes.

**Goal:** 将 `ai_actuary` 从“一个 Python workbench/control-plane 项目”演进为“可由 `ai_interface` 编排的精算工具集合”，用版本化契约、文件制品和轻量执行器连接 Python 精算逻辑与 TypeScript console/pipeline。

**Architecture:** Python 继续保留精算业务权威：chainladder 计算、narrative draft、constitution check、review packet、replay、repeatability、report export 都先以 CLI/file-artifact 工具形态导出。`ai_interface` 作为上层 console/orchestrator，只消费 tool manifest、JSON Schema、run artifacts、logs、exit code，不重写精算逻辑。HTTP/MCP 适配器只作为后续可选层，不作为第一阶段前提。

**Tech Stack:** Python 3.11+, Pydantic v2 JSON Schema, pytest, existing FastAPI control plane, CLI wrappers, artifact manifest files, TypeScript/pnpm/OpenAPI/Zod in `ai_interface`.

---

## 0. 当前判断与边界

### 0.1 已确认的现状

`ai_actuary` 在最新版 `origin/main` 已经不是纯黑盒 FastAPI 单体，而是具备以下内部分层：

- `src/reserving_workflow/calculators/chainladder_adapter.py` — chainladder-python 确定性计算边界。
- `src/reserving_workflow/constitution/engine.py` — 纯规则治理检查。
- `workflows/agent-runtimes/hermes-worker/review_worker.py` — review packet 生成。
- `src/reserving_workflow/artifacts/replay.py` — replay 与 repeatability helper。
- `src/reserving_workflow/reports/export.py` — operator handoff report export。
- `src/reserving_workflow/contracts/control_plane.py` — Run/Event/Review/Workflow/ToolInvocation 等控制面契约。
- `src/reserving_workflow/schemas/core.py` — ReservingCaseInput、DeterministicReserveResult、ConstitutionCheckResult、RunArtifactManifest 等业务制品契约。
- `src/reserving_workflow/tools/catalog.py` 和 `src/reserving_workflow/workflows/catalog.py` — 现有 tool/workflow catalog 雏形。

### 0.2 本计划的核心边界

1. **不把 Python 精算逻辑搬进 `ai_interface`。** `ai_interface` 只做编排、可视化、审批、制品浏览。
2. **第一阶段不拆微服务。** 所有工具先以 CLI + 文件制品形式接入；HTTP/MCP 后置。
3. **Pydantic 是契约源头。** JSON Schema 从 Pydantic/control-plane contracts 导出，TypeScript/Zod 只消费或生成，不手写分叉 schema。
4. **必须显式补出 `narrative-draft`。** constitution check 依赖 narrative draft，不能让 `chainladder-calc` 既计算又承担叙述职责。
5. **每个 PR 单一范围。** PR 要小，但范围内必须可跑、可测、可验收。

---

## 1. 目标工具集合

| Tool ID | 职责 | 输入 | 输出 | 第一版执行方式 |
|---|---|---|---|---|
| `chainladder-calc` | 确定性准备金计算 | `case_input.json` | `deterministic_result.json` | CLI |
| `narrative-draft` | 从确定性结果生成基础叙述草稿 | `case_input.json` + `deterministic_result.json` | `narrative_draft.json` | CLI |
| `constitution-check` | 治理/规则校验 | `case_input.json` + `deterministic_result.json` + `narrative_draft.json` + optional `run_manifest.json` | `constitution_check.json` | CLI |
| `review-generator` | 生成审查升级包 | `constitution_check.json` + deterministic/narrative artifacts + manifest | `review_packet.json` + `review_packet.md` | CLI |
| `replay-run` | 从保存制品重放计算 | `run_manifest.json` | `replayed_result.json` | CLI |
| `repeatability-check` | 多次运行稳定性对比 | `[run_manifest.json, ...]` | `stability_report.json` | CLI |
| `report-export` | 操作员交接报告导出 | registry/run id + artifacts/review store | `operator_handoff.md` + `reserve_summary.*` | CLI |

Canonical pipeline:

```text
case_input.json
  -> chainladder-calc -> deterministic_result.json
  -> narrative-draft -> narrative_draft.json
  -> constitution-check -> constitution_check.json
  -> review-generator when review_required -> review_packet.json/md
  -> report-export -> operator_handoff.md
```

Side pipelines:

```text
run_manifest.json -> replay-run -> replayed_result.json
[run_manifest.json...] -> repeatability-check -> stability_report.json
```

---

## 2. PR 总览

| PR | Repo | Branch | 主题 | 性质 | 验收口径 |
|---|---|---|---|---|---|
| PR1 | `ai_actuary` | `docs/tool-contract-manifest-v1` | 工具契约、schema export、golden artifacts | docs/tests only | 契约可审查，schema/fixtures 可验证 |
| PR2 | `ai_actuary` | `feat/tool-cli-entrypoints` | 7 个工具 CLI 入口 | runtime + tests | 每个工具可用 JSON file IO 独立运行 |
| PR3 | `ai_actuary` | `feat/tool-artifact-runner` | 本地 tool runner 与 manifest executor | runtime + tests | 能顺序执行多个 CLI tool 并收集 artifacts/logs |
| PR4 | `ai_interface` | `feat/skill-manifest-cli-executor` | 最小 SkillManifest + CLI Executor | TS runtime + tests | 能执行 `chainladder-calc` 并展示制品 |
| PR5 | `ai_interface` | `feat/actuarial-pipeline-runner` | 精算 pipeline 编排与 Backstage 可见性 | TS runtime/UI + tests | 完整 pipeline 可串联 calc→narrative→constitution→review/export |
| PR6 | `ai_actuary` | `feat/tool-contract-compat-suite` | 跨 repo contract fixture/compat suite | tests/docs | `ai_actuary` 与 `ai_interface` 对同一 fixtures 解释一致 |
| PR7 | `ai_actuary` or separate | `feat/optional-chainladder-http-adapter` | 可选 HTTP adapter spike | optional | 只有需要并发/长驻时才做 |
| PR8 | `ai_actuary` or separate | `feat/optional-mcp-tools-adapter` | 可选 MCP adapter spike | optional | 只有需要多 agent 工具发现时才做 |

---

## 3. PR1 — Contract-only：工具契约与 golden artifacts

**Repo:** `ai_actuary`  
**Branch:** `docs/tool-contract-manifest-v1`  
**Commit type:** `docs`, `test`  
**Non-goals:** 不改执行路径、不新增 HTTP/MCP、不改 console。

### 3.1 Objective

定义 tool manifest v1、artifact layout、schema export 规则和 golden fixture，先让边界可审查，避免 runtime 先行导致契约漂移。

### 3.2 Files

- Create: `docs/contracts/actuarial-tool-manifest-v1.md`
- Create: `docs/contracts/actuarial-artifact-layout-v1.md`
- Create: `docs/plans/2026-05-20-ai-actuary-tool-decomposition-pr-plan.md` if not already present
- Create: `scripts/export_contract_schemas.py`
- Create: `schemas/actuarial-reserving/v1/*.schema.json`
- Create: `tests/test_contract_schema_export.py`
- Create: `tests/fixtures/tool_contracts/golden_run/`
  - `case_input.json`
  - `deterministic_result.json`
  - `narrative_draft.json`
  - `constitution_check.json`
  - `run_manifest.json`
  - `review_packet.json`
  - `operator_handoff.md`

### 3.3 Tasks

#### Task 1: Document tool manifest v1

**Objective:** 写清 `tool_id`、execution、input/output schema refs、artifact declarations、exit semantics。

**Content requirements:**

```yaml
toolId: chainladder-calc
version: actuarial-reserving.v1
runtime: python
execution:
  kind: cli
  command: python -m reserving_workflow.tools_cli.chainladder_calc
inputs:
  case_input:
    schemaRef: schemas/actuarial-reserving/v1/ReservingCaseInput.schema.json
    artifact: case_input.json
outputs:
  deterministic_result:
    schemaRef: schemas/actuarial-reserving/v1/DeterministicReserveResult.schema.json
    artifact: deterministic_result.json
errors:
  format: tool_error.v1
```

**Verify:** Markdown includes all 7 tool IDs and explicitly says CLI first, HTTP/MCP optional later.

#### Task 2: Add schema export script

**Objective:** 从 Pydantic models 导出 JSON Schema。

**Implementation outline:**

```python
# scripts/export_contract_schemas.py
from pathlib import Path
import json

from reserving_workflow.schemas.core import (
    ReservingCaseInput,
    DeterministicReserveResult,
    NarrativeDraft,
    ConstitutionCheckResult,
    RunArtifactManifest,
)
from reserving_workflow.contracts.control_plane import (
    ToolInvocation,
    Workflow,
    Run,
    RunEvent,
    Review,
)

MODELS = [
    ReservingCaseInput,
    DeterministicReserveResult,
    NarrativeDraft,
    ConstitutionCheckResult,
    RunArtifactManifest,
    ToolInvocation,
    Workflow,
    Run,
    RunEvent,
    Review,
]


def main() -> None:
    out = Path("schemas/actuarial-reserving/v1")
    out.mkdir(parents=True, exist_ok=True)
    for model in MODELS:
        path = out / f"{model.__name__}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
```

**Test:** `tests/test_contract_schema_export.py` should run script in a temp checkout or validate generated files exist and contain `$defs`/`properties` as expected.

**Verify:**

```bash
python scripts/export_contract_schemas.py
python -m pytest tests/test_contract_schema_export.py -q
```

#### Task 3: Add golden artifact fixture

**Objective:** 提供一个可被 `ai_actuary` 和 `ai_interface` 共同消费的最小 golden run。

**Fixture requirements:**

- `case_input.json` uses stable sample case, e.g. `case_id: golden-raa` and `metadata.chainladder_sample: RAA`.
- `deterministic_result.json` has `reserve_summary.latest_diagonal`, `ultimate`, `ibnr` numeric fields.
- `narrative_draft.json` cites the same numeric values.
- `constitution_check.json` is either `pass` or `review_required`, with deterministic reasons.
- `run_manifest.json` maps artifact IDs to relative paths where possible.

**Verify:** Test loads all fixtures through Pydantic models.

### 3.4 Acceptance criteria

- `python scripts/export_contract_schemas.py` succeeds.
- `python -m pytest tests/test_contract_schema_export.py -q` passes.
- Full suite: `python -m pytest tests -q` passes.
- Docs clearly state `narrative-draft` is a first-class tool.
- No runtime behavior changes.

---

## 4. PR2 — Python CLI entrypoints for each tool

**Repo:** `ai_actuary`  
**Branch:** `feat/tool-cli-entrypoints`  
**Commit type:** `feat`, `test`  
**Non-goals:** 不引入 pipeline runner，不改 `ai_interface`，不新增 HTTP。

### 4.1 Objective

把现有 Python 内部函数包装成稳定 CLI 工具，统一 JSON 文件输入、artifact 输出、错误 JSON、退出码。

### 4.2 Files

- Create package: `src/reserving_workflow/tools_cli/`
  - `__init__.py`
  - `_io.py`
  - `_errors.py`
  - `chainladder_calc.py`
  - `narrative_draft.py`
  - `constitution_check.py`
  - `review_generator.py`
  - `replay_run.py`
  - `repeatability_check.py`
  - `report_export.py`
- Modify: `pyproject.toml` if console scripts are wanted.
- Tests:
  - `tests/test_tool_cli_chainladder_calc.py`
  - `tests/test_tool_cli_narrative_draft.py`
  - `tests/test_tool_cli_constitution_check.py`
  - `tests/test_tool_cli_review_generator.py`
  - `tests/test_tool_cli_replay_repeatability.py`
  - `tests/test_tool_cli_report_export.py`

### 4.3 Common CLI contract

Every command supports:

```bash
python -m reserving_workflow.tools_cli.<tool_name> \
  --input ./input.json \
  --artifact-root ./tmp/artifacts/run-123 \
  --output ./tmp/artifacts/run-123/<artifact>.json \
  --manifest ./tmp/artifacts/run-123/run_manifest.json \
  --json
```

Rules:

- Success exits `0` and writes a JSON summary to stdout.
- User/data validation errors exit `2` and write `tool_error.json`.
- Runtime/internal errors exit `1` and write `tool_error.json`.
- All paths in stdout are absolute or manifest-relative by documented rule; do not mix silently.
- Do not duplicate business logic inside CLI modules; call existing calculators/constitution/replay/report functions.

### 4.4 Tasks

#### Task 1: Add shared CLI IO helpers

**Objective:** Centralize JSON read/write, path resolution, error envelope, and manifest updates.

**Files:**

- Create: `src/reserving_workflow/tools_cli/_io.py`
- Create: `src/reserving_workflow/tools_cli/_errors.py`
- Test: `tests/test_tool_cli_io.py`

**Test cases:**

- Read valid JSON file.
- Missing file returns stable error category.
- Write artifact creates parent dirs.
- Manifest update preserves existing artifact paths.

#### Task 2: Add `chainladder-calc` CLI

**Objective:** Wrap `ChainladderAdapter().calculate(...)`.

**Files:**

- Create: `src/reserving_workflow/tools_cli/chainladder_calc.py`
- Test: `tests/test_tool_cli_chainladder_calc.py`

**Verification commands:**

```bash
python -m reserving_workflow.tools_cli.chainladder_calc --help
python -m reserving_workflow.tools_cli.chainladder_calc \
  --input tests/fixtures/tool_contracts/golden_run/case_input.json \
  --artifact-root tmp/tool-cli-smoke/golden \
  --output tmp/tool-cli-smoke/golden/deterministic_result.json \
  --manifest tmp/tool-cli-smoke/golden/run_manifest.json \
  --json
python -m pytest tests/test_tool_cli_chainladder_calc.py -q
```

#### Task 3: Add `narrative-draft` CLI

**Objective:** Extract current `_build_narrative_draft` behavior into reusable public helper and CLI.

**Files:**

- Create or modify: `src/reserving_workflow/narrative.py`
- Create: `src/reserving_workflow/tools_cli/narrative_draft.py`
- Test: `tests/test_tool_cli_narrative_draft.py`

**Important:** Do not leave narrative generation as private helper inside `case_worker.py` only.

#### Task 4: Add `constitution-check` CLI

**Objective:** Wrap `evaluate_case_constitution(...)` using artifact files as inputs.

**Files:**

- Create: `src/reserving_workflow/tools_cli/constitution_check.py`
- Test: `tests/test_tool_cli_constitution_check.py`

**Acceptance:** Given golden `case_input`, `deterministic_result`, and `narrative_draft`, emits valid `ConstitutionCheckResult`.

#### Task 5: Add `review-generator` CLI

**Objective:** Wrap existing `review_worker.build_review_packet(...)` but accept artifact paths/constitution result rather than requiring private worker result objects.

**Files:**

- Modify or adapter: `workflows/agent-runtimes/hermes-worker/review_worker.py`
- Create: `src/reserving_workflow/tools_cli/review_generator.py`
- Test: `tests/test_tool_cli_review_generator.py`

**Pitfall:** Keep generated `review_packet.md` deterministic enough for tests.

#### Task 6: Add `replay-run` and `repeatability-check` CLIs

**Objective:** Wrap `replay_case_from_manifest` and `compare_repeatability`.

**Files:**

- Create: `src/reserving_workflow/tools_cli/replay_run.py`
- Create: `src/reserving_workflow/tools_cli/repeatability_check.py`
- Modify existing `scripts/replay_case.py` / `scripts/compare_repeatability.py` only if needed to delegate to new CLI internals.
- Test: `tests/test_tool_cli_replay_repeatability.py`

#### Task 7: Add `report-export` CLI wrapper alignment

**Objective:** Align existing `scripts/export_run_report.py` with the common tool CLI contract.

**Files:**

- Create: `src/reserving_workflow/tools_cli/report_export.py`
- Modify: `scripts/export_run_report.py` to delegate to CLI internals if appropriate.
- Test: `tests/test_tool_cli_report_export.py`

### 4.5 Acceptance criteria

- Every CLI supports `--help`.
- Every CLI has focused subprocess tests.
- Each tool writes expected artifacts and updates/uses manifest consistently.
- `python -m pytest tests/test_tool_cli_*.py -q` passes.
- `python -m pytest tests -q` passes.

---

## 5. PR3 — Local tool runner inside ai_actuary

**Repo:** `ai_actuary`  
**Branch:** `feat/tool-artifact-runner`  
**Commit type:** `feat`, `test`  
**Non-goals:** 不做 TypeScript UI，不做 external queue，不做 HTTP services。

### 5.1 Objective

在 Python 侧提供一个最小 runner，用 manifest 定义顺序执行多个 CLI 工具，方便 `ai_interface` 接入前先在 `ai_actuary` 自证 pipeline 语义。

### 5.2 Files

- Create: `src/reserving_workflow/tool_runner/__init__.py`
- Create: `src/reserving_workflow/tool_runner/contracts.py`
- Create: `src/reserving_workflow/tool_runner/runner.py`
- Create: `src/reserving_workflow/tool_runner/catalog.py`
- Create: `scripts/run_tool_pipeline.py`
- Create: `tests/test_tool_runner.py`
- Create: `tests/fixtures/tool_pipelines/actuarial_reserving_review.yaml`

### 5.3 Minimal pipeline YAML

```yaml
pipelineId: actuarial-reserving-review
version: actuarial-reserving.v1
artifactRoot: tmp/pipeline-runs/{{run_id}}
steps:
  - id: calc
    toolId: chainladder-calc
    inputs:
      case_input: case_input.json
    outputs:
      deterministic_result: deterministic_result.json
  - id: narrative
    toolId: narrative-draft
    inputs:
      case_input: case_input.json
      deterministic_result: deterministic_result.json
    outputs:
      narrative_draft: narrative_draft.json
  - id: governance
    toolId: constitution-check
    inputs:
      case_input: case_input.json
      deterministic_result: deterministic_result.json
      narrative_draft: narrative_draft.json
    outputs:
      constitution_check: constitution_check.json
  - id: review
    toolId: review-generator
    when: "steps.governance.outputs.status == 'review_required'"
  - id: export
    toolId: report-export
```

### 5.4 Tasks

1. Define runner contracts: step status, artifact refs, command result, error envelope.
2. Add command execution wrapper around subprocess with captured stdout/stderr/log file.
3. Add simple template resolver for prior-step artifact paths; keep it intentionally small.
4. Add conditional `when` support only for known simple conditions; avoid general eval.
5. Add `scripts/run_tool_pipeline.py`.
6. Add tests for success path, step failure, skipped review step, and artifact/log collection.

### 5.5 Acceptance criteria

- `python scripts/run_tool_pipeline.py --pipeline tests/fixtures/tool_pipelines/actuarial_reserving_review.yaml --input tests/fixtures/tool_contracts/golden_run/case_input.json --artifact-root tmp/pipeline-smoke --json` succeeds.
- Runner records per-step stdout/stderr/exit code.
- Failure in one step stops downstream dependent steps and returns stable error JSON.
- Focused tests and full suite pass.

---

## 6. PR4 — ai_interface minimal SkillManifest + CLI Executor

**Repo:** `ai_interface`  
**Branch:** `feat/skill-manifest-cli-executor`  
**Commit type:** `feat`, `test`  
**Non-goals:** 不实现完整 visual pipeline，不重写 Python logic，不做 HTTP/MCP。

### 6.1 Objective

让 `ai_interface` 能读取一个 skill manifest，执行一个 CLI 工具，收集 artifacts/logs，并把结果暴露给 API/frontend。

### 6.2 Files, tentative

Actual paths must be confirmed in `ai_interface` before implementation. Expected areas:

- Create: `lib/api-spec/openapi.yaml` additions for skill execution endpoints.
- Create: `artifacts/api-server/src/skills/manifest.ts`
- Create: `artifacts/api-server/src/skills/cli-executor.ts`
- Create: `artifacts/api-server/src/skills/artifacts.ts`
- Create: `artifacts/api-server/src/routes/skills.ts`
- Create: `skills/community/actuarial/chainladder-calc/skill.yaml`
- Create: tests under API server package.
- Update generated client/Zod schemas after OpenAPI changes.

### 6.3 Minimal API surface

```http
GET /api/skills
GET /api/skills/{skillId}
POST /api/skill-runs
GET /api/skill-runs/{runId}
GET /api/skill-runs/{runId}/artifacts
```

### 6.4 Skill manifest example

```yaml
skillId: chainladder-calc
version: actuarial-reserving.v1
name: Chainladder 准备金计算
category: actuarial
execution:
  kind: cli
  command: python
  args:
    - -m
    - reserving_workflow.tools_cli.chainladder_calc
requiredEnv: []
inputSchemaRef: schemas/actuarial-reserving/v1/ReservingCaseInput.schema.json
outputs:
  deterministic_result:
    artifact: deterministic_result.json
    schemaRef: schemas/actuarial-reserving/v1/DeterministicReserveResult.schema.json
```

### 6.5 Tasks

1. Inspect current `ai_interface` package layout and tests.
2. Add manifest parser with strict validation.
3. Add CLI executor that runs commands in a configured working directory with timeout and safe env allowlist.
4. Add artifact store under a local run directory.
5. Add OpenAPI routes and regenerate types.
6. Add a fixture-based smoke that executes `chainladder-calc` from `ai_actuary` checkout or a configured path.
7. Add minimal UI/backstage panel only if the existing frontend shell has a clear place; otherwise API-only PR is acceptable.

### 6.6 Acceptance criteria

- `pnpm run typecheck` passes.
- API tests pass.
- OpenAPI codegen is updated.
- A local skill run can execute `chainladder-calc` and return artifact refs.
- No provider/model/embedding complexity is exposed to ordinary frontend users.

---

## 7. PR5 — ai_interface actuarial pipeline runner and Backstage visibility

**Repo:** `ai_interface`  
**Branch:** `feat/actuarial-pipeline-runner`  
**Commit type:** `feat`, `test`  
**Non-goals:** 不做 production queue/auth/RBAC，不做 arbitrary code execution beyond registered skills。

### 7.1 Objective

把多个 skill run 串成一个 pipeline，让 Foreground 显示最终业务结果，Backstage 显示每一步输入、输出、日志、制品和状态。

### 7.2 Files, tentative

- Create: `artifacts/api-server/src/pipelines/manifest.ts`
- Create: `artifacts/api-server/src/pipelines/runner.ts`
- Create: `artifacts/api-server/src/routes/pipelines.ts`
- Create: `pipelines/actuarial-reserving-review.yaml`
- Modify frontend sandbox to show pipeline run cards / backstage artifact panel if in scope.
- Tests for runner and API routes.

### 7.3 Pipeline manifest

```yaml
pipelineId: actuarial-reserving-review
name: 精算准备金审查
version: actuarial-reserving.v1
steps:
  - id: calc
    skill: chainladder-calc
  - id: narrative
    skill: narrative-draft
    inputs:
      deterministic_result_path: "{{steps.calc.artifacts.deterministic_result}}"
  - id: governance
    skill: constitution-check
    inputs:
      deterministic_result_path: "{{steps.calc.artifacts.deterministic_result}}"
      narrative_draft_path: "{{steps.narrative.artifacts.narrative_draft}}"
  - id: review
    skill: review-generator
    when: "{{steps.governance.outputs.status == 'review_required'}}"
  - id: export
    skill: report-export
```

### 7.4 Tasks

1. Add pipeline manifest schema.
2. Add ordered step runner over existing Skill Executor.
3. Add artifact path interpolation with a deliberately small templating surface.
4. Add conditional step support for review-required path.
5. Add API routes to start/list/get pipeline runs.
6. Add Backstage payload shape: step status, duration, stdout/stderr logs, input artifacts, output artifacts, error envelope.
7. Add frontend/API smoke: run golden case, inspect calc/narrative/constitution artifacts.

### 7.5 Acceptance criteria

- Golden pipeline produces deterministic result, narrative draft, constitution check, and handoff/report artifacts.
- Backstage can inspect every intermediate artifact.
- Review step is skipped on pass and executed on review_required.
- Frontend remains simple: ordinary user sees “run complete / needs review / failed” and main output; detailed artifacts are behind Backstage/inspect.

---

## 8. PR6 — Cross-repo compatibility suite

**Repos:** likely both `ai_actuary` and `ai_interface`  
**Branches:** `feat/tool-contract-compat-suite`  
**Commit type:** `test`, `docs`

### 8.1 Objective

防止两个 repo 的契约解释漂移。用同一批 golden fixtures 证明 Python 与 TypeScript 对 schema、artifact manifest、tool result 的理解一致。

### 8.2 Options

Option A: Copy versioned fixture package into both repos.  
Option B: Publish/consume a small contract artifact package.  
Option C: Pin one repo path in local test config for integration tests.

Recommended first version: **Option A with explicit version and checksum**, because it keeps PR small and avoids package publishing流程。

### 8.3 Acceptance criteria

- `ai_actuary` can regenerate schemas and fixtures.
- `ai_interface` can validate same fixtures against consumed schemas.
- A compatibility test fails if required artifact IDs change silently.
- Docs state how to bump `actuarial-reserving.v1` to `v2`.

---

## 9. PR7 — Optional HTTP adapter for chainladder-calc

**Repo:** `ai_actuary` or separate runtime package  
**Branch:** `feat/optional-chainladder-http-adapter`  
**Only start if:** CLI pipeline is proven and there is real need for lower startup overhead, concurrency, health checks, or long-lived service deployment.

### 9.1 Objective

Expose the same core `chainladder-calc` function over HTTP without changing tool semantics.

### 9.2 Non-goals

- Do not make every tool a microservice.
- Do not replace artifact manifest semantics.
- Do not duplicate chainladder business logic.

### 9.3 Acceptance criteria

- `POST /calculate` accepts same schema as CLI input.
- Response is identical to `deterministic_result.json` contract.
- Health endpoint exists.
- CLI and HTTP adapter share the same core function tests.

---

## 10. PR8 — Optional MCP adapter

**Repo:** TBD  
**Branch:** `feat/optional-mcp-tools-adapter`  
**Only start if:** User wants these tools discoverable by multiple agent frameworks beyond `ai_interface`.

### 10.1 Objective

Expose selected tools through MCP while preserving Pydantic/schema/artifact contracts.

### 10.2 Acceptance criteria

- MCP tool list includes stable tool IDs.
- Tool calls write the same artifacts as CLI execution.
- MCP adapter has no independent business logic.

---

## 11. Validation gates for every implementation PR

For `ai_actuary` PRs:

```bash
cd /home/ec2-user/work/ai_actuary
python -m pytest tests -q
```

For focused areas:

```bash
python -m pytest tests/test_tool_cli_*.py -q
python -m pytest tests/test_tool_runner.py -q
python -m pytest tests/test_contract_schema_export.py -q
```

For API/console-touched surfaces in `ai_actuary`, additionally run uvicorn and smoke relevant endpoints. For `ai_interface`, follow repo commands after inspection, expected baseline:

```bash
cd /home/ec2-user/work/ai_interface
pnpm run typecheck
pnpm run build
```

If frontend behavior changes, start local services and browser-click key interactions; do not rely only on static route inspection.

---

## 12. Project development management skill snapshot

This plan follows the currently loaded project management/development skills:

### 12.1 `actuarial-ai-project-workflows`

Key rules now active for this project family:

- Use this skill for `AI_actuarial_inforsearch` and `ferryhe/ai_actuary` work.
- Confirm repo/branch/open state before acting.
- Keep one PR to one clear vertical slice.
- Use focused tests and full suite before PR-visible changes.
- For console/API/report surfaces, combine API/TestClient checks with real service/browser smoke when user-facing behavior matters.
- For review comments, use the “old method”: fetch all comments/threads, validate each, fix only correct scoped items, rerun tests, push, resolve only truly fixed threads.
- Tool decomposition guidance is now explicitly part of the skill: treat `ai_actuary` as Python actuarial tool runtime; let `ai_interface` orchestrate; start with CLI/file artifacts; HTTP/MCP later.

### 12.2 `writing-plans`

Key rules used in this document:

- Plans should be implementation-ready for a capable developer with little repo context.
- Use bite-sized tasks, exact paths, exact commands, expected outputs, and acceptance criteria.
- Prefer TDD: failing test → minimal implementation → pass → broader verification.
- For multi-repo module architecture, first PR should be contract/docs/fixtures-only before runtime implementation.
- Do not commit the plan until explicitly approved; writing the local MD and reporting changed files is okay.

---

## 13. Recommended immediate next action

Start with **PR1: `docs/tool-contract-manifest-v1` in `ai_actuary`**.

Reason:

1. It is low-risk and reviewable.
2. It resolves the most important architectural question first: exact tool/artifact/schema boundaries.
3. It gives `ai_interface` a stable target before any executor/UI work begins.
4. It prevents premature HTTP/MCP/service work.

Suggested first branch after this planning branch:

```bash
git switch main
git pull --ff-only origin main
git switch -c docs/tool-contract-manifest-v1
```

Then implement PR1 only. Do not start PR2 until PR1 contract review is accepted.
