"""Fresh workspace context and progressive skill/memory disclosure.

Ported from agentmesh `agent/context.py`; the persona file is `AGENTS.md`.

Skills come from three layers. The runtime layer ships with Hatchery and cannot be
shadowed. The team layer is `wiki/skills/`, shared by every agent and changed through
wiki review. The personal layer is `self/skills/`; a personal skill with a team skill's
name overrides it, and an `upstream:` pin records which team revision the override was
written against so a later team change is reported until the pin is updated.
"""

import dataclasses
import hashlib
import importlib.resources
import pathlib
import re
import string
import typing
from typing import Any, Literal

import yaml

from hatchery import models, runtime
from hatchery.workspace import files as workspace_files

USER_PATH = "self/USER.md"
CORE_MEMORY_PATH = "self/MEMORY.md"
TEAM_PROMPT_PATH = "wiki/PROMPT.md"
CORE_CONTEXT_PATHS = (USER_PATH, CORE_MEMORY_PATH, TEAM_PROMPT_PATH)
CONTEXT_PATHS = (
    "self/AGENTS.md",
    *CORE_CONTEXT_PATHS,
    "self/skills",
    "self/scripts",
    "self/memories",
    "wiki/skills",
)
MAX_FRONTMATTER_CHARS = 16_384
MAX_DESCRIPTION_CHARS = 500
MAX_SKILL_CANDIDATES = 300
MAX_SKILLS_IN_PROMPT = 150
MAX_SKILLS_PROMPT_CHARS = 18_000
MAX_MEMORY_PROMPT_CHARS = 18_000
MAX_AGENTS_PROMPT_CHARS = 20_000
MAX_USER_PROMPT_CHARS = 2_000
MAX_CORE_MEMORY_PROMPT_CHARS = 4_000
MAX_TEAM_PROMPT_CHARS = 8_000
MAX_CONTEXT_ERRORS = 20
MAX_SKILL_VIEW_BYTES = 24 * 1024
MAX_SKILL_FILES = 100
MAX_SKILL_FILES_CHARS = 4_000
REVISION_CHARS = 12
SKILL_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
UPSTREAM_PIN = re.compile(r"(wiki/skills/[^@\s]+/SKILL\.md)@([0-9a-f]{7,40})")

Layer = Literal["runtime", "team", "self"]
LAYERS: tuple[Layer, ...] = typing.get_args(Layer)
LAYER_PREFIXES: dict[Layer, str] = {
    "runtime": runtime.SKILLS_PREFIX,
    "team": "wiki/skills/",
    "self": "self/skills/",
}
UpstreamState = Literal["current", "changed", "removed"]


def prompt(name: str) -> string.Template:
    return string.Template(
        importlib.resources.files("hatchery.agent.prompts").joinpath(f"{name}.md").read_text()
    )


_START_THREAD = """\
This chat is not linked to an external thread. When asked to notify people,
first use find_channels and find_people as applicable. Use only exact
destination and person IDs returned by those tools; never invent handles. Then
start_thread may send the first notification and link that Slack or GitHub
thread to this chat. Ask for clarification rather than guessing between
ambiguous matches."""

_REPLY_INLINE = """\
This conversation is already linked to an external thread. Do not start another
thread. Reply normally without a notification tool call; your inline response
will be delivered to every linked channel."""


def hatchery_prompt(agent: models.Agent, *, linked: bool) -> str:
    """Hatchery's part of the thread system prompt: channels, fx subagents, and the agent record.

    The agent's own persona and memory come from its workspace (`Context.system_prompt`);
    this section adds what only Hatchery provides.
    """
    return prompt("hatchery").substitute(
        communication=_REPLY_INLINE if linked else _START_THREAD,
        name=agent.name,
        id=agent.id,
        repositories="\n".join(f"- {repo}" for repo in agent.repos) or "- None",
        resources="\n".join(
            f"- {resource.title} ({resource.kind}): {resource.url}"
            for resource in agent.resources
        )
        or "- None",
    )


