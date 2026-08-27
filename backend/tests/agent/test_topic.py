from agent import topic


async def test_generate_allows_structured_output(monkeypatch):
    seen = {}

    class Run:
        output = topic.Topic(topic="Improve sidebar chats")

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

    assert generated == "Improve sidebar chats"
    assert seen["model"] == "openai/gpt-5.6-luna"
    assert seen["output_type"] is topic.Topic
    assert seen["params"].output.max_tokens == 100
