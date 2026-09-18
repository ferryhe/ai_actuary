# 项目长期记忆：ai_actuary

## 项目约定

- 用户使用中文交流。

## 启动本地控制台

- `python scripts/run_local_workbench.py` → Console/API `http://127.0.0.1:8000/console`，ADK Web `http://127.0.0.1:8001`；可加 `--api-port/--adk-port/--disable-adk`。
- ADK 依赖：Python 3.11 + `google-adk==2.7.1`。冷启动 30-40s。诊断日志 `tmp/local-workbench-diagnostics/launcher.jsonl`。
- launcher 用 Job Object 管进程树；重启前先查 `Get-NetTCPConnection -State Listen -LocalPort 8000,8001`。
- 握手：无 tty（`Start-Process`）时不会提示，须 `python tmp/approve_handoff.py <handoff_id>`；或启动前设 `$env:AI_ACTUARY_OPERATOR_BOOTSTRAP_TOKEN`。`handoff/request` 的 claim_token 要够长（`secrets.token_urlsafe(24)`）。

## 编辑大文件时的行尾陷阱（重要）

- `src/reserving_workflow/api/app.py`、`tests/test_api_control_plane.py` 提交时行尾是 **`\r\r\n`**（CR CRLF）；`console.html` 是 LF。
- `replace_in_file` 会把整文件重写成 CRLF → 改用**字节级脚本**：`raw.replace(b"\r\r\n", b"\n")` 得到干净文本 → 断言 `count == 1` 精确替换 → 写回 `clean.replace("\n", "\r\r\n")`。判据 `raw.count(b"\r") == 2 * raw.count(b"\n")`。改完 `git diff HEAD --numstat` 核对。
- pytest 报的行号 = 真实行号 ×2（Python 把单独 `\r` 当换行）；用 ripgrep 行号定位。
- PowerShell 写多行 python 用 here-string + `Set-Content -Encoding ascii`，不要用 `python -c`。

## 控制面契约（2026-09-18 起）

- 产物目录改为 `<artifact_root>/<case_id>/<run_id>`；rerun 强制新目录（原共用目录会覆盖 `run_manifest.json`/`review_packet.json` → identity mismatch）。
- `create_run` 顶部只生成一次 run_id，三条分支复用。
- 聚合面（`/console/state`、`/reviews`）降级不 409：`degraded:{active, review_entries[]}`，单条降级 `review_unavailable`；单条面（`/runs/{id}/review`）仍 409。
- 握手守卫改为探测 `/console/state` 的 401/403；handoff 窗口 = 进程生命周期（仅 `exchange_bootstrap` 单次 + TTL）。

## 环境与运行时踩坑

- **`.env` 里不用的变量要整行注释**，别写 `KEY=`（空 `OPENAI_BASE_URL=` 会让 SDK 请求 URL 缺协议 → `Connection error.`，registry 记 `error_category: planner_runtime`）。
- 排查模型调用失败要打印 `__cause__`/`__context__` 链；curl 通而 SDK 不通 = base_url/代理/证书问题。
- planner 链路：`workflows/agent-runtimes/openai-agents/`（openai-agents 0.17.6 + openai 2.43.0），按文件路径运行时加载，改完即时生效；失败时产物目录只有 `validated_input.json`。
- 查失败 run：`python tmp/inspect_failed.py <run_id>`。
- `operator_entrypoint.py` 无 `if __name__ == "__main__"` 守卫，`python -m` 会静默退出；CLI 要自己 `load_dotenv('.env')`，且加 `--registry-path tmp/run-registry.json` 才会进控制台。

## 模型配置（三个独立的模型位）

- **Planner（编排器）**：`AI_ACTUARY_PLANNER_MODEL`，默认 `gpt-5.6-luna`（`workflows/agent-runtimes/openai-agents/config.py`，import 时读取 → **改后必须重启**）。原为硬编码 `gpt-4.1-mini`。
- **Reviewer（AI review）**：`AI_ACTUARY_REVIEW_MODEL` / `_BASE_URL` / `_API_KEY`（`review/ai_reviewer.py`，**每次调用读 env → 改 `.env` 不用重启**）。代码默认 `gpt-5.6-luna`（`DEFAULT_REVIEW_MODEL`）；本仓库 `.env` / `.env.sample` 配 **DeepSeek `deepseek-flash`** + `https://api.deepseek.com/v1`。**2026-09-18 起 DeepSeek 模型 id 为 `deepseek-flash`，`deepseek-chat` 已废弃**（含 `deepseek/` 前缀的 ADK 写法同理）。未设 `AI_ACTUARY_REVIEW_API_KEY` 时，若模型/端点含 `deepseek` 则回退 `DEEPSEEK_API_KEY`，否则 `OPENAI_API_KEY`。任意 OpenAI 兼容端点可用。
- **ADK 聊天**：`AI_ACTUARY_ADK_MODEL`，默认 `gpt-5.6-luna`（`developer_workflows/ai_actuary_developer/agent.py:16`）；`deepseek/` 前缀走 litellm，需 `DEEPSEEK_API_KEY`；Gemini 需 `GOOGLE_API_KEY`。不碰 run 结果。
- **Executor（Hermes worker）默认不调用模型**（确定性执行），无需配置。
- **Narrative（可选第四个模型位，`ai_narrative.py`，2026-09-18 新增，同日改为**默认开启**）**：`AI_ACTUARY_NARRATIVE_ENABLED`（默认 `1`，`resolve_llm_settings(default_enabled="1")`；设 `0` 或 CLI `--narrative-model off` 回到模板）、`AI_ACTUARY_NARRATIVE_MODEL` / `_BASE_URL` / `_API_KEY`（Key 回退规则同 reviewer）、`_MAX_TOKENS`(1200)、`_TIMEOUT_SECONDS`(60)；每次调用读 env。模型只能改写 summary/key_points **措辞**，`cited_values` 永远拷贝确定性结果；护栏 `numbers_supported()` 要求文中每个数字都在证据里（允许少写小数位、年份豁免、千分位容错、case id 数字），否则降级模板 `reason=numeric_guard_rejected`。接入点：`narrative.build_narrative_draft_with_meta(narrative_writer=...)`（chainladder/Hermes worker）与 `ai_narrative.draft_narrative_dict(...)`（experience study runner）。CLI：`--narrative-model auto|on|off`。
  - **坑**：pipeline runner 会把工具 outputs 的每个 key 当成"必需产物文件"去校验 → CLI 的 meta 不能放进 outputs，改写字车文件 `narrative_draft.meta.json`。