def layered(tree: workspace_files.Tree) -> dict[str, workspace_files.File]:
    """A downloaded workspace tree with the runtime layer beneath it."""
    return {**runtime.tree(), **tree}


def revision(content: bytes) -> str:
    """The Git blob identity of file content, so `git show` resolves it for humans."""
    header = b"blob %d\0" % len(content)
    return hashlib.sha1(header + content, usedforsecurity=False).hexdigest()


def short(rev: str) -> str:
    return rev[:REVISION_CHARS]


@dataclasses.dataclass(frozen=True)
class Upstream:
    """A personal skill's record of the team revision it was written against."""

    path: str
    pinned: str
    current: str | None = None

    @property
    def state(self) -> UpstreamState:
        if self.current is None:
            return "removed"
        return "current" if self.current.startswith(self.pinned) else "changed"


@dataclasses.dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: str
    layer: Layer = "self"
    revision: str = ""
    upstream: Upstream | None = None
    overrides: str | None = None

    @property
    def tree_path(self) -> str:
        return f"self/{self.path}" if self.layer == "self" else self.path

    @property
    def tree_root(self) -> str:
        return self.tree_path.removesuffix("/SKILL.md")


@dataclasses.dataclass(frozen=True)
class Memory:
    path: str
    description: str | None = None


@dataclasses.dataclass(frozen=True)
class SkillCatalog:
    """Effective skills by name, every valid skill per layer, and the diagnostics."""

    skills: tuple[Skill, ...] = ()
    errors: tuple[str, ...] = ()
    layers: dict[Layer, tuple[Skill, ...]] = dataclasses.field(default_factory=dict)

    def find(self, name: str, layer: Layer | None = None) -> Skill | None:
        candidates = self.skills if layer is None else self.layers.get(layer, ())
        return next((skill for skill in candidates if skill.name == name), None)


@dataclasses.dataclass(frozen=True)
class Context:
    owner: str
    agents: str = ""
    user_profile: str = ""
    core_memory: str = ""
    team_prompt: str = ""
    skills: tuple[Skill, ...] = ()
    memories: tuple[Memory, ...] = dataclasses.field(default_factory=tuple)
    skill_errors: tuple[str, ...] = ()
    script_shadows: tuple[str, ...] = ()
    core_conflicts: tuple[str, ...] = ()
    core_revisions: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_tree(cls, owner: str, tree: workspace_files.Tree) -> Context:
        agents = tree.get("self/AGENTS.md")
        core = {path: tree.get(path) for path in CORE_CONTEXT_PATHS}
        user = core[USER_PATH]
        memory = core[CORE_MEMORY_PATH]
        team = core[TEAM_PROMPT_PATH]
        conflicts = tuple(
            path
            for path, source in core.items()
            if source is not None and _has_markers(source.content)
        )
        revisions = tuple(
            (
                path,
                hashlib.sha256(source.content).hexdigest() if source is not None else "missing",
            )
            for path, source in core.items()
        )
        catalog = discover_skills(tree)
        memories = []
        for path in sorted(tree):
            if not path.startswith("self/memories/") or path.endswith("/README.md"):
                continue
            relative = path.removeprefix("self/")
            try:
                memories.append(parse_memory(relative, tree[path].text))
            except ValueError:
                memories.append(Memory(relative))
        return cls(
            owner=owner,
            agents=agents.text.strip() if agents else "",
            user_profile=user.text.strip() if user else "",
            core_memory=memory.text.strip() if memory else "",
            team_prompt=team.text.strip() if team else "",
            skills=catalog.skills,
            memories=tuple(memories),
            skill_errors=catalog.errors,
            script_shadows=script_shadows(tree),
            core_conflicts=conflicts,
            core_revisions=revisions,
        )

    def system_prompt(self) -> str:
        skills = render_skills(self.skills, self.skill_errors, self.script_shadows)
        memories = render_memories(self.memories)
        return prompt("system").substitute(
            owner=self.owner,
            agents=render_agents(self.agents),
            user_profile=render_core_file(
                self.user_profile,
                "USER.md",
                MAX_USER_PROMPT_CHARS,
                USER_PATH in self.core_conflicts,
            ),
            core_memory=render_core_file(
                self.core_memory,
                "MEMORY.md",
                MAX_CORE_MEMORY_PROMPT_CHARS,
                CORE_MEMORY_PATH in self.core_conflicts,
            ),
            team=render_core_file(
                self.team_prompt,
                TEAM_PROMPT_PATH,
                MAX_TEAM_PROMPT_CHARS,
                TEAM_PROMPT_PATH in self.core_conflicts,
                empty=(
                    "(empty - the team can add shared guidance for every agent in "
                    "`wiki/PROMPT.md`; edits become a wiki proposal)"
                ),
            ),
            skills=skills,
            memories=memories,
        )


