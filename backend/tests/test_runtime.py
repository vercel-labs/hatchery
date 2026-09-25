import subprocess

from hatchery import runtime


def test_runtime_layer_ships_platform_skills_and_executable_helpers():
    tree = runtime.tree()
    assert {
        "runtime/skills/api/SKILL.md",
        "runtime/skills/schedules/SKILL.md",
        "runtime/skills/coding/SKILL.md",
    } <= set(tree)
    scripts = {p for p in tree if p.startswith("runtime/scripts/")}
    assert scripts == {
        f"runtime/scripts/{name}" for name in ("tree", "search", "edit", "fetch", "remember")
    }
    assert all(tree[p].executable for p in scripts)
    assert not tree["runtime/skills/api/SKILL.md"].executable
    assert tree["runtime/scripts/edit"].content.startswith(b"#!/usr/bin/env python3")
    assert not any(p.endswith(".py") for p in tree)
    assert runtime.scripts()["scripts/search"] == tree["runtime/scripts/search"].content


def test_search_caps_large_results_without_pipefail(tmp_path):
    script = tmp_path / "search"
    script.write_bytes(runtime.tree()["runtime/scripts/search"].content)
    script.chmod(0o755)
    source = tmp_path / "many.txt"
    source.write_text("\n".join(f"matching line {index}" for index in range(250)))

    result = subprocess.run(
        [script, "matching", source], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0
    assert len(result.stdout.splitlines()) == 200
