"""Pick the agent for a new chat before the dispatcher can run."""

import json

import ai
import pydantic

import models


SYSTEM = """\
You assign a new conversation to exactly one hatchery agent. Use the user's
first prompt and its source metadata. Prefer an agent whose description,
repositories, or resources match the work. Return only the requested structured
output. Never answer the user or do the work."""


class Classification(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")

    agent_id: str


def model() -> ai.Model:
    return ai.get_model("openai/gpt-5.6-luna")


async def classify(
    prompt: str, metadata: dict, available: list[models.Agent]
) -> models.Agent:
    from store import agent_files

    if not available:
        raise RuntimeError("cannot classify a chat without agents")
    choices = []
    for available_agent in available:
        description = available_agent.about
        if await agent_files.configured():
            instructions = await agent_files.read(available_agent.slug, "AGENTS.md")
            if instructions is not None:
                description = instructions.content
        choices.append(
            {
                "id": available_agent.id,
                "name": available_agent.name,
                "about": description,
                "repos": available_agent.repos,
                "resources": [
                    resource.model_dump() for resource in available_agent.resources
                ],
            }
        )
    request = json.dumps(
        {"first_prompt": prompt, "metadata": metadata, "agents": choices},
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
            (agent for agent in available if agent.id == result.output.agent_id), None
        )
    if selected is None:
        raise RuntimeError("classifier returned an unknown agent")
    return selected
