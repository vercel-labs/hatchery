"""Workspace context: the skills catalog, memory index, core files, and system prompt.

Ported from agentmesh `tests/unit/test_agent_context.py`.
"""

import pytest

from hatchery import models, runtime
from hatchery.agent import context
from hatchery.workspace import files as workspace_files


def test_skill_and_memory_frontmatter_use_safe_folded_descriptions() -> None:
    skill = context.parse_skill(
        "deploy",
        "---\nname: ship-it\ndescription: >-\n  Deploy the atlas service.\n"
        "  Use for releases.\n---\n# Deploy\n",
        "skills/deploy/SKILL.md",
    )
    memory = context.parse_memory(
        "memories/atlas.md",
        "---\ndescription: >-\n  Atlas decisions and constraints.\n"
        "  Read before planning.\n---\n# Atlas\n",
    )
    assert (skill.name, skill.description) == (
        "ship-it",
        "Deploy the atlas service. Use for releases.",
    )
    assert memory.description == "Atlas decisions and constraints. Read before planning."
    with pytest.raises(ValueError, match="frontmatter"):
        context.parse_skill("x", "# No front matter\n", "skills/x/SKILL.md")
    with pytest.raises(ValueError, match="description"):
        context.parse_skill("x", "---\nname: x\n---\nbody", "skills/x/SKILL.md")


def test_system_prompt_lists_skills_and_memories_and_embeds_agents_md() -> None:
    tree = {
        "self/AGENTS.md": workspace_files.File(b"Be terse.\n"),
        "self/USER.md": workspace_files.File(b"Address the user as boss.\n"),
        "self/MEMORY.md": workspace_files.File(b"The production region is iad1.\n"),
        "self/skills/coding/SKILL.md": workspace_files.File(
            b"---\nname: coding\ndescription: Change code safely.\n---\n"
        ),
        "self/skills/broken/SKILL.md": workspace_files.File(b"no front matter"),
        "self/memories/README.md": workspace_files.File(b"how to use"),
        "self/memories/atlas.md": workspace_files.File(
            b"---\ndescription: Atlas deployment decisions.\n---\n# Atlas\n"
        ),
        "self/memories/projects/skynet.md": workspace_files.File(b"# Missing metadata\n"),
        "wiki/guide.md": workspace_files.File(b"shared"),
    }
    loaded = context.Context.from_tree("alice", tree)
    prompt = loaded.system_prompt()
    assert "You are **alice**, a durable, shared Hatchery agent." in prompt
    assert "- **coding** (`skills/coding/SKILL.md`): Change code safely." in prompt
    assert "skills/broken/SKILL.md: missing YAML frontmatter" in prompt
    assert "memories/\n|-- projects/" in prompt
    assert "|   `-- skynet.md - (missing or invalid" in prompt
    assert "`-- atlas.md - Atlas deployment decisions." in prompt
    assert "memories/README.md" not in prompt
    assert "<agents>\nBe terse.\n</agents>" in prompt
    assert "<user_profile>\nAddress the user as boss.\n</user_profile>" in prompt
    assert "<core_memory>\nThe production region is iad1.\n</core_memory>" in prompt
    assert dict(loaded.core_revisions).keys() == {
        "self/USER.md",
        "self/MEMORY.md",
        "wiki/PROMPT.md",
    }
    assert "/workspace/repos/<organization>/<repository>" in prompt
    assert "never put a checkout under `self/` or `wiki/`" in prompt


def test_duplicate_skill_names_are_diagnostic_and_not_offered() -> None:
    tree = {
        "self/skills/one/SKILL.md": workspace_files.File(b"---\nname: deploy\ndescription: First.\n---\n"),
        "self/skills/two/SKILL.md": workspace_files.File(b"---\nname: deploy\ndescription: Second.\n---\n"),
    }
    catalog = context.discover_skills(tree)
    assert catalog.skills == ()
    assert catalog.errors == (
        "duplicate skill name 'deploy': skills/one/SKILL.md, skills/two/SKILL.md",
    )


def skill(name: str, description: str, extra: str = "") -> File:
    return workspace_files.File(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n{name} body\n".encode()
    )


RUNTIME_API = workspace_files.File(b"---\nname: api\ndescription: Runtime API guide.\n---\nruntime api\n")
TEAM_HELLO = skill("hello", "Team greeting.")


