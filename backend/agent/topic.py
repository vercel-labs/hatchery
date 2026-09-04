"""Generate a short sidebar title fragment from a chat's first request."""

import re

import ai
import pydantic


SYSTEM = """\
Write a lowercase title fragment for the conversation from the user's first request.
It will be placed directly after the user's display name, so make it read naturally,
for example "'s cron jobs work" or "wants to rewire slack". Use at most 20
characters for the fragment itself, including spaces and apostrophes. Use only a few
plain words and no punctuation except a leading possessive apostrophe when useful.
Return only the requested structured output."""


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
        ),
    ) as result:
        async for _ in result:
            pass
        fragment = result.output.topic.strip().lower()
        fragment = re.sub(r"[^\w\s']+", "", fragment).replace("_", " ")
        fragment = " ".join(fragment.split())
        if len(fragment) <= 20:
            return fragment
        shortened = fragment[:20].rstrip()
        return shortened.rsplit(" ", 1)[0] or shortened
