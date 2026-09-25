"""Operator endpoints for serving: status, published routes, schedules, and secrets.

Ported from the serve/secret routes of agentmesh `api/routes.py`. Mounted on the main
app, so Hatchery's session auth and origin checks apply. Listing never returns secret
values; only `reveal` does, with `Cache-Control: no-store`. Setting an existing name
rotates it.
"""

import logging
import typing

import fastapi
import pydantic

from hatchery import messages, vault
from hatchery.agent import supervisor
from hatchery.serve import scheduling, service
from hatchery.store import agents

log = logging.getLogger(__name__)
router = fastapi.APIRouter(prefix="/api/agents/{agent_id}")


class Command(pydantic.BaseModel):
    request_id: str = pydantic.Field(min_length=1, max_length=200)


class SecretCommand(Command):
    value: pydantic.SecretStr


async def _agent(agent_id: str) -> None:
    from hatchery.agent import runtime

    if await agents.get(agent_id) is None:
        raise fastapi.HTTPException(404, "unknown agent")
    if runtime.install() is None:
        raise fastapi.HTTPException(409, "storage is not configured")


@router.get("/serve")
async def serve_status(agent_id: str, request: fastapi.Request) -> dict[str, typing.Any]:
    """Main revision, the revision the serve sandbox holds, counts, and retirement."""
    await _agent(agent_id)
    try:
        return await service.current().status(agent_id, port=request.url.port)
    except FileNotFoundError as error:
        raise fastapi.HTTPException(404, "the agent has no directory on main") from error


@router.get("/routes")
async def routes(agent_id: str, request: fastapi.Request) -> dict[str, typing.Any]:
    """Live and invalid routes published on main, with their public URLs."""
    await _agent(agent_id)
    try:
        return await service.current().list_routes(agent_id, port=request.url.port)
    except FileNotFoundError as error:
        raise fastapi.HTTPException(404, "the agent has no directory on main") from error


@router.get("/schedules")
async def schedules(agent_id: str) -> dict[str, typing.Any]:
    """Jobs on main with rule, next run, pause, availability, and the last result."""
    await _agent(agent_id)
    try:
        return await service.current().list_schedules(agent_id)
    except FileNotFoundError as error:
        raise fastapi.HTTPException(404, "the agent has no directory on main") from error


@router.post("/schedules/{name}/pause", status_code=202)
async def pause_schedule(agent_id: str, name: str, body: Command) -> dict[str, typing.Any]:
    await _agent(agent_id)
    try:
        outcome = await scheduling.current().set_paused(agent_id, name, True, body.request_id)
    except KeyError as error:
        raise fastapi.HTTPException(404, "Schedule not found") from error
    return {"name": name, "paused": True, "outcome": outcome}


@router.post("/schedules/{name}/resume", status_code=202)
async def resume_schedule(agent_id: str, name: str, body: Command) -> dict[str, typing.Any]:
    await _agent(agent_id)
    try:
        outcome = await scheduling.current().set_paused(agent_id, name, False, body.request_id)
    except KeyError as error:
        raise fastapi.HTTPException(404, "Schedule not found") from error
    return {"name": name, "paused": False, "outcome": outcome}


@router.get("/secrets")
async def secrets(agent_id: str) -> dict[str, typing.Any]:
    """Stored and requested names with the agent's note; never values."""
    await _agent(agent_id)
    requested = (await supervisor.roster(agent_id))["secret_requests"]
    stored = {item["name"]: item for item in await vault.inventory(agent_id)}
    return {
        "secrets": [
            {
                "name": name,
                "stored": name in stored,
                "updated_at": stored.get(name, {}).get("updated_at"),
                "note": requested.get(name),
            }
            for name in sorted(stored.keys() | requested.keys())
        ]
    }


@router.post("/secrets/{name}")
async def set_secret(agent_id: str, name: str, body: SecretCommand) -> dict[str, typing.Any]:
    """Store or rotate one value and settle the agent's request for it."""
    try:
        name = vault.validate_secret_name(name)
    except ValueError as error:
        raise fastapi.HTTPException(422, str(error)) from error
    await _agent(agent_id)
    try:
        result = await vault.set(agent_id, name, body.value.get_secret_value(), body.request_id)
    except ValueError as error:
        raise fastapi.HTTPException(422, str(error)) from error
    await supervisor.send(
        agent_id,
        messages.SecretProvided(name),
        idempotency_key=f"secret:provided:{body.request_id}",
    )
    return result


@router.post("/secrets/{name}/reveal")
async def reveal_secret(
    agent_id: str, name: str, body: Command, response: fastapi.Response
) -> dict[str, str]:
    await _agent(agent_id)
    response.headers["Cache-Control"] = "no-store"
    try:
        return {"name": vault.validate_secret_name(name), "value": await vault.reveal(agent_id, name)}
    except ValueError as error:
        raise fastapi.HTTPException(422, str(error)) from error
    except KeyError as error:
        raise fastapi.HTTPException(404, "Secret not found") from error


@router.post("/secrets/{name}/delete")
async def delete_secret(agent_id: str, name: str, body: Command) -> dict[str, typing.Any]:
    await _agent(agent_id)
    try:
        return await vault.delete(agent_id, name, body.request_id)
    except ValueError as error:
        raise fastapi.HTTPException(422, str(error)) from error


async def retire(agent_id: str, request_id: str) -> None:
    """Retirement's serving half: vault tombstone, serve sandbox gone, Scheduler cancelled."""
    from hatchery.agent import runtime

    if runtime.install() is None:
        return
    await service.current().retire(agent_id, request_id)
    await scheduling.current().retire(agent_id)


async def maintain() -> None:
    """The five-minute heartbeat: reconcile schedules with main (covers GitHub merges)."""
    from hatchery.agent import runtime

    if runtime.install() is None:
        return
    try:
        await scheduling.current().reconcile()
    except Exception:
        log.exception("schedule reconciliation failed")