def test_runtime_skills_are_never_shadowed_and_personal_skills_override_team_ones() -> None:
    tree = {
        "runtime/skills/api/SKILL.md": RUNTIME_API,
        "wiki/skills/api/SKILL.md": skill("api", "A team copy of the API guide."),
        "self/skills/api/SKILL.md": skill("api", "A stale personal copy."),
        "wiki/skills/hello/SKILL.md": TEAM_HELLO,
        "wiki/skills/release/SKILL.md": skill("release", "Cut a release the team way."),
        "self/skills/hello/SKILL.md": skill(
            "hello",
            "My greeting.",
            f"upstream: wiki/skills/hello/SKILL.md@{context.short(context.revision(TEAM_HELLO.content))}\n",
        ),
        "self/skills/release/SKILL.md": skill("release", "My release procedure."),
    }
    catalog = context.discover_skills(tree)
    rendered = context.Context.from_tree("alice", tree).system_prompt()

    assert [(s.name, s.layer, s.path) for s in catalog.skills] == [
        ("api", "runtime", "runtime/skills/api/SKILL.md"),
        ("hello", "self", "skills/hello/SKILL.md"),
        ("release", "self", "skills/release/SKILL.md"),
    ]
    assert catalog.find("hello", "team") is not None and catalog.find("api", "self") is not None
    team_release = context.short(context.revision(tree["wiki/skills/release/SKILL.md"].content))
    assert catalog.errors == (
        "wiki/skills/api/SKILL.md shadows the runtime skill 'api'; delete or rename it",
        "skills/api/SKILL.md shadows the runtime skill 'api'; delete or rename it",
        "skills/release/SKILL.md overrides the team skill 'release' without an upstream pin; "
        f"add `upstream: wiki/skills/release/SKILL.md@{team_release}` or rename it",
    )
    assert "- **api** (runtime): Runtime API guide." in rendered
    assert (
        "- **hello** (`skills/hello/SKILL.md`, overrides `wiki/skills/hello/SKILL.md`): "
        "My greeting.\n" in rendered
    )
    assert "\n  upstream changed:" not in rendered
    assert "A stale personal copy" not in rendered and "team copy" not in rendered


def test_team_skills_are_listed_with_their_layer_when_nothing_overrides_them() -> None:
    tree = {"wiki/skills/release/SKILL.md": skill("release", "Cut a release the team way.")}
    rendered = context.Context.from_tree("alice", tree).system_prompt()
    viewed = context.view_skill(tree, "release")

    assert "- **release** (`wiki/skills/release/SKILL.md`, team): Cut a release" in rendered
    assert viewed["layer"] == "team" and viewed["path"] == "wiki/skills/release/SKILL.md"
    assert viewed["revision"] == context.short(context.revision(tree["wiki/skills/release/SKILL.md"].content))
    assert "overridden_by" not in viewed


def test_an_upstream_pin_reports_a_changed_team_skill_until_the_pin_is_updated() -> None:
    original = skill("hello", "Team greeting.")
    pinned = context.short(context.revision(original.content))
    override = skill("hello", "My greeting.", f"upstream: wiki/skills/hello/SKILL.md@{pinned}\n")
    before = {"wiki/skills/hello/SKILL.md": original, "self/skills/hello/SKILL.md": override}
    revised = skill("hello", "Team greeting, now warmer.")
    after = {**before, "wiki/skills/hello/SKILL.md": revised}
    acknowledged = {
        **after,
        "self/skills/hello/SKILL.md": skill(
            "hello",
            "My greeting.",
            f"upstream: wiki/skills/hello/SKILL.md@{context.short(context.revision(revised.content))}\n",
        ),
    }

    quiet = context.Context.from_tree("alice", before).system_prompt()
    noisy = context.Context.from_tree("alice", after).system_prompt()
    settled = context.Context.from_tree("alice", acknowledged).system_prompt()

    assert "\n  upstream changed:" not in quiet
    assert f"\n  upstream changed: {pinned} -> {context.short(context.revision(revised.content))}\n" in noisy
    assert "\n  upstream changed:" not in settled
    changed = context.view_skill(after, "hello")
    assert changed["upstream"] == {
        "path": "wiki/skills/hello/SKILL.md",
        "pinned": pinned,
        "current": context.short(context.revision(revised.content)),
        "state": "changed",
    }
    team = context.view_skill(after, "hello", layer="team")
    assert team["content"].endswith("hello body\n") and "warmer" in team["content"]
    assert team["overridden_by"] == "skills/hello/SKILL.md"
    assert team["revision"] == context.short(context.revision(revised.content))


