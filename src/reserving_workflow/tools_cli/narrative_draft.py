from __future__ import annotations

from pathlib import Path

from reserving_workflow.ai_narrative import NarrativeDraftWriter, narrative_llm_settings
from reserving_workflow.narrative import build_narrative_draft_with_meta
from reserving_workflow.schemas import DeterministicReserveResult, ReservingCaseInput
from reserving_workflow.tools_cli._common import (
    ToolArgumentParser,
    load_model,
    parse_args,
    resolve_output_path,
    run_tool,
    write_json,
    write_model,
)

TOOL_ID = "narrative-draft"


def build_parser() -> ToolArgumentParser:
    parser = ToolArgumentParser(description="Build narrative_draft.json from case and deterministic artifacts.")
    parser.add_argument("--case-input", required=True, help="Path to case_input.json")
    parser.add_argument("--deterministic-result", required=True, help="Path to deterministic_result.json")
    parser.add_argument("--output", default=None, help="Optional output path for narrative_draft.json")
    parser.add_argument(
        "--narrative-model",
        choices=("auto", "on", "off"),
        default="auto",
        help="auto: follow AI_ACTUARY_NARRATIVE_ENABLED; on: force the model slot; off: template only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parse_args(TOOL_ID, build_parser(), argv)

    def _action():
        case_input = load_model(args.case_input, ReservingCaseInput)
        deterministic_result = load_model(args.deterministic_result, DeterministicReserveResult)
        output_path = resolve_output_path(args.output, default_dir=Path(args.case_input).parent, filename="narrative_draft.json")
        draft, meta = build_narrative_draft_with_meta(
            case_input, deterministic_result, narrative_writer=_resolve_writer(args.narrative_model)
        )
        write_model(output_path, draft)
        # The pipeline runner validates every declared output as a file, so
        # drafting metadata goes to a sidecar file instead of the outputs map.
        if meta.get("source") == "model" or meta.get("error"):
            write_json(output_path.with_name("narrative_draft.meta.json"), meta)
        return {"narrative_draft": output_path}

    return run_tool(TOOL_ID, _action)


def _resolve_writer(mode: str) -> NarrativeDraftWriter | None:
    """Resolve the optional narrative model slot for this invocation."""

    settings = narrative_llm_settings()
    if mode == "off":
        return None
    if mode == "on":
        settings = {**settings, "enabled": True}
    return NarrativeDraftWriter(settings)


if __name__ == "__main__":
    raise SystemExit(main())
