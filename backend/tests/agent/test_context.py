from agent import context


def test_renders_agents_memories_skills_scripts_and_schedules():
    rendered = context.render(
        {
            "AGENTS.md": "# Release agent\n\nKeep changes small.",
            "memories/deploy.md": "---\ndescription: Deployment facts\n---\nsecret body",
            "skills/release/SKILL.md": "---\nname: release\ndescription: Cut releases\n---\nsteps",
            "scripts/check": "#!/bin/sh\n",
            "schedules/daily/job.py": "SCHEDULE = None\n",
        }
    )

    assert "# Release agent" in rendered
    assert "memories/deploy.md: Deployment facts" in rendered
    assert "release: Cut releases" in rendered
    assert "secret body" not in rendered
    assert "scripts/check" in rendered
    assert "schedules/daily/job.py" in rendered
