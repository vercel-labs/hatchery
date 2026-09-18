import pathlib
import subprocess

import pytest

from store import agent_files


@pytest.fixture(autouse=True)
def local_store(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("FASTAPI_ENV", "test")
    monkeypatch.setenv("HATCHERY_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def git_remote(monkeypatch, tmp_path):
    remote = tmp_path / "agents.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "test@example.com"], check=True)
    (seed / "README.md").write_text("# Agents\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(seed), "-c", "commit.gpgsign=false", "commit", "-m", "Initial"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", remote.as_uri()], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"], check=True, capture_output=True)
    monkeypatch.setenv("HATCHERY_AGENTS_REPOSITORY_URL", remote.as_uri())
    return remote


async def test_scaffold_lists_and_reads_standard_files(git_remote):
    before = await agent_files.snapshot("reviewer")
    created = await agent_files.scaffold(
        "reviewer", operation_id="scaffold-1", expected_revision=before.revision
    )

    assert created.revision != before.revision
    assert created.files == [
        "AGENTS.md",
        "memories/README.md",
        "schedules/README.md",
        "scripts/README.md",
        "skills/README.md",
    ]
    assert await agent_files.list_files("reviewer") == created.files
    instructions = await agent_files.read("reviewer", "AGENTS.md")
    assert instructions is not None
    assert instructions.revision == created.revision
    assert "Agent instructions" in instructions.content


async def test_write_uses_cas_and_operation_id_is_idempotent(git_remote):
    base = await agent_files.snapshot("writer")
    first = await agent_files.write(
        "writer",
        "memories/facts.md",
        "one durable fact\n",
        operation_id="write-1",
        expected_revision=base.revision,
    )
    repeated = await agent_files.write(
        "writer",
        "memories/facts.md",
        "ignored retry\n",
        operation_id="write-1",
        expected_revision=base.revision,
    )

    assert repeated.revision == first.revision
    assert (await agent_files.read("writer", "memories/facts.md")).content == "one durable fact\n"
    with pytest.raises(agent_files.Conflict) as conflict:
        await agent_files.write(
            "writer",
            "memories/facts.md",
            "stale\n",
            operation_id="write-2",
            expected_revision=base.revision,
        )
    assert conflict.value.current_revision == first.revision

    log = subprocess.run(
        ["git", "--git-dir", str(git_remote), "log", "main", "--format=%B"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert log.count("Hatchery-Operation-ID: write-1") == 1


async def test_delete_file_and_agent_tree(git_remote):
    base = await agent_files.snapshot("cleanup")
    created = await agent_files.scaffold(
        "cleanup", operation_id="scaffold-cleanup", expected_revision=base.revision
    )
    without_readme = await agent_files.delete(
        "cleanup",
        "memories/README.md",
        operation_id="delete-memory-readme",
        expected_revision=created.revision,
    )
    assert "memories/README.md" not in without_readme.files

    removed = await agent_files.delete_agent(
        "cleanup",
        operation_id="delete-cleanup-agent",
        expected_revision=without_readme.revision,
    )
    assert removed.files == []


@pytest.mark.parametrize(
    "path",
    ["../AGENTS.md", "/tmp/file", ".git/config", "nested\\file", "nested/../../file"],
)
async def test_paths_cannot_escape_agent_scope(git_remote, path):
    base = await agent_files.snapshot("safe-agent")
    with pytest.raises(ValueError):
        await agent_files.write(
            "safe-agent",
            path,
            "unsafe",
            operation_id="unsafe-1",
            expected_revision=base.revision,
        )


async def test_repository_url_is_fixed_and_file_urls_are_test_only(monkeypatch, git_remote):
    monkeypatch.setenv("FASTAPI_ENV", "production")
    with pytest.raises(agent_files.RepositoryError, match="development and tests"):
        await agent_files.snapshot("reviewer")
