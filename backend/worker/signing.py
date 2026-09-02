"""Backend-only GitHub App commit signing for sandbox workers."""

import re

import httpx
from vercel import connect


async def sign_commits(connector: str, request: dict) -> list[str]:
    token = await connect.get_token(
        connector, subject=connect.ConnectAppTokenSubject()
    )
    repo = request.get("repo") or {}
    owner = str(repo.get("owner") or "")
    name = str(repo.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", name
    ):
        raise ValueError("invalid GitHub repository")
    signed: list[str] = []
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
    }
    async with httpx.AsyncClient(
        base_url="https://api.github.com", headers=headers, timeout=60
    ) as client:
        for commit in request.get("commits") or []:
            parents = (
                [signed[-1]]
                if signed
                else [str(item) for item in commit.get("parents") or []]
            )
            body = {
                "message": str(commit.get("message") or ""),
                "tree": str(commit.get("tree_sha") or ""),
                "parents": parents,
            }
            author = commit.get("original_author")
            if (
                isinstance(author, dict)
                and author.get("name")
                and author.get("email")
            ):
                trailer = f"Co-Authored-By: {author['name']} <{author['email']}>"
                if trailer not in body["message"]:
                    body["message"] = body["message"].rstrip() + f"\n\n{trailer}\n"
            response = await client.post(
                f"/repos/{owner}/{name}/git/commits", json=body
            )
            response.raise_for_status()
            signed.append(str(response.json()["sha"]))
    return signed
