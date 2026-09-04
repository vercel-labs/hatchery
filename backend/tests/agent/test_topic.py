from agent import topic


def test_topic_has_strict_gateway_schema():
    schema = topic.Topic.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


async def test_generate_allows_structured_output(monkeypatch):
    seen = {}

    class Run:
        output = topic.Topic(topic="Wants to rewire Slack!!!")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class Agent:
        def run(self, model, messages, output_type, params):
            seen["model"] = model
            seen["messages"] = messages
            seen["output_type"] = output_type
            seen["params"] = params
            return Run()

    monkeypatch.setattr(topic.ai, "Agent", Agent)
    monkeypatch.setattr(topic.ai, "get_model", lambda name: name)

    generated = await topic.generate("Please improve chat names")

    assert generated == "wants to rewire"
    assert len(generated) <= 20
    assert "at most 20" in topic.SYSTEM
    assert "'s cron jobs work" in topic.SYSTEM
    assert "wants to rewire slack" in topic.SYSTEM
    assert seen["model"] == "openai/gpt-5.6-luna"
    assert seen["output_type"] is topic.Topic
    assert seen["params"].output.max_tokens == 100
