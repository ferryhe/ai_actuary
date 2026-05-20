from __future__ import annotations

from reserving_workflow.artifacts.replay import compare_repeatability, load_manifest
from reserving_workflow.tools_cli._common import ToolArgumentParser, manifest_artifact_dir, parse_args, resolve_output_path, run_tool, write_json

TOOL_ID = "repeatability-check"


def build_parser() -> ToolArgumentParser:
    parser = ToolArgumentParser(description="Compare repeatability across multiple run manifests.")
    parser.add_argument("--run-manifest", required=True, nargs="+", help="One or more run_manifest.json paths")
    parser.add_argument("--output", default=None, help="Optional output path for stability_report.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parse_args(TOOL_ID, build_parser(), argv)

    def _action():
        manifest_paths = list(args.run_manifest)
        first_manifest = load_manifest(manifest_paths[0])
        output_path = resolve_output_path(
            args.output,
            default_dir=manifest_artifact_dir(manifest_paths[0], first_manifest.model_dump(mode="json")),
            filename="stability_report.json",
        )
        payload = compare_repeatability(manifest_paths)
        write_json(output_path, payload)
        return {"stability_report": output_path}

    return run_tool(TOOL_ID, _action)


if __name__ == "__main__":
    raise SystemExit(main())
