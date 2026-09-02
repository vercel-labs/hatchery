"""Backend-only GitHub App commit signing for sandbox workers."""

import re

import httpx
from vercel import connect


async def sign_commits(connector: str, request: dict) -> list[str]:
    repo = request.get("repo") or {}
    owner = str(repo.get("owner") or "")
    name = str(repo.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", name
    ):
        raise ValueError("invalid GitHub repository")
    token = await connect.get_token_response(
        connector,
        subject=connect.ConnectAppTokenSubject(),
        authorization_details=[
            connect.ConnectGitHubAppInstallationAuthorizationDetail(
                org=owner, repositories=[name]
            )
        ],
    )
    signed: list[str] = []
    headers = {
        "authorization": f"Bearer {token.token}",
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
            if response.is_error:
                detail = response.text[:500]
                accepted = response.headers.get("x-accepted-github-permissions", "")
                suffix = f"; accepted permissions: {accepted}" if accepted else ""
                raise RuntimeError(
                    f"GitHub create commit failed ({response.status_code}): {detail}{suffix}"
                )
            signed.append(str(response.json()["sha"]))
    return signed
