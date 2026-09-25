# skills/

A skill is a reusable procedure: how to do one kind of task well. Each skill is a
directory containing a `SKILL.md` with YAML front matter:

```markdown
---
name: release-checklist
description: Cut a release of the atlas service, from changelog to tag.
---

# Release checklist
1. ...
```

The runtime puts every valid skill's `name`, `description`, and layer in your system
prompt. Names are lowercase hyphenated slugs and must be unique within a layer. When a
task matches, call `skill_view` with that exact name before starting. The result
contains the complete guide and a support-file inventory; pass a relative `file_path`
to load one support file. Put supporting scripts or references beside `SKILL.md` in
the same directory.

Keep `SKILL.md` and each support file below 24 KiB. Split detailed material into
focused support files rather than relying on truncated procedural instructions.

## Layers

- **Runtime** skills ship with Hatchery (`api`, `schedules`, `coding`, ...). They
  document the platform, are always current, and cannot be replaced; a skill here with
  a runtime name is ignored.
- **Team** skills live in `/workspace/wiki/skills/<name>/SKILL.md`. Every agent sees
  them, and edits become wiki proposals for review.
- **Agent** skills live here. A skill with a team skill's name overrides it.

To override or fork a team skill, read it with `skill_view(name, layer="team")`, note
the `revision`, and record it in your frontmatter:

```yaml
upstream: wiki/skills/release-checklist/SKILL.md@3f2a1c9d8e01
```

When the team skill changes, the catalog shows `upstream changed` with the new
revision until you update the pin. Prefer a short override that says "follow the team
skill, with these differences" over a full copy; it stays correct when upstream moves.

Write a new skill when you have done something twice and expect to do it again.
Keep the description to one sentence that says *when* to use the skill.