def render_agents(text: str) -> str:
    if not text:
        return "(empty - write AGENTS.md together with your team)"
    if len(text) <= MAX_AGENTS_PROMPT_CHARS:
        return text
    marker = f"\n\n[AGENTS.md truncated from {len(text)} characters]\n\n"
    room = MAX_AGENTS_PROMPT_CHARS - len(marker)
    head = room * 3 // 4
    return text[:head] + marker + text[-(room - head) :]


def _has_markers(content: bytes) -> bool:
    lines = content.split(b"\n")
    opened = b"<<<<<<< main" in lines or b"<<<<<<< parent" in lines
    return opened and b">>>>>>> thread" in lines


def render_core_file(
    value: str, name: str, limit: int, conflicted: bool = False, empty: str | None = None
) -> str:
    if conflicted:
        return (
            f"[{name} has an unresolved upstream/thread merge conflict. Do not apply either "
            "side as authoritative context; read and resolve the file first.]"
        )
    if not value:
        return empty or (
            f"(empty - keep {name} concise and add only facts useful across nearly every task)"
        )
    if len(value) <= limit:
        return value
    marker = f"\n\n[{name} truncated from {len(value)} characters; consolidate it]\n\n"
    if len(marker) >= limit:
        return marker[:limit]
    room = limit - len(marker)
    head = room * 3 // 4
    return value[:head] + marker + value[-(room - head) :]


def _frontmatter(text: str) -> dict[str, Any]:
    match = FRONTMATTER.match(text)
    if not match:
        raise ValueError("missing YAML frontmatter")
    source = match[1]
    if len(source) > MAX_FRONTMATTER_CHARS:
        raise ValueError("frontmatter is too large")
    try:
        value = yaml.safe_load(source)
    except yaml.YAMLError as error:
        raise ValueError("invalid YAML frontmatter") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("frontmatter must be a mapping")
    return value


def _description(metadata: dict[str, Any]) -> str:
    value = metadata.get("description")
    if not isinstance(value, str) or not (description := " ".join(value.split())):
        raise ValueError("description must be a non-empty string")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError(f"description exceeds {MAX_DESCRIPTION_CHARS} characters")
    return description


def _upstream(metadata: dict[str, Any], layer: Layer) -> Upstream | None:
    value = metadata.get("upstream")
    if value is None:
        return None
    if layer != "self":
        raise ValueError("upstream pins belong to personal skills only")
    match = UPSTREAM_PIN.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise ValueError("upstream must look like wiki/skills/<name>/SKILL.md@<revision>")
    return Upstream(match[1], match[2])


def parse_skill(directory: str, text: str, path: str, layer: Layer = "self") -> Skill:
    metadata = _frontmatter(text)
    name = metadata.get("name")
    if not isinstance(name, str) or not SKILL_NAME.fullmatch(name):
        raise ValueError("name must be a 1-64 character lowercase hyphenated slug")
    return Skill(name, _description(metadata), path, layer, upstream=_upstream(metadata, layer))


