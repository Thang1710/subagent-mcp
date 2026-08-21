import json
from pathlib import Path
import subprocess
import sys

import pytest

from spikes.phase0a import manifest as manifest_module
from spikes.phase0a.manifest import TrustKey, blocked_items, scan_project


def _trust(item: dict[str, object], repository_id: str = "repo-1", revision: int = 1) -> TrustKey:
    return TrustKey(
        repository_id=repository_id,
        canonical_path=item.get("canonical_path", item["path"]),  # type: ignore[arg-type]
        sha256=item["sha256"],  # type: ignore[arg-type]
        trust_revision=revision,
    )


def test_scan_project_finds_hooks_and_external_import(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo x"}]}]}}),
        encoding="utf-8",
    )
    outside = tmp_path / "outside.md"
    outside.write_text("external", encoding="utf-8")
    (repo / "CLAUDE.md").write_text(f"@{outside}\n", encoding="utf-8")
    manifest = scan_project(repo)
    assert manifest["settings"][0]["hook_events"] == ["SessionStart"]
    assert manifest["external_imports"][0]["outside_repo"] is True
    assert len(manifest["settings"][0]["sha256"]) == 64
    blocked = blocked_items(manifest, trusted_items=set(), trust_revision=1)
    assert {item["kind"] for item in blocked} == {
        "project_hooks",
        "external_import",
        "unresolvable_hook_command",
    }


