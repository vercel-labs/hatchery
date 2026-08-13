from channels import protocol


def test_event_envelope():
    ev = protocol.event(protocol.MESSAGE_COMPLETED, message="hi")
    assert ev.type == "message.completed"
    assert ev.data == {"message": "hi"}
    assert ev.meta.id.startswith("evt_")
    assert ev.meta.at  # iso timestamp


def test_event_ids_are_unique():
    ids = {protocol.event(protocol.TURN_STARTED).meta.id for _ in range(100)}
    assert len(ids) == 100


def test_message_roles():
    message = protocol.Message(role="user", content="hello")
    assert message.model_dump() == {"role": "user", "content": "hello"}


def test_history_derives_conversation_from_stream():
    stream = [
        protocol.event(protocol.TURN_STARTED),
        protocol.event(protocol.MESSAGE_RECEIVED, message="hi", channel="slack"),
        protocol.event(protocol.STATUS_UPDATED, status="thinking..."),
        protocol.event(protocol.MESSAGE_COMPLETED, message="hello!"),
        protocol.event(protocol.TURN_COMPLETED),
    ]
    assert [(m.role, m.content) for m in protocol.history(stream)] == [
        ("user", "hi"),
        ("assistant", "hello!"),
    ]