def parse_memory(path: str, text: str) -> Memory:
    return Memory(path, _description(_frontmatter(text)))


def _display_path(tree_path: str, layer: Layer) -> str:
    return tree_path.removeprefix("self/") if layer == "self" else tree_path


def _discover_layer(tree: workspace_files.Tree, layer: Layer) -> tuple[list[Skill], list[str]]:
    prefix = LAYER_PREFIXES[layer]
    candidates: list[Skill | str] = []
    roots: list[str] = []
    paths = sorted(
        (path for path in tree if path.startswith(prefix) and path.endswith("/SKILL.md")),
        key=lambda path: (path.count("/"), path),
    )
    omitted = max(0, len(paths) - MAX_SKILL_CANDIDATES)
    for path in paths[:MAX_SKILL_CANDIDATES]:
        root = path.removesuffix("/SKILL.md")
        if any(root.startswith(f"{parent}/") for parent in roots):
            continue
        roots.append(root)
        shown = _display_path(path, layer)
        try:
            skill = parse_skill(root.removeprefix(prefix), tree[path].text, shown, layer)
            candidates.append(dataclasses.replace(skill, revision=revision(tree[path].content)))
        except ValueError as error:
            candidates.append(f"{shown}: {error}")

    skills = [candidate for candidate in candidates if isinstance(candidate, Skill)]
    errors = [candidate for candidate in candidates if isinstance(candidate, str)]
    duplicates = {
        skill.name for skill in skills if sum(item.name == skill.name for item in skills) > 1
    }
    if duplicates:
        for name in sorted(duplicates):
            duplicate_paths = ", ".join(skill.path for skill in skills if skill.name == name)
            errors.append(f"duplicate skill name {name!r}: {duplicate_paths}")
        skills = [skill for skill in skills if skill.name not in duplicates]
    if omitted:
        errors.append(f"{omitted} {layer} skill candidates omitted after the discovery limit")
    return skills, errors


def _resolve_upstream(skill: Skill, tree: workspace_files.Tree) -> Skill:
    if skill.upstream is None:
        return skill
    source = tree.get(skill.upstream.path)
    current = revision(source.content) if source is not None else None
    return dataclasses.replace(skill, upstream=Upstream(skill.upstream.path, skill.upstream.pinned, current))


def discover_skills(tree: workspace_files.Tree) -> SkillCatalog:
    """Resolve the three layers by name: runtime is never shadowed; self overrides team."""
    layers: dict[Layer, tuple[Skill, ...]] = {}
    errors: list[str] = []
    for layer in LAYERS:
        found, layer_errors = _discover_layer(tree, layer)
        layers[layer] = tuple(sorted(found, key=lambda skill: skill.name))
        errors.extend(layer_errors)

    protected = {skill.name for skill in layers["runtime"]}
    team = {skill.name: skill for skill in layers["team"] if skill.name not in protected}
    effective: list[Skill] = list(layers["runtime"])
    for skill in layers["team"]:
        if skill.name in protected:
            errors.append(
                f"{skill.path} shadows the runtime skill {skill.name!r}; delete or rename it"
            )
    for skill in layers["self"]:
        if skill.name in protected:
            errors.append(
                f"{skill.path} shadows the runtime skill {skill.name!r}; delete or rename it"
            )
            continue
        resolved = _resolve_upstream(skill, tree)
        shadowed = team.pop(skill.name, None)
        if shadowed is not None:
            resolved = dataclasses.replace(resolved, overrides=shadowed.path)
            if resolved.upstream is None:
                errors.append(
                    f"{skill.path} overrides the team skill {skill.name!r} without an upstream "
                    f"pin; add `upstream: {shadowed.path}@{short(shadowed.revision)}` or "
                    "rename it"
                )
        effective.append(resolved)
    effective.extend(team.values())
    return SkillCatalog(
        tuple(sorted(effective, key=lambda skill: skill.name)), tuple(errors), layers
    )


