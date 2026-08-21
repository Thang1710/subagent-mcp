from __future__ import annotations

import os
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
VERSION = "0.1.0a11"


def _read_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


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
    assert project["dependencies"] == [
        "mcp>=2.0.0,<2.1",
        "claude-agent-sdk==0.2.142",
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
    }


def test_preview_directory_is_ignored_exactly_once() -> None:
    ignore_lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ignore_lines.count(".preview/") == 1


def test_public_documents_use_display_and_distribution_identities() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    assert readme.startswith("# Subagent MCP")
    assert DIST_NAME in readme
    assert "```mermaid" in readme
    assert VERSION in changelog
    assert "MIT License" in license_text
    assert "usage credits" in security.lower()
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


def test_ci_and_release_workflows_are_deterministic_and_manual() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert 'not real_git_worktree' in ci
    assert "runtime_canary" not in ci
    assert "workflow_dispatch:" in release
    assert "environment: pypi" in release
    assert "id-token: write" in release
    assert "pypa/gh-action-pypi-publish@release/v1" in release
    assert "GH_REPO: ${{ github.repository }}" in release
    assert "tomllib" not in release
    assert "Select-String -Path pyproject.toml" in release
    assert "password:" not in release
    assert "api-token:" not in release
    assert "release-manifest.json" in release
    assert "SHA256SUMS.txt" in release
    assert 'item.name.endswith((".whl", ".tar.gz"))' in release


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
        }
        for suffix in required_suffixes:
            assert any(name.endswith(suffix) for name in names), suffix


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
