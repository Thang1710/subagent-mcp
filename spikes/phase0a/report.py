from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


_GATE_NAMES = (
    "standalone_cli",
    "subscription_auth",
    "credential_precedence",
    "observer_visibility",
    "lifecycle_commands",
    "agents_json_schema",
    "context_init_subset",
    "context_attestation",
    "init_only_capability",
    "plugin_disable_effective",
    "strict_mcp_pre_spawn",
    "project_manifest",
    "windows_handle_release",
    "session_start_hook",
    "worktree_create_hook",
    "worktree_remove_hook",
    "stop_hook",
    "stop_failure_hook",
    "daemon_stop_race",
    "agent_view_overhead",
    "background_concurrency",
)
_STATUSES = {"PASS", "FAIL", "UNKNOWN", "BLOCKED"}
_BEGIN_GATES = "<!-- BEGIN GENERATED GATES -->"
_END_GATES = "<!-- END GENERATED GATES -->"
_BEGIN_SECTION = "<!-- BEGIN GENERATED SECTION 19.1 -->"
_END_SECTION = "<!-- END GENERATED SECTION 19.1 -->"
_BEGIN_DECISION = "<!-- BEGIN GENERATED PHASE DECISION -->"
_END_DECISION = "<!-- END GENERATED PHASE DECISION -->"
_RFC3339 = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


def _validate_generated_at(generated_at: str) -> None:
    if not isinstance(generated_at, str) or _RFC3339.fullmatch(generated_at) is None:
        raise ValueError("generated_at must be RFC3339")
    try:
        normalized = generated_at[:-1] + "+00:00" if generated_at[-1] in "Zz" else generated_at
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("generated_at must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError("generated_at must be RFC3339")


def _validate_adjudicated_gates(gates: Mapping[str, Mapping[str, Any]]) -> None:
    if not isinstance(gates, Mapping) or set(gates) != set(_GATE_NAMES):
        raise ValueError("report requires the exact adjudicated gate set")
    markers = {
        _BEGIN_GATES, _END_GATES, _BEGIN_SECTION, _END_SECTION,
        _BEGIN_DECISION, _END_DECISION,
    }
    for name in _GATE_NAMES:
        row = gates[name]
        if not isinstance(row, Mapping) or set(row) != {"status", "evidence"}:
            raise ValueError(f"{name} adjudication row is invalid")
        if row.get("status") not in _STATUSES:
            raise ValueError(f"invalid gate status for {name}")
        evidence = row.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"{name} requires nonempty evidence")
        if any(marker in evidence for marker in markers):
            raise ValueError(f"{name} evidence contains a generated marker")


def _render_gate_block(
    gates: Mapping[str, Mapping[str, Any]],
    generated_at: str,
) -> str:
    _validate_adjudicated_gates(gates)
    _validate_generated_at(generated_at)
    lines = [
        f"Generated: {generated_at}",
        "",
        "| Gate | Status | Evidence |",
        "|---|---|---|",
    ]
    for name in sorted(_GATE_NAMES):
        row = gates[name]
        evidence = str(row["evidence"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {name} | {row['status']} | {evidence} |")
    lines.extend([
        "",
        "UNKNOWN is not PASS. FAIL or BLOCKED prevents the dependent Phase 0b capability.",
    ])
    return "\n".join(lines)


def _render_section_block(rows: Sequence[Mapping[str, str]]) -> str:
    if len(rows) != 10:
        raise ValueError("section 19.1 requires exactly ten rows")
    lines = [
        "| Requirement | Outcome | Report evidence |",
        "|---|---|---|",
    ]
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"requirement", "outcome", "evidence"}:
            raise ValueError("section 19.1 row is invalid")
        requirement = row["requirement"]
        outcome = row["outcome"]
        evidence = row["evidence"]
        if (
            not isinstance(requirement, str)
            or not requirement
            or outcome not in _STATUSES
            or not isinstance(evidence, str)
            or not evidence
        ):
            raise ValueError("section 19.1 row is invalid")
        lines.append(
            "| " + requirement.replace("|", "\\|").replace("\n", " ")
            + " | " + outcome
            + " | " + evidence.replace("|", "\\|").replace("\n", " ") + " |"
        )
    return "\n".join(lines)


def _render_decision_block(decision: Mapping[str, Any]) -> str:
    if not isinstance(decision, Mapping) or set(decision) != {
        "phase_0a_accepted", "phase_0b_may_begin", "status", "nonpass_requirements",
    }:
        raise ValueError("phase decision is invalid")
    accepted = decision["phase_0a_accepted"]
    may_begin = decision["phase_0b_may_begin"]
    status = decision["status"]
    missing = decision["nonpass_requirements"]
    if (
        type(accepted) is not bool
        or type(may_begin) is not bool
        or status not in {"PASS", "BLOCKED"}
        or not isinstance(missing, list)
        or any(not isinstance(item, str) or not item for item in missing)
        or len(missing) != len(set(missing))
        or accepted != (status == "PASS")
        or may_begin != accepted
        or bool(missing) == accepted
    ):
        raise ValueError("phase decision is inconsistent")
    if accepted:
        return (
            "Phase 0a evidence decision: **PASS**. All section 19.1 requirements pass. "
            "Phase 0b still requires the user report-acceptance gate before any action."
        )
    lines = [
        "Phase 0a evidence decision: **BLOCKED**. Phase 0b must not begin.",
        "",
        "Non-PASS requirements:",
        "",
    ]
    lines.extend(f"- {item}" for item in missing)
    return "\n".join(lines)


def _replace_generated_block(text: str, begin: str, end: str, body: str) -> str:
    if begin in body or end in body:
        raise ValueError("generated body contains its marker")
    if text.count(begin) != 1 or text.count(end) != 1:
        raise ValueError("report must contain exactly one generated marker pair")
    start = text.index(begin)
    finish = text.index(end)
    if start > finish:
        raise ValueError("report generated markers are out of order")
    replacement = f"{begin}\n{body.strip()}\n{end}"
    return text[:start] + replacement + text[finish + len(end):]


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _update_adjudicated_report(
    path: str | Path,
    *,
    gates: Mapping[str, Mapping[str, Any]],
    section_rows: Sequence[Mapping[str, str]],
    decision: Mapping[str, Any],
    generated_at: str,
) -> None:
    target = Path(path)
    text = target.read_bytes().decode("utf-8").replace("\r\n", "\n")
    replacements = (
        (_BEGIN_GATES, _END_GATES, _render_gate_block(gates, generated_at)),
        (_BEGIN_SECTION, _END_SECTION, _render_section_block(section_rows)),
        (_BEGIN_DECISION, _END_DECISION, _render_decision_block(decision)),
    )
    for begin, end, body in replacements:
        text = _replace_generated_block(text, begin, end, body)
    _write_text_atomic(target, text)
