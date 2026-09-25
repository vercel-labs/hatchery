---
name: coding
description: Change code in a repository safely - explore, plan, edit, verify.
---

# Coding

1. **Orient.** `tree <dir>` for the layout, `search <pattern>` for the code that
   matters, then read the relevant files whole. Check `memories/` for notes about
   this project.
2. **Plan before editing.** State the change in one or two sentences. If it touches
   more than a handful of files, write the plan to `memories/<project>.md` first.
3. **Edit precisely.** Use `edit <file> <old> <new>` for surgical replacements; it
   refuses ambiguous matches. Rewrite a file only when most of it changes.
4. **Verify.** Run the project's tests, type checker, and linter. Read the failing
   output instead of guessing. Do not declare success without evidence.
5. **Record.** Note anything that surprised you or that you will need again in
   `memories/`. If your team's coding procedure differs from this one, write it as a
   team or agent skill; this runtime skill cannot be edited.

Do not commit or push the shared memory repository under `self/` or `wiki/`; the
runtime checkpoints it. Coding repositories under `/workspace/repos` use their
normal branches, commits, pushes, and pull requests.

## Identity and credentials

The runtime brokers GitHub credentials for coding repositories at the sandbox network
boundary, so `git` and `gh` work without a token in any command or file. Never change
the Git identity, never paste tokens into commands or files, and never work around a
permission error; report it instead.

Open pull requests on a branch a teammate would name, and share the URL when it is
open.
