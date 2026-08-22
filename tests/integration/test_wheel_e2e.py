from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import os
import shutil
import site
import subprocess
import sys
import tarfile
import venv
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, TextContent


ROOT = Path(__file__).resolve().parents[2]
DIST_NAME = "subagent-harness-mcp"
PACKAGE_NAME = "subagent_harness_mcp"
VERSION = "0.1.0a22"
SCHEMAS = (
    "config-v1.json",
    "adapter-v1.json",
    "agent-descriptor-v1.json",
    "tools-v1.json",
)


def _python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _console(environment: Path) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return (
        environment
        / ("Scripts" if os.name == "nt" else "bin")
        / f"{DIST_NAME}{suffix}"
    )


def _site_packages(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Lib/site-packages"
    return environment / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"


def _clean_env(**updates: str) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in {"PYTHONPATH", "PYTHONHOME", "SUBAGENT_MCP_HOME"}
    }
    environment.update(updates)
    return environment


@pytest.fixture(scope="session")
def release_distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    configured = os.environ.get("SUBAGENT_MCP_TEST_DIST_DIR")
    dist_dir = (
        Path(configured).resolve()
        if configured
        else tmp_path_factory.mktemp("release-dist")
    )
    if not configured:
        completed = subprocess.run(
            ["uv", "build", "--out-dir", str(dist_dir)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1
    return wheels[0], sdists[0]


@pytest.fixture(scope="session")
def locked_dependency_source() -> Path:
    return next(
        path
        for path in map(Path, site.getsitepackages())
        if (path / "mcp").is_dir()
    )


@pytest.fixture(scope="session")
def installed_artifacts(
    release_distributions: tuple[Path, Path],
    locked_dependency_source: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[tuple[str, Path, Path], ...]:
    uv = shutil.which("uv")
    assert uv is not None
    installed: list[tuple[str, Path, Path]] = []
    for artifact in release_distributions:
        kind = "wheel" if artifact.suffix == ".whl" else "sdist"
        root = tmp_path_factory.mktemp(f"installed-{kind}")
        environment = root / "venv"
        venv.EnvBuilder(with_pip=False).create(environment)
        completed = subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--offline",
                "--no-deps",
                "--python",
                str(_python(environment)),
                str(artifact),
            ],
            cwd=root,
            env=_clean_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        site_packages = _site_packages(environment)
        excluded_prefixes = ("subagent_harness_mcp", "claude_agent_sdk")
        for entry in locked_dependency_source.iterdir():
            folded = entry.name.casefold()
            if (
                folded.endswith(".pth")
                and folded != "pywin32.pth"
            ) or folded.startswith(excluded_prefixes):
                continue
            target = site_packages / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target)
            else:
                shutil.copy2(entry, target)
        assert (site_packages / "mcp").is_dir()
        assert (site_packages / PACKAGE_NAME).is_dir()
        assert not any(
            path.name.casefold().startswith("claude_agent_sdk")
            for path in site_packages.iterdir()
        )
        assert not (site_packages / "_editable_impl_subagent_harness_mcp.pth").exists()
        installed.append((kind, environment, root))
    return tuple(installed)


def _meta(result: CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, TextContent)
    marker = "\n```subagent-mcp-meta\n"
    _, separator, encoded = content.text.partition(marker)
    assert separator == marker
    assert encoded.endswith("\n```")
    return json.loads(encoded[:-4])


def _write_fake_config(home: Path) -> None:
    config_dir = home / "config"
    config_dir.mkdir(parents=True)
    document = {
        "schema_version": 1,
        "revision": 1,
        "runtimes": {
            "fake": {
                "enabled": True,
                "selection_mode": "fixed",
                "fallback": False,
                "variants": [
                    {
                        "id": "release-smoke",
                        "model": "provider/model-release-smoke",
                        "reasoning": {"mode": "native"},
                    }
                ],
            }
        },
    }
    (config_dir / "config.json").write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


async def _fake_stdio_smoke(python: Path, run_root: Path) -> None:
    home = run_root / "product-home"
    workspace = run_root / "workspace"
    workspace.mkdir()
    _write_fake_config(home)
    server_code = """
from subagent_harness_mcp.adapters.fake import FakeAdapter
from subagent_harness_mcp.adapters.registry import AdapterRegistry
from subagent_harness_mcp.config import ConfigStore
from subagent_harness_mcp.paths import resolve_paths
from subagent_harness_mcp.server import create_server
from subagent_harness_mcp.service import SubagentMcpService
from subagent_harness_mcp.store import StateStore

paths = resolve_paths()
registry = AdapterRegistry(builtin_factories=(FakeAdapter,))
registry.discover()
service = SubagentMcpService(config=ConfigStore(paths), store=StateStore.open(paths), registry=registry)
create_server(service).run('stdio')
"""
    parameters = StdioServerParameters(
        command=str(python),
        args=["-I", "-c", server_code],
        env=_clean_env(SUBAGENT_MCP_HOME=str(home)),
        cwd=run_root,
        encoding="utf-8",
        encoding_error_handler="replace",
    )
    async with Client(
        stdio_client(parameters),
        mode="legacy",
        read_timeout_seconds=15,
    ) as client:
        tools = await client.list_tools()
        assert len(tools.tools) == 14
        spawned = await client.call_tool(
            "agent_spawn",
            {
                "request_id": "release-spawn-1",
                "runtime_id": "fake",
                "variant_id": "release-smoke",
                "task": {
                    "title": "Installed artifact smoke",
                    "prompt": "Complete deterministically.",
                    "acceptance_criteria": ["Return one normalized result."],
                    "role": "sub-agent",
                },
                "cwd": str(workspace),
                "mode": "review",
                "transport": "managed-sdk",
                "required_capabilities": ["repo_read"],
                "workspace": "current",
                "response_mode": "full",
            },
        )
        spawn = _meta(spawned)["result"]
        sent = await client.call_tool(
            "agent_send",
            {
                "request_id": "release-send-1",
                "conversation_id": spawn["conversation_id"],
                "prompt": "Continue the same native session.",
                "response_mode": "full",
            },
        )
        follow_up = _meta(sent)["result"]
        assert follow_up["external_session_id"] == spawn["external_session_id"]
        assert (
            follow_up["descriptor"]["model_display_name"]
            == "provider/model-release-smoke"
        )
        closed = await client.call_tool(
            "agent_close",
            {
                "request_id": "release-close-1",
                "conversation_id": spawn["conversation_id"],
                "response_mode": "full",
            },
        )
        assert _meta(closed)["result"]["conversation_state"] == "closed"


@pytest.mark.parametrize("index", (0, 1), ids=("wheel", "sdist"))
def test_installed_artifact_runs_entrypoint_resources_fake_stdio_and_ui(
    installed_artifacts: tuple[tuple[str, Path, Path], ...],
    index: int,
) -> None:
    kind, environment, root = installed_artifacts[index]
    run_root = root / "outside-source-tree"
    run_root.mkdir()
    version = subprocess.run(
        [_console(environment), "--version"],
        cwd=run_root,
        env=_clean_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert version.returncode == 0, version.stdout + version.stderr
    assert version.stdout.strip() == f"{DIST_NAME} {VERSION}"

    resource_and_ui_code = f"""
import http.client
import json
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit
from subagent_harness_mcp.ui import LoopbackUiServer

root = files({PACKAGE_NAME!r})
assert 'Subagent MCP' in root.joinpath('static', 'index.html').read_text(encoding='utf-8')
for name in {SCHEMAS!r}:
    document = json.loads(root.joinpath('schemas', name).read_text(encoding='utf-8'))
    assert document.get('$schema') == 'https://json-schema.org/draft/2020-12/schema'
server = LoopbackUiServer(lambda: {{'health': {{'state': 'ready'}}}}, lambda patch, revision: {{'revision': revision + 1}})
thread = server.start()
try:
    connection = http.client.HTTPConnection(server.bound_host, server.bound_port, timeout=3)
    connection.request('GET', '/', headers={{'Host': server.host_header}})
    response = connection.getresponse()
    body = response.read()
    assert response.status == 200 and b'Subagent MCP' in body
    connection.close()
    token = parse_qs(urlsplit(server.bootstrap_url).fragment)['token'][0]
    connection = http.client.HTTPConnection(server.bound_host, server.bound_port, timeout=3)
    connection.request('POST', '/api/v1/session', headers={{'Host': server.host_header, 'Origin': server.origin, 'X-Subagent-MCP-Token': token}})
    response = connection.getresponse()
    payload = json.loads(response.read())
    assert response.status == 200 and payload['csrf_token']
    connection.close()
finally:
    server.close()
assert not thread.is_alive()
print('installed-ui-ok')
"""
    smoke = subprocess.run(
        [_python(environment), "-I", "-c", resource_and_ui_code],
        cwd=run_root,
        env=_clean_env(SUBAGENT_MCP_HOME=str(root / "ui-home")),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert smoke.returncode == 0, f"{kind}: {smoke.stdout}{smoke.stderr}"
    assert smoke.stdout.strip() == "installed-ui-ok"
    asyncio.run(_fake_stdio_smoke(_python(environment), run_root))


def _archive_members(artifact: Path) -> dict[str, bytes]:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            return {
                name: archive.read(name)
                for name in archive.namelist()
                if not name.endswith("/")
            }
    with tarfile.open(artifact, "r:gz") as archive:
        members: dict[str, bytes] = {}
        for member in archive.getmembers():
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            assert stream is not None
            members[member.name] = stream.read()
        return members


def test_release_archives_are_relative_and_free_of_local_evidence(
    release_distributions: tuple[Path, Path],
) -> None:
    forbidden_content = (
        b"D:\\private-workspace",
        b"C:\\Users\\private-user",
        b"private-user-marker",
        b"sk-ant-",
    )
    forbidden_paths = ("spikes/", "docs/phase0", ".superpowers/", "tests/fixtures/")
    for artifact in release_distributions:
        members = _archive_members(artifact)
        assert members
        for name, payload in members.items():
            normalized = name.replace("\\", "/")
            path = PurePosixPath(normalized)
            assert not path.is_absolute()
            assert ".." not in path.parts
            assert not (path.parts and ":" in path.parts[0])
            assert not any(marker in normalized for marker in forbidden_paths)
            assert not any(marker in payload for marker in forbidden_content)
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert len(digest) == 64 and digest != "0" * 64
