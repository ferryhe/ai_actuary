from __future__ import annotations

from pathlib import Path

from reserving_workflow.narrative import build_narrative_draft
from reserving_workflow.schemas import DeterministicReserveResult, ReservingCaseInput
from reserving_workflow.tools_cli._common import ToolArgumentParser, load_model, parse_args, resolve_output_path, run_tool, write_model

TOOL_ID = "narrative-draft"


def build_parser() -> ToolArgumentParser:
    parser = ToolArgumentParser(description="Build narrative_draft.json from case and deterministic artifacts.")
    parser.add_argument("--case-input", required=True, help="Path to case_input.json")
    parser.add_argument("--deterministic-result", required=True, help="Path to deterministic_result.json")
    parser.add_argument("--output", default=None, help="Optional output path for narrative_draft.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parse_args(TOOL_ID, build_parser(), argv)

    def _action():
        case_input = load_model(args.case_input, ReservingCaseInput)
        deterministic_result = load_model(args.deterministic_result, DeterministicReserveResult)
        output_path = resolve_output_path(args.output, default_dir=Path(args.case_input).parent, filename="narrative_draft.json")
        draft = build_narrative_draft(case_input, deterministic_result)
        write_model(output_path, draft)
        return {"narrative_draft": output_path}

    return run_tool(TOOL_ID, _action)


if __name__ == "__main__":
    raise SystemExit(main())
