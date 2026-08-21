from __future__ import annotations

from pathlib import Path

import pytest

from spikes.phase0a import report


def _gates() -> dict[str, dict[str, str]]:
    return {
        name: {"status": "BLOCKED", "evidence": "indexed evidence is incomplete"}
        for name in report._GATE_NAMES
    }


def _rows() -> list[dict[str, str]]:
    return [
        {
            "requirement": f"{index}. Requirement",
            "outcome": "BLOCKED",
            "evidence": "dependency=BLOCKED",
        }
        for index in range(1, 11)
    ]


def _decision() -> dict[str, object]:
    return {
        "phase_0a_accepted": False,
        "phase_0b_may_begin": False,
        "status": "BLOCKED",
        "nonpass_requirements": [row["requirement"] for row in _rows()],
    }


def _report_text() -> str:
    return (
        "# Report\n\nReviewed before\n\n"
        "<!-- BEGIN GENERATED GATES -->\nold gates\n<!-- END GENERATED GATES -->\n\n"
        "Reviewed middle\n\n"
        "<!-- BEGIN GENERATED SECTION 19.1 -->\nold section\n"
        "<!-- END GENERATED SECTION 19.1 -->\n\n"
        "Reviewed after\n\n"
        "<!-- BEGIN GENERATED PHASE DECISION -->\nold decision\n"
        "<!-- END GENERATED PHASE DECISION -->\n"
    )


def test_gate_renderer_is_deterministic_for_adjudicated_inputs() -> None:
    gates = _gates()
    gates["standalone_cli"] = {"status": "PASS", "evidence": "live-host.json"}

    first = report._render_gate_block(gates, "2026-08-20T00:00:00+07:00")
    second = report._render_gate_block(gates, "2026-08-20T00:00:00+07:00")

    assert first == second
    assert "| standalone_cli | PASS | live-host.json |" in first
    assert "| worktree_remove_hook | BLOCKED |" in first


@pytest.mark.parametrize(
    "generated_at",
    [
        "2026-08-20T00:00:00+00:00:30",
        "2026-08-20T00:00:00+00:60",
        "2026-08-20T00:00:00+24:00",
    ],
)
def test_gate_renderer_rejects_non_rfc3339_offsets(generated_at: str) -> None:
    with pytest.raises(ValueError, match="generated_at must be RFC3339"):
        report._render_gate_block(_gates(), generated_at)


def test_gate_renderer_requires_exact_gate_set() -> None:
    gates = _gates()
    gates.pop("worktree_remove_hook")
    with pytest.raises(ValueError, match="exact adjudicated gate set"):
        report._render_gate_block(gates, "2026-08-20T00:00:00+07:00")


def test_update_report_preserves_all_outer_narrative(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    target.write_text(_report_text(), encoding="utf-8")

    report._update_adjudicated_report(
        target,
        gates=_gates(),
        section_rows=_rows(),
        decision=_decision(),
        generated_at="2026-08-20T00:00:00+07:00",
    )

    rendered = target.read_text(encoding="utf-8")
    assert "Reviewed before" in rendered
    assert "Reviewed middle" in rendered
    assert "Reviewed after" in rendered
    assert "old gates" not in rendered
    assert "old section" not in rendered
    assert "old decision" not in rendered
    assert "Phase 0b must not begin" in rendered


def test_update_report_refuses_missing_marker_without_mutation(tmp_path: Path) -> None:
    target = tmp_path / "report.md"
    original = _report_text().replace("<!-- END GENERATED SECTION 19.1 -->", "")
    target.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="generated marker pair"):
        report._update_adjudicated_report(
            target,
            gates=_gates(),
            section_rows=_rows(),
            decision=_decision(),
            generated_at="2026-08-20T00:00:00+07:00",
        )

    assert target.read_text(encoding="utf-8") == original


def test_report_module_exposes_no_external_gate_status_cli() -> None:
    assert not hasattr(report, "main")
    assert not hasattr(report, "default_gates")
    assert not hasattr(report, "init_gate_file")
