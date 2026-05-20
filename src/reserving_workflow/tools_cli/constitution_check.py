from __future__ import annotations

from pathlib import Path

from reserving_workflow.constitution import evaluate_case_constitution
from reserving_workflow.schemas import (
    DeterministicReserveResult,
    NarrativeDraft,
    ReservingCaseInput,
    RunArtifactManifest,
)
from reserving_workflow.tools_cli._common import ToolArgumentParser, load_model, parse_args, resolve_output_path, run_tool, write_model

TOOL_ID = "constitution-check"


def build_parser() -> ToolArgumentParser:
    parser = ToolArgumentParser(description="Evaluate governed constitution checks for a reserving run.")
    parser.add_argument("--case-input", required=True, help="Path to case_input.json")
    parser.add_argument("--deterministic-result", required=True, help="Path to deterministic_result.json")
    parser.add_argument("--narrative-draft", required=True, help="Path to narrative_draft.json")
    parser.add_argument("--run-manifest", default=None, help="Optional path to run_manifest.json")
    parser.add_argument("--output", default=None, help="Optional output path for constitution_check.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parse_args(TOOL_ID, build_parser(), argv)

    def _action():
        case_input = load_model(args.case_input, ReservingCaseInput)
        deterministic_result = load_model(args.deterministic_result, DeterministicReserveResult)
        narrative_draft = load_model(args.narrative_draft, NarrativeDraft)
        run_manifest = load_model(args.run_manifest, RunArtifactManifest) if args.run_manifest else None
        output_path = resolve_output_path(args.output, default_dir=Path(args.case_input).parent, filename="constitution_check.json")
        result = evaluate_case_constitution(case_input, deterministic_result, narrative_draft, run_manifest)
        write_model(output_path, result)
        return {"constitution_check": output_path}

    return run_tool(TOOL_ID, _action)


if __name__ == "__main__":
    raise SystemExit(main())
