import pytest

from hatchery import templates


def test_default_template_is_an_agent_starter_without_platform_copies():
    tree = templates.load("default")
    assert {
        "AGENTS.md",
        "USER.md",
        "MEMORY.md",
        "README.md",
        "memories/README.md",
        "skills/README.md",
        "scripts/README.md",
    } <= set(tree)
    assert "SOUL.md" not in tree
    assert not any(p.endswith("/SKILL.md") for p in tree)
    assert [p for p in tree if p.startswith("scripts/")] == ["scripts/README.md"]
    assert not tree["scripts/README.md"].executable


def test_unknown_template_is_reported_with_the_available_ones():
    with pytest.raises(FileNotFoundError, match="default"):
        templates.load("finance")
