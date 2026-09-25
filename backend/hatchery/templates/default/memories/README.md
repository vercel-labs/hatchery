# memories/

Notes for your future self. The runtime puts each file's path and frontmatter
`description` in a bounded tree in the system prompt; file contents remain on demand.
Read what you need at the start of a task (`tree memories`, then `cat` the relevant file).

Use root `USER.md` for shared team context and interaction defaults, and root
`MEMORY.md` for stable facts and conventions useful in nearly every task. Those files
are always loaded and do not belong in this topic index.

Start every memory file with YAML frontmatter followed by its ordinary Markdown:

```markdown
---
description: >-
  Deployment procedures, rollback steps, and production verification for Atlas.
  Read before changing or investigating an Atlas deployment.
---

# Atlas deployments
```

The path is the memory's identity, so no separate `name` is needed. Write a concrete
routing description that says what the file contains and when it is relevant. Keep it
under 500 characters and update it when the file's purpose changes. Files with missing
or invalid metadata stay visible in the tree as needing repair.

Conventions:

- One topic per file, in Markdown, named `kebab-case.md` (for example
  `deploy-process.md`, `people.md`, `project-atlas.md`).
- Record facts, decisions, and their reasons. Skip transcripts and scratch work.
- `journal.md` is append-only: `remember "what happened"` adds a dated line.
- Rewrite files when they get stale; delete what is no longer true.

Everything here is shared with the team through Git. Never store secrets or
private personal details.
