"""Backend-only GitHub-signed commit replay for sandbox workers."""

import base64
import logging
import re

import httpx
from vercel import connect


log = logging.getLogger("worker.signing")
GRAPHQL_URL = "https://api.github.com/graphql"
MUTATION = """
mutation CreateCommit($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid signature { isValid } }
  }
}
"""


async def sign_commits(
    connector: str, installation_id: str, request: dict
) -> list[str]:
    repo = request.get("repo") or {}
    owner = str(repo.get("owner") or "")
    name = str(repo.get("name") or "")
    branch = str(request.get("branch") or "")
    expected = str(request.get("base_oid") or "")
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+", owner)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch)
        or not re.fullmatch(r"[0-9a-f]{40,64}", expected)
    ):
        raise ValueError("invalid GitHub commit signing request")
    try:
        token = await connect.get_token_response(
            connector,
            subject=connect.ConnectAppTokenSubject(),
            installation_id=installation_id,
        )
    except Exception:
        log.exception(
            "GitHub app token mint failed",
            extra={
                "github_owner": owner,
                "github_repo": name,
                "installation_id": installation_id,
            },
        )
        raise
    signed: list[str] = []
    source_parent = expected
    headers = {
        "authorization": f"Bearer {token.token}",
        "accept": "application/vnd.github+json",
        "x-github-api-version": "2026-03-10",
    }
    async with httpx.AsyncClient(headers=headers, timeout=60) as client:
        for commit in request.get("commits") or []:
            original = str(commit.get("sha") or "")
            diff = await client.get(
                f"https://api.github.com/repos/{owner}/{name}/compare/{source_parent}...{original}",
                headers={"accept": "application/vnd.github.raw+json"},
            )
            if diff.is_error:
                raise RuntimeError(
                    f"GitHub compare failed ({diff.status_code}): {diff.text[:500]}"
                )
            additions = []
            deletions = []
            for file in diff.json().get("files") or []:
                path = str(file.get("filename") or "")
                status = str(file.get("status") or "")
                if status in ("removed", "renamed"):
                    deletions.append({"path": str(file.get("previous_filename") or path)})
                if status != "removed":
                    content = await client.get(
                        f"https://api.github.com/repos/{owner}/{name}/contents/{path}",
                        params={"ref": original},
                        headers={"accept": "application/vnd.github.raw+json"},
                    )
                    if content.is_error:
                        raise RuntimeError(
                            f"GitHub content read failed ({content.status_code}): {content.text[:500]}"
                        )
                    additions.append(
                        {
                            "path": path,
                            "contents": base64.b64encode(content.content).decode(),
                        }
                    )
            message = str(commit.get("message") or "")
            headline, separator, body = message.partition("\n")
            author = commit.get("original_author")
            if isinstance(author, dict) and author.get("name") and author.get("email"):
                trailer = f"Co-Authored-By: {author['name']} <{author['email']}>"
                body = body.rstrip()
                if trailer not in body:
                    body = f"{body}\n\n{trailer}".strip()
            variables = {
                "input": {
                    "branch": {
                        "repositoryNameWithOwner": f"{owner}/{name}",
                        "branchName": branch,
                    },
                    "expectedHeadOid": expected,
                    "message": {
                        "headline": headline,
                        **({"body": body} if separator or body else {}),
                    },
                    "fileChanges": {
                        "additions": additions,
                        "deletions": deletions,
                    },
                }
            }
            response = await client.post(
                GRAPHQL_URL, json={"query": MUTATION, "variables": variables}
            )
            payload = response.json() if response.content else {}
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if response.is_error or errors:
                detail = str(errors or response.text)[:500]
                accepted = response.headers.get("x-accepted-github-permissions", "")
                suffix = f"; accepted permissions: {accepted}" if accepted else ""
                raise RuntimeError(
                    f"GitHub signed commit failed ({response.status_code}): {detail}{suffix}"
                )
            created = payload["data"]["createCommitOnBranch"]["commit"]
            if not (created.get("signature") or {}).get("isValid"):
                raise RuntimeError("GitHub created a commit without a valid signature")
            source_parent = original
            expected = str(created["oid"])
            signed.append(expected)
    return signed
