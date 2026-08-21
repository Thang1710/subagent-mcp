from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


_IMPORT = re.compile(r"(?<!\S)@([^\s]+)")
_MAX_IMPORT_DEPTH = 5
_GIT_TIMEOUT_SECONDS = 5


@dataclass(frozen=True, order=True)
class TrustKey:
    repository_id: str
    canonical_path: str
    sha256: str
    trust_revision: int


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _repository_id(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        common_dir = Path(result.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = repo / common_dir
        return f"git:{common_dir.resolve()}"
    return f"path:{repo}"


def _instruction_candidates(repo: Path) -> list[Path]:
    candidates = [repo / "CLAUDE.md", repo / ".claude" / "CLAUDE.md"]
    candidates.extend(sorted((repo / ".claude" / "rules").glob("**/*.md")))
    candidates.extend(sorted((repo / ".claude" / "skills").glob("**/SKILL.md")))
    candidates.extend(sorted((repo / ".claude" / "agents").glob("**/*.md")))
    candidates.extend(sorted((repo / ".claude" / "commands").glob("**/*.md")))
    return sorted({path.absolute() for path in candidates if path.is_file()}, key=str)


def _file_item(kind: str, path: Path, repo: Path) -> dict[str, Any]:
    original = path.absolute()
    canonical = original.resolve(strict=True)
    return {
        "kind": kind,
        "path": str(original),
        "canonical_path": str(canonical),
        "outside_repo": not _inside(canonical, repo),
        "exists": True,
        "sha256": _hash(original),
    }


def _missing_item(kind: str, path: str, canonical_path: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": path,
        "canonical_path": canonical_path,
        "outside_repo": None,
        "exists": False,
        "sha256": None,
    }


def _expand_project_dir(value: str, repo: Path) -> str:
    project = str(repo)
    return (
        value.replace("${CLAUDE_PROJECT_DIR}", project)
        .replace("$CLAUDE_PROJECT_DIR", project)
        .replace("%CLAUDE_PROJECT_DIR%", project)
    )


def _looks_path_like(value: str) -> bool:
    if not value or value.startswith("-"):
        return False
    path = Path(value)
    return (
        path.is_absolute()
        or value.startswith((".", "~"))
        or "/" in value
        or "\\" in value
        or path.suffix.casefold() in {".bat", ".cmd", ".exe", ".ps1", ".py", ".sh"}
    )


def _resolve_target(value: str, repo: Path, *, bare_executable: bool) -> Path | None:
    expanded = _expand_project_dir(value, repo)
    if bare_executable and not _looks_path_like(expanded):
        found = shutil.which(expanded)
        return None if found is None else Path(found).absolute()
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = repo / path
    return path.absolute()


def _hook_target_item(
    kind: str,
    raw: str,
    target: Path | None,
    repo: Path,
    source: Path,
    hook_event: str,
) -> dict[str, Any]:
    if target is None:
        synthetic = f"{source.absolute()}#{hook_event}:{raw}"
        item = _missing_item(kind, raw, synthetic)
    else:
        canonical = target.resolve(strict=False)
        if target.is_file():
            item = _file_item(kind, target, repo)
        else:
            item = _missing_item(kind, str(target), str(canonical))
            item["outside_repo"] = not _inside(canonical, repo)
    item.update({"raw": raw, "settings_path": str(source.absolute()), "hook_event": hook_event})
    return item


def _unresolvable_hook_item(source: Path, hook_event: str, index: int) -> dict[str, Any]:
    synthetic = f"{source.absolute()}#{hook_event}:command:{index}"
    item = _missing_item("unresolvable_hook_command", synthetic, synthetic)
    item.update({"settings_path": str(source.absolute()), "hook_event": hook_event})
    return item


def _command_hook_targets(
    source: Path,
    hooks: dict[str, Any],
    repo: Path,
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    command_index = 0
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for handler in group["hooks"]:
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    continue
                command_index += 1
                command = handler.get("command")
                args = handler.get("args")
                if (
                    not isinstance(command, str)
                    or not command.strip()
                    or not isinstance(args, list)
                    or any(not isinstance(arg, str) for arg in args)
                ):
                    targets.append(_unresolvable_hook_item(source, str(event), command_index))
                    continue
                executable = _resolve_target(command, repo, bare_executable=True)
                targets.append(
                    _hook_target_item(
                        "hook_executable", command, executable, repo, source, str(event)
                    )
                )
                for arg in args:
                    expanded = _expand_project_dir(arg, repo)
                    if not _looks_path_like(expanded):
                        continue
                    targets.append(
                        _hook_target_item(
                            "hook_argument",
                            arg,
                            _resolve_target(arg, repo, bare_executable=False),
                            repo,
                            source,
                            str(event),
                        )
                    )
    unique = {
        (item["kind"], item["path"], item["canonical_path"]): item
        for item in targets
    }
    return sorted(unique.values(), key=lambda item: (item["kind"], item["path"]))


def _settings_items(repo: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    settings: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []
    for relative in (Path(".claude/settings.json"), Path(".claude/settings.local.json")):
        path = (repo / relative).absolute()
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("settings must be an object")
        hooks = payload.get("hooks", {})
        if not isinstance(hooks, dict):
            raise ValueError("hooks must be an object")
        settings.append({
            **_file_item("project_hooks", path, repo),
            "hook_events": sorted(hooks),
        })
        targets.extend(_command_hook_targets(path, hooks, repo))
    return sorted(settings, key=lambda item: item["path"]), sorted(
        targets, key=lambda item: (item["kind"], item["path"])
    )


def _import_items(repo: Path, instruction_files: list[Path]) -> list[dict[str, Any]]:
    queue = deque((path, path.resolve(strict=True), 0) for path in instruction_files)
    visited_sources = {path.resolve(strict=True) for path in instruction_files}
    visited_targets: set[Path] = set()
    imports: list[dict[str, Any]] = []

    while queue:
        source, canonical_source, source_depth = queue.popleft()
        for match in _IMPORT.finditer(canonical_source.read_text(encoding="utf-8")):
            raw = match.group(1)
            target = Path(raw).expanduser()
            if not target.is_absolute():
                target = source.parent / target
            original_target = target.absolute()
            canonical_target = original_target.resolve(strict=False)
            if canonical_target in visited_targets:
                continue
            visited_targets.add(canonical_target)
            exists = original_target.is_file()
            depth = source_depth + 1
            depth_exceeded = depth > _MAX_IMPORT_DEPTH
            imports.append({
                "kind": "external_import",
                "source": str(source),
                "raw": raw,
                "path": str(original_target),
                "canonical_path": str(canonical_target),
                "outside_repo": not _inside(canonical_target, repo),
                "exists": exists,
                "sha256": _hash(original_target) if exists else None,
                "depth": depth,
                "depth_exceeded": depth_exceeded,
            })
            if exists and not depth_exceeded and canonical_target not in visited_sources:
                visited_sources.add(canonical_target)
                queue.append((original_target, canonical_target, depth))
    return sorted(imports, key=lambda item: item["canonical_path"])


def scan_project(root: str | Path) -> dict[str, Any]:
    repo = Path(root).resolve(strict=True)
    instruction_paths = _instruction_candidates(repo)
    settings, hook_targets = _settings_items(repo)
    return {
        "repo": str(repo),
        "repository_id": _repository_id(repo),
        "settings": settings,
        "hook_targets": hook_targets,
        "instruction_files": [
            _file_item("instruction_file", path, repo) for path in instruction_paths
        ],
        "external_imports": _import_items(repo, instruction_paths),
    }


def blocked_items(
    manifest: dict[str, Any],
    *,
    trusted_items: set[TrustKey],
    trust_revision: int,
) -> list[dict[str, Any]]:
    repository_id = manifest["repository_id"]
    candidates = [
        *(item for item in manifest["settings"] if item["hook_events"]),
        *manifest.get("hook_targets", []),
        *(
            item
            for item in manifest.get("instruction_files", [])
            if item["outside_repo"]
        ),
        *(
            item
            for item in manifest["external_imports"]
            if item["outside_repo"] or not item["exists"] or item["depth_exceeded"]
        ),
    ]
    blocked: list[dict[str, Any]] = []
    for item in candidates:
        key = TrustKey(
            repository_id,
            item["canonical_path"],
            item["sha256"] or "",
            trust_revision,
        )
        if not item["exists"] or key not in trusted_items:
            blocked.append({
                "kind": item["kind"],
                "path": item["path"],
                "canonical_path": item["canonical_path"],
                "sha256": item["sha256"],
                "repository_id": repository_id,
                "trust_revision": trust_revision,
            })
    return sorted(blocked, key=lambda item: (item["kind"], item["path"]))
