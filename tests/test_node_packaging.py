from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACE_STEP_REQUIREMENT = (
    "ace-step @ git+https://github.com/ace-step/ACE-Step-1.5.git@"
    "dce621408bee8c31b4fcf4811682eb9359e1bc94"
)


def test_node_runtime_graph_is_separate_from_controller_project() -> None:
    root_project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    node_project = tomllib.loads((ROOT / "deploy/node/pyproject.toml").read_text())

    assert "node" not in root_project["dependency-groups"]
    assert any(
        dependency.startswith(ACE_STEP_REQUIREMENT + ";")
        for dependency in node_project["project"]["dependencies"]
    )
    assert node_project["tool"]["uv"]["package"] is False
    assert node_project["tool"]["uv"]["sources"]["ace-service"]["path"] == "../.."
    assert (ROOT / "deploy/node/uv.lock").is_file()