def test_a_pinned_fork_under_another_name_tracks_and_survives_upstream_removal() -> None:
    team = skill("release", "Cut a release the team way.")
    fork = skill(
        "my-release",
        "Release with my extra steps.",
        f"upstream: wiki/skills/release/SKILL.md@{context.short(context.revision(team.content))}\n",
    )
    tracked = {"wiki/skills/release/SKILL.md": team, "self/skills/my-release/SKILL.md": fork}
    orphaned = {"self/skills/my-release/SKILL.md": fork}

    with_team = context.Context.from_tree("alice", tracked).system_prompt()
    without_team = context.Context.from_tree("alice", orphaned).system_prompt()

    assert (
        "- **my-release** (`skills/my-release/SKILL.md`, from `wiki/skills/release/SKILL.md`): "
        "Release with my extra steps.\n" in with_team
    )
    assert "- **release** (`wiki/skills/release/SKILL.md`, team)" in with_team
    assert "\n  upstream changed:" not in with_team
    assert "\n  upstream removed: the team skill no longer exists" in without_team
    assert context.view_skill(orphaned, "my-release")["upstream"]["state"] == "removed"


def test_invalid_upstream_pins_and_pins_outside_personal_skills_are_diagnosed() -> None:
    tree = {
        "self/skills/bad/SKILL.md": skill("bad", "Bad pin.", "upstream: skills/x/SKILL.md@abc\n"),
        "wiki/skills/pinned/SKILL.md": skill(
            "pinned", "Team pin.", "upstream: wiki/skills/x/SKILL.md@1234567\n"
        ),
    }
    catalog = context.discover_skills(tree)
    assert catalog.skills == ()
    assert catalog.errors == (
        "wiki/skills/pinned/SKILL.md: upstream pins belong to personal skills only",
        "skills/bad/SKILL.md: upstream must look like wiki/skills/<name>/SKILL.md@<revision>",
    )


def test_skill_view_layer_selection_reports_missing_layers_and_bad_names() -> None:
    tree = {
        "runtime/skills/api/SKILL.md": RUNTIME_API,
        "self/skills/mine/SKILL.md": skill("mine", "Mine."),
    }
    assert context.view_skill(tree, "api", layer="runtime")["content"].endswith("runtime api\n")
    with pytest.raises(ValueError, match="unknown team skill 'api'"):
        context.view_skill(tree, "api", layer="team")
    with pytest.raises(ValueError, match="unknown self skill 'api'"):
        context.view_skill(tree, "api", layer="self")
    with pytest.raises(ValueError, match="layer must be one of runtime, team, self"):
        context.view_skill(tree, "mine", layer="global")


def test_personal_scripts_that_hide_runtime_helpers_are_flagged() -> None:
    tree = context.layered(
        {
            "self/scripts/edit": workspace_files.File(b"#!/bin/sh\n", executable=True),
            "self/scripts/mine": workspace_files.File(b"#!/bin/sh\n", executable=True),
            "self/scripts/nested/search": workspace_files.File(b"#!/bin/sh\n", executable=True),
        }
    )
    loaded = context.Context.from_tree("alice", tree)
    prompt = loaded.system_prompt()

    assert loaded.script_shadows == ("scripts/edit",)
    assert "Skill and script issues:\n- scripts/edit shadows the runtime helper `edit`" in prompt
    assert "scripts/mine" not in prompt


def test_team_prompt_is_loaded_every_turn_bounded_and_quarantined_when_conflicted() -> None:
    loaded = context.Context.from_tree(
        "alice", {"wiki/PROMPT.md": workspace_files.File(b"Always tag Slack posts with #mesh.\n")}
    )
    conflicted = context.Context.from_tree(
        "alice",
        {"wiki/PROMPT.md": workspace_files.File(b"<<<<<<< parent\nA\n=======\nB\n>>>>>>> thread\n")},
    )

    assert "<team>\nAlways tag Slack posts with #mesh.\n</team>" in loaded.system_prompt()
    assert dict(loaded.core_revisions).keys() == {
        "self/USER.md",
        "self/MEMORY.md",
        "wiki/PROMPT.md",
    }
    assert conflicted.core_conflicts == ("wiki/PROMPT.md",)
    quarantined = conflicted.system_prompt()
    assert "[wiki/PROMPT.md has an unresolved upstream/thread merge conflict" in quarantined
    assert "\nA\n" not in quarantined and "\nB\n" not in quarantined