def script_shadows(tree: workspace_files.Tree) -> tuple[str, ...]:
    """Personal scripts that hide a runtime helper of the same name on `PATH`."""
    provided = {
        path.removeprefix(runtime.SCRIPTS_PREFIX)
        for path in tree
        if path.startswith(runtime.SCRIPTS_PREFIX)
    }
    return tuple(
        sorted(
            path.removeprefix("self/")
            for path in tree
            if path.startswith("self/scripts/")
            and path.count("/") == 2
            and path.removeprefix("self/scripts/") in provided
        )
    )


def _skill_line(skill: Skill) -> str:
    if skill.layer == "runtime":
        origin = "runtime"
    elif skill.layer == "team":
        origin = f"`{skill.path}`, team"
    elif skill.overrides is not None:
        origin = f"`{skill.path}`, overrides `{skill.overrides}`"
    elif skill.upstream is not None:
        origin = f"`{skill.path}`, from `{skill.upstream.path}`"
    else:
        origin = f"`{skill.path}`"
    line = f"- **{skill.name}** ({origin}): {skill.description}"
    if skill.upstream is not None and skill.upstream.state == "changed":
        assert skill.upstream.current is not None
        current = short(skill.upstream.current)
        line += f"\n  upstream changed: {skill.upstream.pinned} -> {current}"
    elif skill.upstream is not None and skill.upstream.state == "removed":
        line += "\n  upstream removed: the team skill no longer exists; drop the pin to acknowledge"
    return line


def render_skills(
    skills: tuple[Skill, ...], errors: tuple[str, ...], script_shadows: tuple[str, ...] = ()
) -> str:
    lines: list[str] = []
    omitted = 0
    for skill in skills:
        line = _skill_line(skill)
        full = len("\n".join([*lines, line])) > MAX_SKILLS_PROMPT_CHARS
        if len(lines) >= MAX_SKILLS_IN_PROMPT or full:
            omitted += 1
        else:
            lines.append(line)
    omitted += max(0, len(skills) - len(lines) - omitted)
    if omitted:
        lines.append(f"- ... {omitted} additional skills omitted; inspect `skills/` if needed.")
    errors = (
        *errors,
        *(
            f"{path} shadows the runtime helper `{path.removeprefix('scripts/')}`; delete it "
            "to receive runtime fixes, or rename it to keep a personal variant"
            for path in script_shadows
        ),
    )
    if errors:
        heading = "\nSkill and script issues:"
        if len("\n".join([*lines, heading])) <= MAX_SKILLS_PROMPT_CHARS:
            lines.append(heading)
        shown = 0
        for error in errors[:MAX_CONTEXT_ERRORS]:
            line = f"- {error}"
            if len("\n".join([*lines, line])) > MAX_SKILLS_PROMPT_CHARS:
                break
            lines.append(line)
            shown += 1
        remaining = len(errors) - shown
        if remaining:
            line = f"- ... {remaining} additional issues omitted."
            if len("\n".join([*lines, line])) <= MAX_SKILLS_PROMPT_CHARS:
                lines.append(line)
    return "\n".join(lines) or (
        "No valid skills yet. Write one in `skills/<name>/SKILL.md` when a procedure repeats."
    )


def _memory_tree(memories: tuple[Memory, ...]) -> list[tuple[str, bool]]:
    tree: dict[str, Any] = {}
    for memory in memories:
        node = tree
        parts = pathlib.PurePosixPath(memory.path.removeprefix("memories/")).parts
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = memory.description

    lines: list[tuple[str, bool]] = [("memories/", False)]

    def walk(node: dict[str, Any], prefix: str) -> None:
        entries = sorted(node.items(), key=lambda item: (not isinstance(item[1], dict), item[0]))
        for index, (name, value) in enumerate(entries):
            last = index == len(entries) - 1
            connector = "`-- " if last else "|-- "
            if isinstance(value, dict):
                lines.append((f"{prefix}{connector}{name}/", False))
                walk(value, prefix + ("    " if last else "|   "))
            else:
                description = value or "(missing or invalid frontmatter description)"
                lines.append((f"{prefix}{connector}{name} - {description}", True))

    walk(tree, "")
    return lines


