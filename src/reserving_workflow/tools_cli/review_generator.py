from __future__ import annotations

from reserving_workflow.review.generator import build_review_packet_from_artifacts
from reserving_workflow.schemas import ConstitutionCheckResult, DeterministicReserveResult, NarrativeDraft, RunArtifactManifest
from reserving_workflow.tools_cli._common import ToolArgumentParser, load_model, manifest_artifact_dir, parse_args, run_tool

TOOL_ID = "review-generator"


def build_parser() -> ToolArgumentParser:
    parser = ToolArgumentParser(description="Generate review_packet.json and review_packet.md from run artifacts.")
    parser.add_argument("--constitution-check", required=True, help="Path to constitution_check.json")
    parser.add_argument("--deterministic-result", required=True, help="Path to deterministic_result.json")
    parser.add_argument("--narrative-draft", required=True, help="Path to narrative_draft.json")
    parser.add_argument("--run-manifest", required=True, help="Path to run_manifest.json")
    parser.add_argument("--output-dir", default=None, help="Optional output directory for review packet artifacts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parse_args(TOOL_ID, build_parser(), argv)

    def _action():
        constitution_check = load_model(args.constitution_check, ConstitutionCheckResult)
        deterministic_result = load_model(args.deterministic_result, DeterministicReserveResult)
        narrative_draft = load_model(args.narrative_draft, NarrativeDraft)
        run_manifest = load_model(args.run_manifest, RunArtifactManifest)
        output_dir = args.output_dir or manifest_artifact_dir(args.run_manifest, run_manifest.model_dump(mode="json"))
        packet = build_review_packet_from_artifacts(
            constitution_check=constitution_check.model_dump(mode="json"),
            deterministic_result=deterministic_result.model_dump(mode="json"),
            narrative_draft=narrative_draft.model_dump(mode="json"),
            run_manifest=run_manifest.model_dump(mode="json"),
            output_dir=output_dir,
        )
        return {
            "review_packet": packet["packet_paths"]["json"],
            "review_packet_markdown": packet["packet_paths"]["markdown"],
        }

    return run_tool(TOOL_ID, _action)


if __name__ == "__main__":
    raise SystemExit(main())
