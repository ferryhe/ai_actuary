from __future__ import annotations

from pathlib import Path

from reserving_workflow.calculators import ChainladderAdapter
from reserving_workflow.schemas import ReservingCaseInput
from reserving_workflow.tools_cli._common import ToolArgumentParser, load_model, parse_args, resolve_output_path, run_tool, write_model

TOOL_ID = "chainladder-calc"


def build_parser() -> ToolArgumentParser:
    parser = ToolArgumentParser(description="Run deterministic chainladder calculation from case_input.json.")
    parser.add_argument("--case-input", required=True, help="Path to case_input.json")
    parser.add_argument("--output", default=None, help="Optional output path for deterministic_result.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parse_args(TOOL_ID, build_parser(), argv)

    def _action():
        case_input = load_model(args.case_input, ReservingCaseInput)
        output_path = resolve_output_path(args.output, default_dir=Path(args.case_input).parent, filename="deterministic_result.json")
        result = ChainladderAdapter().calculate(case_input)
        write_model(output_path, result)
        return {"deterministic_result": output_path}

    return run_tool(TOOL_ID, _action)


if __name__ == "__main__":
    raise SystemExit(main())
