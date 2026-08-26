"""Pick the space for a new chat before the dispatcher can run."""

import json

import ai
import pydantic

import models


SYSTEM = """\
You assign a new conversation to exactly one hatchery space. Use the user's
first prompt and its source metadata. Prefer a space whose description,
repositories, or resources match the work. Return only the requested structured
output. Never answer the user or do the work."""


class Classification(pydantic.BaseModel):
    space_id: str


def model() -> ai.Model:
    return ai.get_model("openai/gpt-5.6-luna")


async def classify(
    prompt: str, metadata: dict, available: list[models.Space]
) -> models.Space:
    if not available:
        raise RuntimeError("cannot classify a chat without spaces")
    choices = [
        {
            "id": space.id,
            "name": space.name,
            "about": space.about,
            "repos": space.repos,
            "resources": [resource.model_dump() for resource in space.resources],
        }
        for space in available
    ]
    request = json.dumps(
        {"first_prompt": prompt, "metadata": metadata, "spaces": choices},
        ensure_ascii=False,
    )
    agent = ai.Agent()
    async with agent.run(
        model(),
        [ai.system_message(SYSTEM), ai.user_message(request)],
        output_type=Classification,
        params=ai.InferenceRequestParams(
            sampling={
                ai.TemperatureSamplerParams: ai.TemperatureSamplerParams(temperature=0)
            },
            output=ai.OutputParams(max_tokens=100),
        ),
    ) as result:
        async for _ in result:
            pass
        selected = next(
            (space for space in available if space.id == result.output.space_id), None
        )
    if selected is None:
        raise RuntimeError("classifier returned an unknown space")
    return selected
