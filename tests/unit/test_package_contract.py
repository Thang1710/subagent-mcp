from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import venv
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[2]
DIST_NAME = "subagent-harness-mcp"
PACKAGE_NAME = "subagent_harness_mcp"
VERSION = "1.0.25"


def _read_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _archive_members(artifact: Path) -> dict[str, bytes]:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            return {
                name: archive.read(name)
                for name in archive.namelist()
                if not name.endswith("/")
            }
    with tarfile.open(artifact, mode="r:gz") as archive:
        members: dict[str, bytes] = {}
        for member in archive.getmembers():
            if not member.isfile():
                continue
            stream = archive.extractfile(member)
            assert stream is not None
            members[member.name] = stream.read()
        return members


def _run_source_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
        "from subagent_harness_mcp.cli import main\n"
        "try:\n"
        f"    exit_code = main({list(arguments)!r})\n"
        "except SystemExit:\n"
        "    assert 'mcp' not in sys.modules\n"
        "    assert 'claude_agent_sdk' not in sys.modules\n"
        "    raise\n"
        "assert 'mcp' not in sys.modules\n"
        "assert 'claude_agent_sdk' not in sys.modules\n"
        "raise SystemExit(exit_code)\n"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _venv_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _console_script(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / f"{DIST_NAME}.exe"
    return environment / "bin" / DIST_NAME


@pytest.fixture(scope="session")
def built_distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    configured = os.environ.get("SUBAGENT_MCP_TEST_DIST_DIR")
    if configured:
        dist_dir = Path(configured).resolve()
    else:
        dist_dir = tmp_path_factory.mktemp("package-build")
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
    assert [path.name for path in wheels] == [
        f"{PACKAGE_NAME}-{VERSION}-py3-none-any.whl"
    ]
    assert [path.name for path in sdists] == [f"{PACKAGE_NAME}-{VERSION}.tar.gz"]
    return wheels[0], sdists[0]


def test_pyproject_declares_publishable_package_contract() -> None:
    document = _read_toml(ROOT / "pyproject.toml")
    project = document["project"]

    assert project["name"] == DIST_NAME
    assert project["version"] == VERSION
    assert project["description"].startswith("Subagent MCP")
    assert project["readme"] == "README.md"
    assert project["requires-python"] == ">=3.10"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert {"grok", "grok-build"} <= set(project["keywords"])
    assert "Development Status :: 5 - Production/Stable" in project["classifiers"]
    assert "Development Status :: 2 - Pre-Alpha" not in project["classifiers"]
    assert project["dependencies"] == [
        "mcp>=2.0.0,<2.1",
        "claude-agent-sdk==0.2.142",
        "psutil>=6.1,<8",
    ]
    assert project["scripts"] == {
        DIST_NAME: "subagent_harness_mcp.cli:main"
    }

    assert document["build-system"] == {
        "requires": ["hatchling>=1.27,<2"],
        "build-backend": "hatchling.build",
    }
    wheel = document["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/subagent_harness_mcp"]
    assert wheel["force-include"] == {
        "schemas": "subagent_harness_mcp/schemas"
    }
    sdist = document["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "/schemas" in sdist["include"]
    assert "/docs/architecture.md" in sdist["include"]
    assert "/docs/adapter-authoring.md" in sdist["include"]
    assert "/docs/threat-model.md" in sdist["include"]
    assert document["tool"]["uv"]["package"] is True
    assert document["dependency-groups"]["phase0b"] == [
        "claude-agent-sdk==0.2.142"
    ]


def test_lock_has_direct_runtime_dependencies_and_exact_versions() -> None:
    document = _read_toml(ROOT / "uv.lock")
    packages = document["package"]
    versions = {
        package["name"]: package.get("version")
        for package in packages
        if "version" in package
    }
    assert versions["mcp"] == "2.0.0"
    assert versions["claude-agent-sdk"] == "0.2.142"

    project = next(
        package for package in packages if package["name"] == DIST_NAME
    )
    assert project["version"] == VERSION
    assert project["source"] != {"virtual": "."}
    assert {dependency["name"] for dependency in project["dependencies"]} == {
        "claude-agent-sdk",
        "mcp",
        "psutil",
    }


def test_preview_directory_is_ignored_exactly_once() -> None:
    ignore_lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ignore_lines.count(".preview/") == 1


def test_windows_config_launcher_uses_the_local_checkout() -> None:
    launcher = (ROOT / "open-config.bat").read_text(encoding="utf-8")
    command = f'uv run --project "%~dp0." --frozen {DIST_NAME} ui'

    assert "where uv" in launcher
    assert f"{command} --open" in launcher
    assert f"{command} --background" in launcher
    assert "http://127.0.0.1:8765" in launcher


def test_public_documents_use_display_and_distribution_identities() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_flat = " ".join(readme.split())
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert readme.startswith("# Subagent MCP")
    assert DIST_NAME in readme
    assert "```mermaid" in readme
    assert "**Claude Code — Ready.**" in readme
    assert "**DeepSeek Harness — Ready.**" in readme
    assert f"**Stable:** `{VERSION}`" in readme
    assert "fail-closed" not in readme
    assert "subscription-only policy before starting work" not in readme_flat
    assert "no provider task starts" not in readme_flat
    assert "before accepting its output" in readme_flat
    assert "can consume included subscription quota" in readme_flat
    assert VERSION in changelog
    assert "MIT License" in license_text
    assert "usage credits" in security.lower()
    assert "Stable boundary" in security
    for relative in (
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "docs/architecture.md",
        "docs/adapter-authoring.md",
        "docs/threat-model.md",
    ):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert "Subagent MCP" in content

    for content in (
        readme,
        security,
        (ROOT / "docs/architecture.md").read_text(encoding="utf-8"),
        (ROOT / "docs/threat-model.md").read_text(encoding="utf-8"),
    ):
        assert "AgentBridge" not in content

    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert workflow.startswith("name: publish-release")
    assert "--prerelease" not in workflow


def test_grok_build_candidate_documentation_is_truthful() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "architecture.md").read_text(
        encoding="utf-8"
    )
    authoring = (ROOT / "docs" / "adapter-authoring.md").read_text(
        encoding="utf-8"
    )
    claude_status = "**Claude Code — Ready.**"
    deepseek_status = "**DeepSeek Harness — Ready.**"
    grok_status = "**Grok Build — In development.**"

    assert readme.index(claude_status) < readme.index(deepseek_status)
    assert readme.index(deepseek_status) < readme.index(grok_status)
    grok_block = " ".join(
        readme.split(grok_status, 1)[1].split("\n\n", 1)[0].lower().split()
    )
    for phrase in (
        "read-only review",
        "bounded path-prefix writing",
        "separately approved live read-only and writer gates",
        "cached native login",
        "disabled by default",
        "no credits, paid overage, or model fallback",
        "terminal/test/git",
        "network/web/browser",
        "mcp/plugins/hooks",
        "nested agents",
        "native worktrees",
        "restart recovery",
        "macos/linux",
        "pre-request quota",
    ):
        assert phrase in grok_block

    for document in (architecture, authoring):
        normalized = " ".join(document.split())
        assert "provider-neutral ACP stdio helper" in normalized
        assert "argv-array process creation" in normalized
        assert "newline-delimited JSON-RPC" in normalized
        assert "bounded stdout and stderr" in normalized
        assert "exact owned-process cleanup" in normalized
        assert "does not own models, authentication, permissions" in normalized
        assert (
            "DeepSeek Harness does not use this helper in the current release."
            in normalized
        )


def test_public_documents_do_not_publish_codex_task_ids() -> None:
    task_id = re.compile(r"\b01a[0-9a-f]{5,}-[0-9a-f-]{20,}\b")
    documents = list(ROOT.glob("*.md")) + list((ROOT / "docs").rglob("*.md"))

    assert not [
        path.relative_to(ROOT).as_posix()
        for path in documents
        if task_id.search(path.read_text(encoding="utf-8"))
    ]


def test_registry_publish_workflow_uses_secretless_oidc_and_pinned_publisher() -> None:
    workflow = (
        ROOT / ".github/workflows/publish-mcp-registry.yml"
    ).read_text(encoding="utf-8")

    assert "types: [published]" in workflow
    assert "workflow_dispatch:" in workflow
    assert "id-token: write" in workflow
    assert (
        "registry/releases/download/v1.8.1/"
        "mcp-publisher_linux_amd64.tar.gz"
    ) in workflow
    assert "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc" in workflow
    assert "./mcp-publisher login github-oidc" in workflow
    assert "./mcp-publisher publish server.json" in workflow
    assert "secrets." not in workflow
    assert "+            " not in workflow


def test_readme_isolates_persistent_ui_from_codex_stdio_updates() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_flat = " ".join(readme.split())
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    uvx_prefix = f"uvx --isolated --from {DIST_NAME}=={VERSION} {DIST_NAME}"

    assert f"codex mcp add subagent-mcp -- {uvx_prefix} serve" in readme
    assert f"{uvx_prefix} ui --background" in readme
    assert "uvx --isolated --from" in readme
    assert f"{DIST_NAME} ui --stop" in readme
    assert f"uv tool install {DIST_NAME}" not in readme
    assert "uv tool install --reinstall" not in readme
    assert "codex mcp add subagent-mcp -- subagent-harness-mcp serve" not in readme
    assert "codex mcp remove subagent-mcp" in readme
    assert "close every Codex window once" in readme_flat
    assert "uvx --isolated" in architecture
    assert "different cache" in architecture.lower()


def test_official_mcp_registry_metadata_targets_the_pypi_stdio_server() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    assert "<!-- mcp-name: io.github.Thang1710/subagent-mcp -->" in readme
    assert server == {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": "io.github.Thang1710/subagent-mcp",
        "title": "Subagent MCP",
        "description": "Let Codex orchestrate external coding agents through their native harnesses.",
        "repository": {
            "url": "https://github.com/Thang1710/subagent-mcp",
            "source": "github",
        },
        "websiteUrl": "https://github.com/Thang1710/subagent-mcp",
        "version": VERSION,
        "packages": [
            {
                "registryType": "pypi",
                "identifier": DIST_NAME,
                "version": VERSION,
                "runtimeHint": "uvx",
                "transport": {"type": "stdio"},
                "packageArguments": [
                    {
                        "type": "positional",
                        "value": "serve",
                        "description": "Start the Subagent MCP stdio server.",
                    }
                ],
            }
        ],
    }
    sdist = _read_toml(ROOT / "pyproject.toml")["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "/server.json" in sdist["include"]


def test_ci_and_release_workflows_are_deterministic_and_manual() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    registry_release = (
        ROOT / ".github/workflows/publish-mcp-registry.yml"
    ).read_text(encoding="utf-8")
    dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
    action_refs = re.findall(
        r"^\s*(?:-\s+)?uses:\s+([^#\s]+)",
        release,
        re.MULTILINE,
    )
    registry_action_refs = re.findall(
        r"^\s*(?:-\s+)?uses:\s+([^#\s]+)",
        registry_release,
        re.MULTILINE,
    )
    run_scripts: list[str] = []
    lines = release.splitlines()
    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        match = re.fullmatch(r"run:\s*(.*)", line.strip())
        if match is None:
            continue
        scalar = match.group(1)
        if not re.fullmatch(r"[|>][+-]?", scalar):
            run_scripts.append(scalar)
            continue
        block: list[str] = []
        for following in lines[index + 1 :]:
            if following.strip():
                following_indent = len(following) - len(following.lstrip())
                if following_indent <= indent:
                    break
            block.append(following)
        run_scripts.append("\n".join(block))

    assert 'not real_git_worktree' in ci
    assert "runtime_canary" not in ci
    assert "workflow_dispatch:" in release
    assert "environment: pypi" in release
    assert "id-token: write" in release
    assert "actions: write" in release
    assert len(action_refs) == 6
    assert all(
        re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action_ref)
        for action_ref in action_refs
    )
    assert registry_action_refs
    assert all(
        re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action_ref)
        for action_ref in registry_action_refs
    )
    assert any(
        action_ref.startswith("pypa/gh-action-pypi-publish@")
        for action_ref in action_refs
    )
    assert (
        "pypa/gh-action-pypi-publish@"
        "dc37677b2e1c63e2034f94d8a5b11f265b73ba33"
    ) in action_refs
    assert run_scripts
    assert all("${{" not in script for script in run_scripts)
    assert release.index("Validate release tag input") < release.index(
        "actions/checkout@"
    )
    assert "$env:RELEASE_TAG" in release
    assert "GH_REPO: ${{ github.repository }}" in release
    assert "tomllib" not in release
    assert "Select-String -Path pyproject.toml" in release
    assert "password:" not in release
    assert "api-token:" not in release
    assert "release-manifest.json" in release
    assert "SHA256SUMS.txt" in release
    assert "sha256sum --check --strict SHA256SUMS.txt" in release
    assert 'item.name.endswith((".whl", ".tar.gz"))' in release
    assert "gh workflow run publish-mcp-registry.yml" in release
    assert "--ref main" in release
    assert '-f tag="$RELEASE_TAG"' in release
    assert "package-ecosystem: github-actions" in dependabot
    assert "directory: /" in dependabot


