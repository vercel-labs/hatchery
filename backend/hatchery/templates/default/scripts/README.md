# scripts/

Your own executable helpers. This directory is first on your `PATH`, so call them by
name from any bash command. Keep each script small, print usage with `--help`, mark it
executable (`chmod +x`), and pin shared Python dependencies in `../requirements.txt`.

The runtime helpers below ship with Hatchery and follow this directory on `PATH`.
They stay current with the runtime, so do not copy them here; an agent script with
one of these names hides the runtime version and is flagged in your system prompt.

| Helper | Use it for |
| --- | --- |
| `tree [dir] [-L depth]` | A compact directory listing that skips build and VCS noise. |
| `search <regex> [path]` | Grep the tree for a pattern with file:line context. |
| `edit <file> <old> <new>` | Replace one exact occurrence of `old` with `new`; refuses ambiguity. |
| `fetch <url>` | Download a page as readable text (HTML stripped). |
| `remember <note>` | Append a dated line to `memories/journal.md`. |
