from chat import protocol


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