def test_source_import_is_lightweight_and_typed(tmp_path: Path) -> None:
    environment = tmp_path / "source-environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = _venv_python(environment)
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(ROOT / 'src')!r}); "
        "import subagent_harness_mcp as package; "
        f"assert package.__version__ == {VERSION!r}; "
        "assert 'mcp' not in sys.modules; "
        "assert 'claude_agent_sdk' not in sys.modules"
    )
    completed = subprocess.run(
        [python, "-I", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert (ROOT / "src" / PACKAGE_NAME / "py.typed").read_bytes() == b""


def test_cli_reports_version_without_traceback() -> None:
    completed = _run_source_cli("--version")
    assert completed.returncode == 0
    assert completed.stdout.strip() == f"{DIST_NAME} {VERSION}"
    assert completed.stderr == ""
    assert "Traceback" not in completed.stdout


@pytest.mark.parametrize("arguments", [()])
def test_cli_placeholder_errors_are_concise(arguments: tuple[str, ...]) -> None:
    completed = _run_source_cli(*arguments)
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "error:" in completed.stderr
    assert "--help" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_wheel_and_sdist_contain_the_public_package(
    built_distributions: tuple[Path, Path],
) -> None:
    wheel, sdist = built_distributions

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for relative in ("__init__.py", "cli.py", "py.typed"):
            assert f"{PACKAGE_NAME}/{relative}" in names
        for relative in (
            "static/index.html",
            "static/app.css",
            "static/app.js",
            "schemas/config-v1.json",
            "schemas/adapter-v1.json",
            "schemas/agent-descriptor-v1.json",
            "schemas/tools-v1.json",
        ):
            assert f"{PACKAGE_NAME}/{relative}" in names
        for relative in ("adapters/acp_stdio.py", "adapters/grok_build.py"):
            assert f"{PACKAGE_NAME}/{relative}" in names

        metadata_path = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser(policy=default).parsebytes(
            archive.read(metadata_path)
        )
        assert metadata["Name"] == DIST_NAME
        assert metadata["Version"] == VERSION
        assert metadata["Requires-Python"] == ">=3.10"
        assert metadata["License-Expression"] == "MIT"
        metadata_keywords = {
            keyword.strip()
            for value in metadata.get_all("Keywords", [])
            for keyword in value.split(",")
        }
        assert {"grok", "grok-build"} <= metadata_keywords
        requirements = {
            requirement.replace(" ", "")
            for requirement in metadata.get_all("Requires-Dist", [])
        }
        assert "claude-agent-sdk==0.2.142" in requirements
        assert "mcp<2.1,>=2.0.0" in requirements

        entry_points_path = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = archive.read(entry_points_path).decode("utf-8")
        assert (
            f"{DIST_NAME} = subagent_harness_mcp.cli:main" in entry_points
        )
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in names)

    with tarfile.open(sdist, mode="r:gz") as archive:
        names = set(archive.getnames())
        required_suffixes = {
            "/pyproject.toml",
            "/README.md",
            "/LICENSE",
            "/SECURITY.md",
            "/CHANGELOG.md",
            "/CONTRIBUTING.md",
            "/CODE_OF_CONDUCT.md",
            "/docs/architecture.md",
            "/docs/adapter-authoring.md",
            "/docs/threat-model.md",
            "/schemas/config-v1.json",
            "/schemas/adapter-v1.json",
            "/schemas/agent-descriptor-v1.json",
            "/schemas/tools-v1.json",
            f"/src/{PACKAGE_NAME}/__init__.py",
            f"/src/{PACKAGE_NAME}/cli.py",
            f"/src/{PACKAGE_NAME}/py.typed",
            f"/src/{PACKAGE_NAME}/adapters/acp_stdio.py",
            f"/src/{PACKAGE_NAME}/adapters/grok_build.py",
        }
        for suffix in required_suffixes:
            assert any(name.endswith(suffix) for name in names), suffix


def test_release_archives_exclude_private_grok_evidence(
    built_distributions: tuple[Path, Path],
) -> None:
    forbidden_paths = (
        ".phase0a/",
        ".preview/",
        "tests/fixtures/",
        "fake_grok_acp.py",
        "grok-build-no-model.json",
        "transcripts/",
    )
    forbidden_content = (
        re.compile(
            rb"[A-Za-z]:[\\/](?:Users|ClaudeCode|CodeX|DeepSeekHarness)[\\/]"
        ),
        re.compile(rb"/(?:home|Users)/[^/\s]+/"),
        re.compile(rb"\b01a[0-9a-f]{5,}-[0-9a-f-]{20,}\b"),
        re.compile(rb"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I),
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        re.compile(
            rb"\b(?:gh[pousr]_|xox[baprs]-|sk-ant-|xai-)"
            rb"[A-Za-z0-9_-]{12,}"
        ),
        re.compile(rb"grok-build-no-model|raw[_ -]?no[_ -]?model", re.I),
    )

    for artifact in built_distributions:
        members = _archive_members(artifact)
        assert members
        for name, payload in members.items():
            normalized = name.replace("\\", "/")
            assert not any(marker in normalized for marker in forbidden_paths)
            assert not any(pattern.search(payload) for pattern in forbidden_content)


def test_wheel_installs_and_runs_in_an_isolated_environment(
    built_distributions: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    wheel, _ = built_distributions
    environment = tmp_path / "wheel-environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = _venv_python(environment)

    uv = shutil.which("uv")
    assert uv is not None
    installed = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            str(python),
            str(wheel),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr

    code = (
        "import sys; import subagent_harness_mcp as package; "
        f"assert package.__version__ == {VERSION!r}; "
        "assert 'mcp' not in sys.modules; "
        "assert 'claude_agent_sdk' not in sys.modules"
    )
    imported = subprocess.run(
        [python, "-I", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert imported.returncode == 0, imported.stdout + imported.stderr

    invoked = subprocess.run(
        [_console_script(environment), "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert invoked.returncode == 0
    assert invoked.stdout.strip() == f"{DIST_NAME} {VERSION}"
    assert invoked.stderr == ""