def render_memories(memories: tuple[Memory, ...]) -> str:
    if not memories:
        return "No memory files yet. Start with `memories/<topic>.md` or `remember <note>`."
    rendered = _memory_tree(memories)
    selected: list[tuple[str, bool]] = []
    for item in rendered:
        if len("\n".join([*(line for line, _ in selected), item[0]])) > MAX_MEMORY_PROMPT_CHARS:
            break
        selected.append(item)
    omitted = len(memories) - sum(is_file for _, is_file in selected)
    if omitted:
        while selected:
            candidate = (
                f"... {omitted} memory files omitted; run `tree memories` to inspect all paths."
            )
            lines = [line for line, _ in selected]
            if len("\n".join([*lines, candidate])) <= MAX_MEMORY_PROMPT_CHARS:
                break
            _, removed_file = selected.pop()
            omitted += int(removed_file)
        marker = f"... {omitted} memory files omitted; run `tree memories` to inspect all paths."
        selected.append((marker, False))
    return "\n".join(line for line, _ in selected)


def view_skill(
    tree: workspace_files.Tree, name: str, file_path: str | None = None, layer: str | None = None
) -> dict[str, Any]:
    """One skill by name, resolved through the layers unless one layer is named."""
    if layer is not None and layer not in LAYERS:
        raise ValueError(f"layer must be one of {', '.join(LAYERS)}")
    catalog = discover_skills(tree)
    skill = catalog.find(name, layer)
    if skill is None:
        where = f"{layer} skill" if layer else "or invalid skill"
        raise ValueError(f"unknown {where} {name!r}")
    root = skill.tree_root
    selected = "SKILL.md" if file_path is None else file_path
    parsed = pathlib.PurePosixPath(selected)
    if (
        parsed.is_absolute()
        or str(parsed) != selected
        or not parsed.parts
        or any(part in (".", "..") for part in parsed.parts)
    ):
        raise ValueError("skill file_path must be canonical and relative to the skill directory")
    path = f"{root}/{selected}"
    source = tree.get(path)
    if source is None:
        raise ValueError(f"skill file does not exist: {_display_path(path, skill.layer)}")
    if len(source.content) > MAX_SKILL_VIEW_BYTES:
        raise ValueError(
            f"skill file exceeds {MAX_SKILL_VIEW_BYTES} bytes; split details into smaller "
            "support files"
        )
    try:
        content = source.content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("skill files must be UTF-8 text") from error
    output: dict[str, Any] = {
        "name": skill.name,
        "layer": skill.layer,
        "path": _display_path(path, skill.layer),
        "content": content,
    }
    if skill.layer == "team":
        output["revision"] = short(skill.revision)
        overriding = catalog.find(name)
        if overriding is not None and overriding.layer == "self":
            output["overridden_by"] = overriding.path
    if skill.upstream is not None:
        output["upstream"] = {
            "path": skill.upstream.path,
            "pinned": skill.upstream.pinned,
            "current": short(skill.upstream.current) if skill.upstream.current else None,
            "state": skill.upstream.state,
        }
    if skill.overrides is not None:
        output["overrides"] = skill.overrides
    if file_path is None:
        support = [
            candidate.removeprefix(f"{root}/")
            for candidate in sorted(tree)
            if candidate.startswith(f"{root}/") and candidate != f"{root}/SKILL.md"
        ]
        listed: list[str] = []
        for candidate in support[:MAX_SKILL_FILES]:
            if len("\n".join([*listed, candidate])) > MAX_SKILL_FILES_CHARS:
                break
            listed.append(candidate)
        output["files"] = listed
        omitted = len(support) - len(listed)
        if omitted:
            output["files_omitted"] = omitted
    return output