def test_memory_tree_reports_omitted_files_when_its_prompt_budget_is_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hatchery.agent.context.MAX_MEMORY_PROMPT_CHARS", 120)
    memories = tuple(
        context.Memory(f"memories/topic-{index}.md", f"Description for topic {index} and its details.")
        for index in range(10)
    )
    rendered = context.render_memories(memories)

    assert len(rendered) <= 120
    assert "memory files omitted" in rendered
    assert "tree memories" in rendered


def test_core_context_is_bounded_and_conflicts_are_quarantined() -> None:
    rendered = context.render_core_file("start-" + "x" * 100 + "-end", "MEMORY.md", 80)
    conflicted = context.Context.from_tree(
        "alice",
        {
            "self/USER.md": workspace_files.File(
                b"<<<<<<< main\nCall me boss.\n=======\nCall me chief.\n>>>>>>> thread\n"
            )
        },
    )
    prompt = conflicted.system_prompt()

    assert len(rendered) == 80
    assert rendered.startswith("start-") and rendered.endswith("-end")
    assert "truncated" in rendered
    assert conflicted.core_conflicts == ("self/USER.md",)
    assert "Do not apply either side as authoritative context" in prompt
    assert "Call me boss" not in prompt and "Call me chief" not in prompt


def test_skill_view_returns_the_guide_inventory_and_one_contained_support_file() -> None:
    tree = {
        "self/skills/release/SKILL.md": workspace_files.File(
            b"---\nname: release\ndescription: Cut a release.\n---\n# Release\n"
        ),
        "self/skills/release/references/checks.md": workspace_files.File(b"# Checks\n"),
        "self/skills/release/scripts/tag.sh": workspace_files.File(b"#!/bin/sh\n", executable=True),
    }
    guide = context.view_skill(tree, "release")
    support = context.view_skill(tree, "release", "references/checks.md")

    assert guide["path"] == "skills/release/SKILL.md"
    assert guide["content"].endswith("# Release\n")
    assert guide["files"] == ["references/checks.md", "scripts/tag.sh"]
    assert support == {
        "name": "release",
        "layer": "self",
        "path": "skills/release/references/checks.md",
        "content": "# Checks\n",
    }
    with pytest.raises(ValueError, match="canonical"):
        context.view_skill(tree, "release", "../AGENTS.md")


def test_empty_workspace_still_produces_guidance() -> None:
    prompt = context.Context.from_tree("bob", {}).system_prompt()
    assert "No valid skills yet" in prompt and "No memory files yet" in prompt


def test_agents_projection_keeps_a_bounded_head_and_tail() -> None:
    text = "begin\n" + "x" * 30_000 + "\nend"
    rendered = context.render_agents(text)

    assert len(rendered) == 20_000
    assert rendered.startswith("begin\n") and rendered.endswith("\nend")
    assert "[AGENTS.md truncated from 30010 characters]" in rendered


# Compaction


def test_hatchery_prompt_names_the_agent_and_fits_the_linked_state() -> None:
    agent = models.Agent(
        id="docs",
        name="Docs",
        color="blue-700",
        created_at="2026-09-25T00:00:00+00:00",
        repos=["acme/docs"],
        resources=[models.Resource(title="Guide", url="https://example.com/guide")],
    )

    unlinked = context.hatchery_prompt(agent, linked=False)
    linked = context.hatchery_prompt(agent, linked=True)

    assert "- Name: Docs\n- ID: docs" in unlinked
    assert "Available repositories:\n- acme/docs" in unlinked
    assert "- Guide (link): https://example.com/guide" in unlinked
    assert "start_thread may send the first notification" in unlinked
    assert "Do not start another\nthread." in linked
    assert "start_thread may send" not in linked


def dump(message: ai.messages.Message) -> dict[str, object]:
    return message.model_dump(mode="json")


