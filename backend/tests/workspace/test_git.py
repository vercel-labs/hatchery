"""Signing commits through GitHub's Git database API, against a real Git-backed fake."""

import base64
import json
import os
import pathlib
import shutil
import subprocess

import httpx
import pytest

from hatchery.workspace import files as workspace_files
from hatchery.workspace import git as workspace_git

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def fake_github(root: pathlib.Path, *, sign: bool = True) -> tuple[httpx.Client, pathlib.Path, list]:
    """A GitHub Git database API backed by a bare repository that signs like GitHub does."""
    server = root / "server.git"
    subprocess.run(["git", "init", "-q", "--bare", str(server)], check=True)
    key = root / "key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    calls: list[str] = []

    def git(*args: str, data: bytes | None = None, strip: bool = True) -> str:
        env = {
            "PATH": os.defpath,
            "HOME": str(root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_INDEX_FILE": str(root / "server-index"),
            "GIT_AUTHOR_NAME": "hatchery[bot]",
            "GIT_AUTHOR_EMAIL": "bot@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "GitHub",
            "GIT_COMMITTER_EMAIL": "noreply@github.com",
        }
        output = subprocess.run(
            ["git", f"--git-dir={server}", *args], input=data, env=env, capture_output=True,
            check=True,
        ).stdout.decode()
        return output.strip() if strip else output

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token"
        assert request.url.path.startswith("/repos/acme/storage/git/")
        kind = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content)
        calls.append(kind)
        if kind == "blobs":
            data = base64.b64decode(body["content"])
            return httpx.Response(201, json={"sha": git("hash-object", "-w", "--stdin", data=data)})
        if kind == "trees":
            git("read-tree", body.get("base_tree", "--empty"))
            records = []
            # Deletions first, so a file can become a directory.
            for entry in sorted(body["tree"], key=lambda item: item.get("sha", "") is not None):
                if entry.get("sha", "") is None:
                    records.append(f"0 {'0' * 40}\t{entry['path']}\0")
                    continue
                oid = entry.get("sha") or git(
                    "hash-object", "-w", "--stdin", data=entry["content"].encode()
                )
                git("cat-file", "-e", oid)  # GitHub refuses blobs it does not have
                records.append(f"{entry['mode']} {oid}\t{entry['path']}\0")
            git("update-index", "-z", "--index-info", data="".join(records).encode())
            return httpx.Response(201, json={"sha": git("write-tree")})
        assert kind == "commits" and set(body) == {"message", "tree", "parents"}
        parents = [flag for parent in body["parents"] for flag in ("-p", parent)]
        signing = ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key}"] if sign else []
        sha = git(
            *signing, "commit-tree", body["tree"], *parents, *(["-S"] if sign else []),
            data=body["message"].encode(),
        )
        # GitHub reports the signed object as its signature plus the unsigned payload.
        header, _, message = git("cat-file", "commit", sha, strip=False).partition("\n\n")
        kept, signature = [], []
        for line in header.split("\n"):
            if line.startswith("gpgsig "):
                signature.append(line.removeprefix("gpgsig "))
            elif line.startswith(" ") and signature:
                signature.append(line[1:])
            else:
                kept.append(line)
        verification = (
            {"verified": True, "payload": "\n".join(kept) + "\n\n" + message,
             "signature": "\n".join(signature)}
            if sign
            else {"verified": False, "payload": None, "signature": None}
        )
        return httpx.Response(
            201,
            json={
                "sha": sha,
                "tree": {"sha": body["tree"]},
                "parents": [{"sha": parent} for parent in body["parents"]],
                "verification": verification,
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handle)), server, calls


@pytest.fixture
def local() -> workspace_git.Git:
    with workspace_git.Git("https://github.com/acme/storage", "token") as git:
        yield git


def publish(git: workspace_git.Git, server: pathlib.Path, sha: str) -> None:
    """Put a commit on the fake GitHub, as a fetched branch tip already is."""
    git.run("update-ref", "refs/heads/seed", sha)
    subprocess.run(
        ["git", f"--git-dir={server}", "fetch", "-q", str(git.path), "refs/heads/seed"], check=True
    )


def commit(git: workspace_git.Git, base: str | None, changes: dict) -> str:
    return git.commit(
        base,
        changes,
        prefixes=("agents/alice/",),
        owner="alice",
        message="Checkpoint alice\n\nRotor-Operation: abc\n",
    )


def test_sign_rebuilds_the_exact_commit_signed_by_github(tmp_path, local):
    client, server, calls = fake_github(tmp_path)
    File = workspace_files.File
    base = commit(
        local,
        None,
        {"agents/alice/AGENTS.md": File(b"I am alice.\n"), "agents/alice/old.md": File(b"old\n")},
    )
    publish(local, server, base)
    sha = commit(
        local,
        base,
        {
            "agents/alice/AGENTS.md": File(b"I am alice, v2.\n"),
            "agents/alice/old.md": None,
            "agents/alice/logo.png": File(b"\x89PNG\x00\xff\xfe"),
            "agents/alice/run.sh": File(b"#!/bin/sh\necho hi\n", executable=True),
        },
    )

    signed = local.sign(sha, client=client)

    assert signed != sha
    assert calls == ["blobs", "trees", "commits"]  # only binary content needs its own blob
    assert b"\ngpgsig " in local.run("cat-file", "commit", signed)
    assert local.entries(signed) == local.entries(sha)
    assert local.commit_parents(signed) == (base,)
    assert local.commit_message(signed) == local.commit_message(sha)
    assert local.replay(signed, "abc") == signed


def test_sign_keeps_every_parent_of_a_merge(tmp_path, local):
    client, server, _ = fake_github(tmp_path)
    File = workspace_files.File
    base = commit(local, None, {"agents/alice/AGENTS.md": File(b"I am alice.\n")})
    other = commit(local, base, {"agents/alice/notes.md": File(b"from main\n")})
    publish(local, server, other)
    sha = local.commit(
        base,
        {"agents/alice/notes.md": File(b"from main\n"), "agents/alice/mine.md": File(b"mine\n")},
        prefixes=("agents/alice/",),
        owner="alice",
        message="Refresh alice from main\n",
        extra_parents=(other,),
    )

    signed = local.sign(sha, client=client)

    assert local.commit_parents(signed) == (base, other)
    assert local.entries(signed) == local.entries(sha)


def test_sign_skips_the_tree_when_nothing_changed(tmp_path, local):
    client, server, calls = fake_github(tmp_path)
    base = commit(local, None, {"agents/alice/AGENTS.md": workspace_files.File(b"I am alice.\n")})
    publish(local, server, base)

    signed = local.sign(commit(local, base, {}), client=client)

    assert calls == ["commits"]
    assert local.entries(signed) == local.entries(base)


def test_sign_keeps_the_local_commit_when_github_does_not_sign(tmp_path, local):
    client, server, _ = fake_github(tmp_path, sign=False)
    base = commit(local, None, {"agents/alice/AGENTS.md": workspace_files.File(b"I am alice.\n")})
    publish(local, server, base)
    sha = commit(local, base, {"agents/alice/AGENTS.md": workspace_files.File(b"v2\n")})

    assert local.sign(sha, client=client) == sha
