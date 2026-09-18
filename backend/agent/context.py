"""Bounded prompt context from one agent's Git-backed files."""

import re


_MAX_INSTRUCTIONS = 20_000
_MAX_CATALOG = 18_000
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


def _metadata(text: str) -> dict[str, str]:
    match = _FRONTMATTER.match(text[:16_384])
    if match is None:
        return {}
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"name", "description"}:
            values[key.strip()] = value.strip().strip('"\'')[:500]
    return values


def render(files: dict[str, str]) -> str:
    instructions = files.get("AGENTS.md", "").strip()
    if len(instructions) > _MAX_INSTRUCTIONS:
        instructions = instructions[:_MAX_INSTRUCTIONS] + "\n\n[AGENTS.md truncated]"
    if not instructions:
        instructions = "No AGENTS.md instructions are available."

    memories = []
    for path, content in sorted(files.items()):
        if not path.startswith("memories/") or path == "memories/README.md":
            continue
        description = _metadata(content).get("description") or "missing frontmatter description"
        memories.append(f"- {path}: {description}")

    skills = []
    for path, content in sorted(files.items()):
        if not path.startswith("skills/") or not path.endswith("/SKILL.md"):
            continue
        metadata = _metadata(content)
        name = metadata.get("name") or path.split("/")[-2]
        description = metadata.get("description") or "missing frontmatter description"
        skills.append(f"- {name}: {description} ({path})")

    scripts = [path for path in sorted(files) if path.startswith("scripts/") and path != "scripts/README.md"]
    schedules = [path for path in sorted(files) if path.startswith("schedules/") and path.endswith("/job.py")]
    catalog = "\n".join(
        [
            "Memories (read complete files with read_memory):",
            *(memories or ["- None"]),
            "",
            "Skills (load the relevant SKILL.md before following it):",
            *(skills or ["- None"]),
            "",
            "Scripts:",
            *([f"- {path}" for path in scripts] or ["- None"]),
            "",
            "Schedules:",
            *([f"- {path}" for path in schedules] or ["- None"]),
        ]
    )
    if len(catalog) > _MAX_CATALOG:
        catalog = catalog[:_MAX_CATALOG] + "\n[agent file catalog truncated]"
    return f"Agent instructions from AGENTS.md:\n{instructions}\n\n{catalog}"
