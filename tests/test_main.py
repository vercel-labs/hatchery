from unittest import mock

import chat
import main


async def test_parity_turn_starts_workflow_without_waiting(monkeypatch):
    started = mock.AsyncMock()
    monkeypatch.setattr(main.vercel.workflow, "start", started)
    turn = mock.Mock()
    turn.message = chat.Message(role="user", content="run parity")
    turn.channel = "slack"
    turn.session = chat.Session(
        id="ses-1",
        token="slack:C1:1.0",
        channel="slack",
        channel_state={"channel_id": "C1", "thread_ts": "1.0"},
        created_at="",
    )
    turn.status = mock.AsyncMock()
    turn.reply = mock.AsyncMock()

    await main.handler(turn)

    turn.status.assert_awaited_once_with("scanning repos...")
    started.assert_awaited_once_with(
        main.worker.parity_workflow,
        {
            "channel": "slack",
            "state": {"channel_id": "C1", "thread_ts": "1.0"},
        },
    )
    turn.reply.assert_not_awaited()
