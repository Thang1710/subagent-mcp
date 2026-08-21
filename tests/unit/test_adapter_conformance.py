from __future__ import annotations

import asyncio

from subagent_harness_mcp.adapters import run_adapter_conformance
from subagent_harness_mcp.adapters.fake import FakeAdapter, FakeHarness


def test_public_conformance_runner_exercises_normalized_lifecycle(tmp_path) -> None:
    harness = FakeHarness()

    report = asyncio.run(
        run_adapter_conformance(
            lambda: FakeAdapter(harness),
            workspace_path=str(tmp_path),
            model="provider/conformance-model",
            reasoning={"effort": "high"},
            transport="managed-sdk",
        )
    )

    assert report.runtime_id == "fake"
    assert report.operations == (
        "probe",
        "resolve_context",
        "spawn",
        "open_session",
        "snapshot",
        "send",
        "interrupt",
        "close",
    )
    assert report.final_conversation_state == "closed"
    assert all(harness.call_count(operation) == 1 for operation in report.operations)
