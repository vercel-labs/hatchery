import pytest

from worker import protocol


def test_command_envelope_is_versioned_and_strict():
    command = protocol.command(
        "wrk_1", 3, "task.input", task_id="task_1", payload={"text": "continue"}
    )

    assert command.version == 1
    assert command.sequence == 3
    assert command.type == "task.input"
    assert command.id.startswith("cmd_")
    assert protocol.command_topic("wrk_1") == "hatchery-worker-wrk_1-commands-v1"

    with pytest.raises(ValueError):
        protocol.Command.model_validate({**command.model_dump(), "unknown": True})