def test_same_hash_at_another_path_does_not_inherit_trust(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    first = repo / ".claude" / "settings.json"
    second = repo / ".claude" / "settings.local.json"
    payload = json.dumps({"hooks": {"Stop": []}})
    first.write_text(payload, encoding="utf-8")
    second.write_text(payload, encoding="utf-8")
    manifest = scan_project(repo)
    trust = {_trust(manifest["settings"][0], manifest["repository_id"])}
    blocked = blocked_items(manifest, trusted_items=trust, trust_revision=1)
    assert [item["path"] for item in blocked] == [str(second.resolve())]


def test_same_path_hash_at_another_repository_does_not_inherit_trust(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": []}}), encoding="utf-8")
    manifest = scan_project(repo)
    trust = {_trust(manifest["settings"][0], repository_id="other-repo")}
    assert blocked_items(manifest, trusted_items=trust, trust_revision=1)


def test_same_path_hash_at_another_revision_does_not_inherit_trust(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": []}}), encoding="utf-8")
    manifest = scan_project(repo)
    trust = {_trust(manifest["settings"][0], manifest["repository_id"], revision=2)}
    assert blocked_items(manifest, trusted_items=trust, trust_revision=1)


def test_same_repository_path_revision_with_stale_digest_remains_blocked(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    settings = repo / ".claude" / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": []}}), encoding="utf-8")
    manifest = scan_project(repo)
    item = manifest["settings"][0]
    trust = {
        TrustKey(
            repository_id=manifest["repository_id"],
            canonical_path=item["path"],
            sha256="0" * 64,
            trust_revision=1,
        )
    }
    blocked = blocked_items(manifest, trusted_items=trust, trust_revision=1)
    assert [entry["path"] for entry in blocked] == [str(settings.resolve())]


def test_scan_project_follows_transitive_imports_once(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside_a = tmp_path / "a.md"
    outside_b = tmp_path / "b.md"
    outside_a.write_text(f"@{outside_b}\n", encoding="utf-8")
    outside_b.write_text(f"@{outside_a}\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text(f"@{outside_a}\n", encoding="utf-8")
    manifest = scan_project(repo)
    assert [item["path"] for item in manifest["external_imports"]] == [
        str(outside_a.resolve()),
        str(outside_b.resolve()),
    ]


def test_scan_project_includes_rules_skills_agents_and_commands(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = [
        repo / ".claude" / "rules" / "rule.md",
        repo / ".claude" / "skills" / "review" / "SKILL.md",
        repo / ".claude" / "agents" / "reviewer.md",
        repo / ".claude" / "commands" / "review.md",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("safe\n", encoding="utf-8")
    manifest = scan_project(repo)
    assert {item["path"] for item in manifest["instruction_files"]} == {
        str(path.resolve()) for path in paths
    }


def test_missing_external_import_is_recorded_and_blocked(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    missing = tmp_path / "missing.md"
    (repo / "CLAUDE.md").write_text(f"@{missing}\n", encoding="utf-8")
    manifest = scan_project(repo)
    item = manifest["external_imports"][0]
    assert item["kind"] == "external_import"
    assert item["exists"] is False
    assert item["sha256"] is None
    assert blocked_items(manifest, trusted_items=set(), trust_revision=1)[0]["path"] == str(missing.resolve())


def test_scan_project_rejects_non_object_settings(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="settings must be an object"):
        scan_project(repo)


def test_scan_project_rejects_non_object_hooks(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text('{"hooks":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="hooks must be an object"):
        scan_project(repo)


def test_scan_project_finds_inline_import_and_expands_home(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    imported = home / "shared.md"
    imported.write_text("shared\n", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    (repo / "CLAUDE.md").write_text(
        "Read @~/shared.md before making changes.\n", encoding="utf-8"
    )

    item = scan_project(repo)["external_imports"][0]

    assert item["raw"] == "~/shared.md"
    assert item["canonical_path"] == str(imported.resolve())


def test_scan_project_records_but_does_not_follow_import_beyond_five_hops(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    chain = [tmp_path / f"outside-{index}.md" for index in range(1, 8)]
    for current, following in zip(chain, chain[1:]):
        current.write_text(f"@{following}\n", encoding="utf-8")
    chain[-1].write_text("deep content\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text(f"@{chain[0]}\n", encoding="utf-8")

    manifest = scan_project(repo)

    by_path = {item["canonical_path"]: item for item in manifest["external_imports"]}
    assert by_path[str(chain[5].resolve())]["depth_exceeded"] is True
    assert str(chain[6].resolve()) not in by_path
    blocked = blocked_items(manifest, trusted_items=set(), trust_revision=1)
    assert any(
        item["canonical_path"] == str(chain[5].resolve())
        and item["kind"] == "external_import"
        for item in blocked
    )


def test_scan_project_discovers_nested_agents_and_commands(tmp_path: Path):
    repo = tmp_path / "repo"
    paths = [
        repo / ".claude" / "agents" / "nested" / "reviewer.md",
        repo / ".claude" / "commands" / "nested" / "review.md",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("safe\n", encoding="utf-8")

    manifest = scan_project(repo)

    assert {item["path"] for item in manifest["instruction_files"]} == {
        str(path.absolute()) for path in paths
    }


def test_external_symlinked_instruction_requires_canonical_target_trust(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    rules = repo / ".claude" / "rules"
    rules.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("external\n", encoding="utf-8")
    link = rules / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        link.write_text("external\n", encoding="utf-8")
        original_resolve = Path.resolve

        def symlink_resolve(path, *args, **kwargs):
            if path.absolute() == link.absolute():
                return outside.resolve()
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", symlink_resolve)

    manifest = scan_project(repo)
    item = manifest["instruction_files"][0]
    blocked = blocked_items(manifest, trusted_items=set(), trust_revision=3)

    assert item["path"] == str(link.absolute())
    assert item["canonical_path"] == str(outside.resolve())
    assert item["outside_repo"] is True
    assert blocked[0]["canonical_path"] == str(outside.resolve())
    trust = {
        TrustKey(
            manifest["repository_id"],
            str(outside.resolve()),
            item["sha256"],
            3,
        )
    }
    assert blocked_items(manifest, trusted_items=trust, trust_revision=3) == []


def test_exec_hook_binds_executable_and_existing_script_argument(
    tmp_path: Path, monkeypatch
):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"executable")
    script = repo / ".claude" / "hook.py"
    script.write_text("print('safe')\n", encoding="utf-8")
    monkeypatch.setattr(manifest_module.shutil, "which", lambda name: str(executable) if name == "python" else None)
    settings = repo / ".claude" / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python",
                                    "args": ["${CLAUDE_PROJECT_DIR}/.claude/hook.py", "--safe"],
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    manifest = scan_project(repo)

    assert {item["kind"] for item in manifest["hook_targets"]} == {
        "hook_executable",
        "hook_argument",
    }
    trust = {
        _trust(manifest["settings"][0], manifest["repository_id"]),
        *(
            _trust(item, manifest["repository_id"])
            for item in manifest["hook_targets"]
        ),
    }
    assert blocked_items(manifest, trusted_items=trust, trust_revision=1) == []

    script.write_text("print('changed')\n", encoding="utf-8")
    changed = scan_project(repo)
    blocked = blocked_items(changed, trusted_items=trust, trust_revision=1)
    assert [item["kind"] for item in blocked] == ["hook_argument"]


def test_shell_form_and_missing_path_like_hook_targets_are_blocked(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "echo shell"}]},
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": sys.executable,
                                    "args": ["./missing.py"],
                                }
                            ]
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    manifest = scan_project(repo)
    blocked = blocked_items(manifest, trusted_items=set(), trust_revision=1)

    assert {item["kind"] for item in manifest["hook_targets"]} == {
        "unresolvable_hook_command",
        "hook_argument",
        "hook_executable",
    }
    assert any(
        item["kind"] == "hook_argument" and item["sha256"] is None
        for item in blocked
    )


def test_repository_identity_falls_back_on_git_timeout(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(manifest_module.subprocess, "run", timeout)

    assert scan_project(repo)["repository_id"] == f"path:{repo.resolve()}"


def test_repository_identity_uses_resolved_git_common_dir(tmp_path: Path):
    repo = tmp_path / "repo"
    completed = subprocess.run(
        ["git", "init", str(repo)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    expected = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()

    assert scan_project(repo)["repository_id"] == f"git:{Path(expected).resolve()}"