- gpt-5 系列只接受 `max_completion_tokens`，不接受 `max_tokens`（ai_reviewer 已做 fallback）；DeepSeek 用 `max_tokens`。

## 文档体系（2026-09-18 起）

- `README.md`（EN）+ `README.zh-CN.md`（ZH 全量）。
- `docs/operations-manual.md` + `operations-manual.zh-CN.md`：端到端操作手册（两条链路 mermaid、逐步交接表、review/AI review 来源、配置表、排障）。
- `docs/adk-operations-manual.md` + `.zh-CN.md`：ADK 开发面（是什么/配置/三组工具/执行规则/禁用项/冒烟模式）。
- `docs/README.md` 是文档索引。
- **`tests/test_documentation_handoff.py` 会锁 README 字面词**：`Calculation Core`、`OpenAI Planner`、`Hermes Workers`、`Step-by-Step Operating Guide`、`Human Responsibilities vs Agent Responsibilities`、`scripts/run_batch_benchmark.py` 等必须存在，改 README 时别删。

## Review 链路（2026-09-18 扩展后）

- 触发：console/API `review_threshold_origin_count`（chainladder 专用）或通用 `review_thresholds`（dict，任意工具）→ `run_config["review_thresholds"]` → `evaluate_case_constitution`（`constitution/engine.py`）：`diagnostics[metric] > threshold` 或 `required_artifacts` 缺失 → `review_required`；hard_constraints → `fail`；否则 `pass`。
- **experience study 已纳入**（2026-09-18）：`model_tools/runner.py` 产出 diagnostics（`row_count/group_count/result_count/zero_denominator_count/low_credibility_count/max_ae_ratio/min_ae_ratio/actual_claim_count_total/expected_claim_count_total`），走同一个 constitution engine；默认阈值 `{zero_denominator_count: 0, low_credibility_count: 0, max_ae_ratio: 5}`。engine 的 hard constraint 现在把 `metadata["tool_id"]` 视为"已有输入"的证据。`ae_small` 样本必然触发（4 个零分母组 + 8 条低可信度结果）。
- **AI review**（`review/ai_reviewer.py`，2026-09-18 新增）：仅 `needs_review` 时调用，落 `ai_review.json` + `ai_review.md`，并回写 `review_packet.json/.md`（新增 `ai_review` 与 `ai_suggestion` 字段，console 的 "AI guidance" 区块直接渲染）。只给提示，禁止改数字；失败降级为 `status=failed` + error，不影响 run 状态。
  - 配置（**每次调用读 env，改 .env 不需重启**）：`AI_ACTUARY_REVIEW_MODEL`（默认 gpt-5.6-luna）、`AI_ACTUARY_REVIEW_BASE_URL` / `AI_ACTUARY_REVIEW_API_KEY`（回退 `OPENAI_*`，可指向任何 OpenAI 兼容 provider）、`AI_ACTUARY_AI_REVIEW_ENABLED`、`AI_ACTUARY_REVIEW_MAX_TOKENS`（4000）、`AI_ACTUARY_REVIEW_TIMEOUT_SECONDS`（60）。
  - 坑：reasoning 模型 `max_completion_tokens` 不足会返回空 JSON → 已加"空响应重试一次"，仍空则 `empty_model_response`。token 参数先试 `max_completion_tokens`，报不支持再退回 `max_tokens`。
  - 接入点：`operator_entrypoint._attach_ai_review`（chainladder 路径，delivery 之前）与 `model_tools/runner.build_experience_study_review_packet`。`review_packet` 没有 `packet_paths.json` 时直接跳过（否则会给测试里的假 packet 补路径，破坏 delivery 失败用例）。
- Inbox 卡片：`_review_inbox_payload` 按注册表逐条取 review，`review_required=True` 或 `status="needs_review"` 才有票；`tmp/reviews` 里的历史记录若对应 run 不在注册表则不显示。决策：`POST /reviews/{review_id}/decision`。
- 改 `console.html` 后必须同步 `tests/test_operator_console_assets.py` 的三个常量（字符数/字节数/sha256），否则该用例失败。
- 历史坑（已修）：`GovernedCaseSummary.cited_values` 原为 `dict[str, float]`，而 worker 诊断含 str/bool → 校验失败导致整条 `planner_runtime`；已改 `dict[str, float|str|bool]`。

## 已知的既有测试失败（环境原因，与改动无关）

- `tests/test_tool_contract_compat_manifest.py`（行尾导致 sha256 不符）
- `tests/test_adk_developer_foundation.py` 的两个 adk 用例
