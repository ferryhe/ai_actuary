"""Isolated, offline ADK 2.7.1 pure-config contract probe."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import yaml


class _OfflineSocket(socket.socket):
    def connect(self, address):  # type: ignore[no-untyped-def]
        del address
        raise RuntimeError("external network is disabled in Workflow Lab validation")

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        del address
        raise RuntimeError("external network is disabled in Workflow Lab validation")


def main() -> int:
    socket.socket = _OfflineSocket  # type: ignore[assignment]
    socket.create_connection = _network_blocked  # type: ignore[assignment]

    from google.adk.agents.llm_agent_config import LlmAgentConfig
    from google.adk.agents.loop_agent_config import LoopAgentConfig
    from google.adk.agents.parallel_agent_config import ParallelAgentConfig
    from google.adk.agents.sequential_agent_config import SequentialAgentConfig

    root = Path(sys.argv[1])
    classes = {
        "LlmAgent": LlmAgentConfig,
        "LoopAgent": LoopAgentConfig,
        "ParallelAgent": ParallelAgentConfig,
        "SequentialAgent": SequentialAgentConfig,
    }
    root_class = ""
    configs: dict[str, dict[str, object]] = {}
    for path in sorted(root.rglob("*.yaml"), key=lambda value: value.as_posix()):
        if path.name == "workflow_policy.yaml":
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        selector = data.get("agent_class")
        classes[selector].model_validate(data)
        relative = path.relative_to(root).as_posix()
        configs[relative] = data
        if relative == "root_agent.yaml":
            root_class = selector
    _validate_declarative_graph(root, configs)
    print(
        json.dumps(
            {
                "ok": True,
                "agent_class": root_class,
                "validated_files": len(configs),
                "model_calls": 0,
                "external_network_calls": 0,
            },
            sort_keys=True,
        )
    )
    return 0


def _validate_declarative_graph(
    root: Path, configs: dict[str, dict[str, object]]
) -> None:
    if "root_agent.yaml" not in configs:
        raise ValueError("root_agent.yaml is missing from declarative graph")
    names: dict[str, str] = {}
    for relative, data in sorted(configs.items()):
        name = data.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"agent name is missing in {relative}")
        previous = names.setdefault(name, relative)
        if previous != relative:
            raise ValueError(
                f"duplicate agent name {name!r} in {previous} and {relative}"
            )

    app_root = root.resolve()
    state: dict[str, str] = {}

    def visit(relative: str) -> None:
        marker = state.get(relative)
        if marker == "visiting":
            raise ValueError(f"declarative agent graph cycle at {relative}")
        if marker == "visited":
            return
        state[relative] = "visiting"
        current = app_root.joinpath(*Path(relative).parts)
        for reference in configs[relative].get("sub_agents") or []:
            if not isinstance(reference, dict):
                raise TypeError(f"invalid sub-agent reference in {relative}")
            config_path = reference.get("config_path")
            if not isinstance(config_path, str) or not config_path:
                raise ValueError(f"config_path is required in {relative}")
            if os.path.isabs(config_path):
                raise ValueError(f"absolute config_path in {relative}")
            referencing_directory = current.parent.resolve()
            resolved = (referencing_directory / config_path).resolve()
            if os.path.commonpath([str(referencing_directory), str(resolved)]) != str(
                referencing_directory
            ):
                raise ValueError(f"config_path escapes referencing directory in {relative}")
            try:
                target = resolved.relative_to(app_root).as_posix()
            except ValueError as exc:
                raise ValueError(f"config_path escapes app root in {relative}") from exc
            if target not in configs:
                raise ValueError(
                    f"missing config_path target {config_path!r} referenced by {relative}"
                )
            visit(target)
        state[relative] = "visited"

    visit("root_agent.yaml")
    unreachable = sorted(set(configs) - set(state))
    if unreachable:
        raise ValueError(f"unreachable declarative agent configs: {unreachable!r}")


def _network_blocked(*args, **kwargs):  # type: ignore[no-untyped-def]
    del args, kwargs
    raise RuntimeError("external network is disabled in Workflow Lab validation")


if __name__ == "__main__":
    raise SystemExit(main())
