from __future__ import annotations

from pathlib import Path

from reserving_workflow.artifacts.replay import load_manifest
from reserving_workflow.calculators import ChainladderAdapter
from reserving_workflow.schemas import ReservingCaseInput
from reserving_workflow.tools_cli._common import ToolArgumentParser, ToolCliError, load_model, manifest_artifact_dir, parse_args, resolve_output_path, run_tool, write_model

TOOL_ID = "replay-run"


def build_parser() -> ToolArgumentParser:
    parser = ToolArgumentParser(description="Replay a saved run_manifest.json and write replayed_result.json.")
    parser.add_argument("--run-manifest", required=True, help="Path to run_manifest.json")
    parser.add_argument("--output", default=None, help="Optional output path for replayed_result.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parse_args(TOOL_ID, build_parser(), argv)

    def _action():
        manifest = load_manifest(args.run_manifest)
        artifact_path = manifest.artifact_paths.get("case_input")
        if not artifact_path:
            raise ToolCliError("run_manifest is missing artifact_paths.case_input", category="validation_error")
        manifest_dir = Path(args.run_manifest).expanduser().resolve().parent
        artifact_root = Path(manifest.artifact_root).expanduser() if manifest.artifact_root else manifest_dir
        if not artifact_root.is_absolute():
            artifact_root = (manifest_dir / artifact_root).resolve()
        else:
            artifact_root = artifact_root.resolve()
        case_input_path = Path(artifact_path).expanduser()
        if not case_input_path.is_absolute():
            case_input_path = (artifact_root / case_input_path).resolve()
        case_input = load_model(case_input_path, ReservingCaseInput)
        output_path = resolve_output_path(
            args.output,
            default_dir=manifest_artifact_dir(args.run_manifest, manifest.model_dump(mode="json")),
            filename="replayed_result.json",
        )
        replayed_result = ChainladderAdapter().calculate(case_input)
        write_model(output_path, replayed_result)
        return {"replayed_result": output_path}

    return run_tool(TOOL_ID, _action)


if __name__ == "__main__":
    raise SystemExit(main())
