"""Resolve a chat's owner-scoped Vercel authority for Devbox calls."""

import fastapi

import auth as vercel_auth
from agent import devbox
from store import chats, spaces


async def for_chat(chat_id: str) -> devbox.Auth:
    chat = await chats.get(chat_id)
    if chat is None:
        raise fastapi.HTTPException(404, "unknown chat")
    space = await spaces.get(chat.space_id)
    if space is None:
        raise fastapi.HTTPException(404, "unknown space")
    if not space.owner_id:
        raise fastapi.HTTPException(409, "connect this space to Vercel first")
    if not space.vercel_team_id or not space.vercel_project_id:
        raise fastapi.HTTPException(409, "select a Vercel team and project for this space")
    if not space.repos:
        raise fastapi.HTTPException(409, "add a repository to this space first")
    return devbox.Auth(
        token=await vercel_auth.access_token(space.owner_id),
        team_id=space.vercel_team_id,
        project_id=space.vercel_project_id,
        repo=space.repos[0],
    )
