# AI 精算部 / AI Actuary

> 采用 **CAS Core + OpenAI Planner + Hermes Workers** 组合架构构建的智能精算系统。

---

## 当前定位

这个仓库不再只是一个泛化的 “Skill 组合” 设想，而是一个明确的研究与工程项目工作区：

- **CAS Core**：保存 deterministic reserving logic、constitution、benchmark、artifact schema；
- **OpenAI Planner**：使用 OpenAI Agents Python 承担规划、路由、治理决策；
- **Hermes Workers**：使用 Hermes 承担执行、打包、通知、流程经验沉淀。

一句话：**OpenAI 负责想清楚做什么，Hermes 负责把事情做完，CAS Core 负责什么算对。**

---

## 仓库结构

```text
.
├── docs/
│   ├── architecture/
│   └── plans/
├── prompts/
│   └── codex/
└── references/
    └── upstream/
        ├── cas/
        ├── hermes/
        └── openai-agents/
```

### 关键内容

- `docs/plans/openai-hermes-composition-design.md`
  - 组合式设计、角色定义、工作流、项目阶段计划
- `prompts/codex/step-by-step-prompts.md`
  - 分步骤交给 Codex 的开发提示词
- `references/upstream/cas/`
  - CAS 项目原始 proposal、architecture、plan、benchmark 说明
- `references/upstream/openai-agents/`
  - OpenAI Agents Python 核心文档快照
- `references/upstream/hermes/`
  - Hermes 核心能力与开发文档快照

---

## 先看什么

1. 看 `docs/plans/openai-hermes-composition-design.md`
2. 看 `docs/architecture/overview.md`
3. 看 `prompts/codex/step-by-step-prompts.md`
4. 需要背景时回查 `references/upstream/*`

---

## 当前代码骨架

本分支已经建立最小项目骨架，供后续按步骤实现：

- `pyproject.toml`
  - 最小 Python 项目配置；当前仅包含 `pydantic` 与 `pytest`
- `src/reserving_workflow/`
  - `schemas/`：CAS Core 的基础 schema
  - `calculators/`：基于 **CAS 官方 `chainladder-python`** 的 deterministic calculator adapter boundary
  - `artifacts/`、`constitution/`、`review/`、`evaluation/`：最小模块边界
- `workflows/agent-runtimes/openai-agents/`
  - planner 侧占位文件：`agents.py`、`tools.py`、`runner.py`、`routing.py`、`config.py`
- `workflows/agent-runtimes/hermes-worker/`
  - worker 侧占位文件：`task_contracts.py`、`case_worker.py`、`batch_worker.py`、`review_worker.py`、`artifact_packager.py`
- `tests/`
  - 最小单元测试，验证 core schema 与 worker contract 的创建和序列化

当前刻意**不包含**：

- 真实 reserving calculator
- 真实 OpenAI API / OpenAI Agents SDK 接线
- 真实 Hermes CLI / Hermes API 调用
- benchmark runner 与 artifact store 具体实现

---

## 下一步开发方向

- 采用 **CAS 官方 `chainladder-python`** 作为 deterministic calculator 工具层，不自研新的准备金算法模块
- 在本仓库中实现 chainladder adapter boundary，并统一映射到 `DeterministicReserveResult`
- 再实现 constitution rule engine v1
- 然后接 Hermes worker contract 与 OpenAI planner skeleton 到真实运行链路

---

## 说明

当前提交重点是把 **计划、结构、参考材料、最小代码骨架** 放进一个独立项目仓库，便于后续用 Codex / Hermes 逐步实现。
