import json

import models
import migrate_agents
from store import agent_files, agents


async def test_imports_only_legacy_space_definition(monkeypatch, tmp_path):
    data = tmp_path / "data"
    spaces = data / "spaces"
    spaces.mkdir(parents=True)
    (spaces / "spc_docs.json").write_text(
        json.dumps(
            {
                "id": "spc_docs",
                "name": "Docs Agent",
                "about": "# Docs\n\nMaintain documentation.",
                "repos": ["acme/docs"],
                "resources": [],
                "color": "blue-700",
            }
        ),
        encoding="utf-8",
    )
    (data / "chats").mkdir()
    (data / "chats" / "chat_old.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HATCHERY_DATA_DIR", str(data))

    async def configured():
        return True

    monkeypatch.setattr(agent_files, "configured", configured)
    calls = []
    revision = iter(["a" * 40, "b" * 40])

    async def snapshot(slug):
        return models.AgentFilesSnapshot(
            agent_slug=slug, revision=next(revision), files=[]
        )

    async def scaffold(slug, **kwargs):
        calls.append(("scaffold", slug, kwargs))

    async def write(slug, path, content, **kwargs):
        calls.append(("write", slug, path, content, kwargs))

    monkeypatch.setattr(agent_files, "snapshot", snapshot)
    monkeypatch.setattr(agent_files, "scaffold", scaffold)
    monkeypatch.setattr(agent_files, "write", write)

    imported = await migrate_agents.migrate()

    assert [agent.slug for agent in imported] == ["docs-agent"]
    assert (await agents.get(imported[0].id)).repos == ["acme/docs"]
    assert calls[0][0:2] == ("scaffold", "docs-agent")
    assert calls[1][0:3] == ("write", "docs-agent", "AGENTS.md")
    assert calls[1][3] == "# Docs\n\nMaintain documentation.\n"
