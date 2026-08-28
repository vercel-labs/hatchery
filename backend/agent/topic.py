"""Generate a short sidebar topic from a chat's first request."""

import ai
import pydantic


SYSTEM = """\
Name this conversation from the user's first request. Use only a few plain words
that describe the requested work. Do not use punctuation, labels, or a full
sentence. Return only the requested structured output."""


class Topic(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    topic: str


async def generate(prompt: str) -> str:
    agent = ai.Agent()
    async with agent.run(
        ai.get_model("openai/gpt-5.6-luna"),
        [ai.system_message(SYSTEM), ai.user_message(prompt)],
        output_type=Topic,
        params=ai.InferenceRequestParams(
            sampling={
                ai.TemperatureSamplerParams: ai.TemperatureSamplerParams(temperature=0)
            },
            output=ai.OutputParams(max_tokens=10 * 10),
        ),
    ) as result:
        async for _ in result:
            pass
        return result.output.topic.strip()
