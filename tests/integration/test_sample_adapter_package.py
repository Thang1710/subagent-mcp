from __future__ import annotations

import ast
import json
import os
import subprocess
import venv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SAMPLE_ROOT = ROOT / "examples" / "sample_adapter"


def _python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run(command: list[str], *, cwd: Path, cache: Path) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"PYTHONPATH", "PYTHONHOME", "UV_CACHE_DIR"}
    }
    environment["UV_CACHE_DIR"] = str(cache)
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_separate_sample_adapter_discovers_and_passes_public_conformance(tmp_path) -> None:
    source = SAMPLE_ROOT / "src" / "subagent_mcp_sample_adapter" / "__init__.py"
    assert source.is_file(), "sample adapter package is missing"
    allowed_imports = {
        "subagent_harness_mcp.adapters",
        "subagent_harness_mcp.contracts",
    }
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "subagent_harness_mcp"
        ):
            assert node.module in allowed_imports
        if isinstance(node, ast.Import):
            assert all(
                not alias.name.startswith("subagent_harness_mcp")
                or alias.name in allowed_imports
                for alias in node.names
            )

    dist = tmp_path / "dist"
    cache = ROOT / ".preview" / "pytest-uv-cache"
    core_build = _run(
        [
            "uv",
            "build",
            "--wheel",
            "--out-dir",
            str(dist),
            str(ROOT),
        ],
        cwd=ROOT,
        cache=cache,
    )
    assert core_build.returncode == 0, core_build.stdout + core_build.stderr
    sample_build = _run(
        [
            "uv",
            "build",
            "--wheel",
            "--out-dir",
            str(dist),
            str(SAMPLE_ROOT),
        ],
        cwd=ROOT,
        cache=cache,
    )
    assert sample_build.returncode == 0, sample_build.stdout + sample_build.stderr
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 2

    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    installed = _run(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            str(_python(environment)),
            *(str(wheel) for wheel in wheels),
        ],
        cwd=tmp_path,
        cache=cache,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    smoke = _run(
        [
            str(_python(environment)),
            "-I",
            "-c",
            """
import asyncio
import json
from subagent_harness_mcp.adapters import run_adapter_conformance
from subagent_harness_mcp.adapters.registry import AdapterRegistry

registry = AdapterRegistry()
registry.discover()
adapter = registry.get('sample-echo')
report = asyncio.run(run_adapter_conformance(
    lambda: adapter,
    workspace_path=WORKSPACE,
    model='sample/exact-model',
    reasoning={'effort': 'sample'},
    transport='managed-sdk',
))
print(json.dumps({
    'runtime_id': report.runtime_id,
    'operations': report.operations,
    'state': report.final_conversation_state,
}))
""".replace("WORKSPACE", repr(str(workspace))),
        ],
        cwd=tmp_path,
        cache=cache,
    )
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    payload = json.loads(smoke.stdout)
    assert payload == {
        "runtime_id": "sample-echo",
        "operations": [
            "probe",
            "resolve_context",
            "spawn",
            "open_session",
            "snapshot",
            "send",
            "interrupt",
            "close",
        ],
        "state": "closed",
    }
