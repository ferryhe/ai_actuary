from __future__ import annotations

from reserving_workflow.reports import export_run_report
from reserving_workflow.tools_cli._common import ToolArgumentParser, parse_args, run_tool

TOOL_ID = "report-export"


def build_parser() -> ToolArgumentParser:
    parser = ToolArgumentParser(description="Export operator handoff and reserve summary artifacts for a run.")
    parser.add_argument("--registry-path", required=True, help="Path to the local run registry JSON file.")
    parser.add_argument("--run-id", required=True, help="Run id to export.")
    parser.add_argument("--review-store-dir", default="./tmp/reviews", help="Path to the local review store directory.")
    parser.add_argument("--output-dir", default=None, help="Optional output directory for exported artifacts.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parse_args(TOOL_ID, build_parser(), argv)

    def _action():
        payload = export_run_report(
            registry_path=args.registry_path,
            run_id=args.run_id,
            review_store_root=args.review_store_dir,
            output_dir=args.output_dir,
        )
        exports = payload.get("exports", {})
        return {
            "operator_handoff": exports.get("operator_handoff_markdown"),
            "reserve_summary_json": exports.get("reserve_summary_json"),
            "reserve_summary_markdown": exports.get("reserve_summary_markdown"),
        }

    return run_tool(TOOL_ID, _action)


if __name__ == "__main__":
    raise SystemExit(main())
