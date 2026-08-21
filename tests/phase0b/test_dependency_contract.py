from importlib.metadata import version


def test_phase0b_uses_the_reviewed_sdk_version() -> None:
    assert version("claude-agent-sdk") == "0.2.142"
