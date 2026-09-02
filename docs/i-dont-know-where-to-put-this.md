# I don't know where to put this

## From `docs/arch/sandbox.md`

### Maybe bugs

- With Vercel CLI 59.10.0 and `vercel-queue` 0.7.3, the local Queue broker can return a message ID from `queue.send()` while an immediate `queue.poll()` of the same fresh topic returns no messages.
- The issue reproduces when both calls run in one process with the `VERCEL_QUEUE_BASE_URL`, `VERCEL_QUEUE_TOKEN`, and `VERCEL_REGION` supplied by `vercel dev`.

## From `agent-browser.md`

# Agent Browser live dispatcher experiment

## Goal

Use agent-browser against the protected live Hatchery deployment to submit one dispatcher turn and verify its trace in Braintrust.

## Result

The corrected 2026-08-31 run completed successfully with `agent-browser 0.35.2`.

- Deployment: `https://hatchery-prod.playground-vercel.tools`
- Marker: `ab-e2e-1788218092`
- Chat: `chat_bf624b59f109`
- Sandbox: `wrk_2524a9d0ab1e`
- Task: `task_8b9e50c5aa1d`
- Result: `# hatchery`

Authentication succeeded through the dedicated persistent Chromium profile. The browser was kept alive through one persistent terminal session, `New chat` worked across separate agent-browser commands, and the dispatcher completed its turn.

The Braintrust trace was found by chat ID. Its root trace was `8a7e0816d2d4eb01472ff97e84d28ca2`, rooted at `hatchery.turn`.

## Authentication

A dedicated persistent profile was created with a headed browser:

```sh
npx --yes agent-browser \
  --headed \
  --profile ~/.agent-browser/hatchery-vercel \
  open https://hatchery-1rx25zkvo.playground-vercel.tools
```

Vercel login was completed manually. Credentials, cookies, and tokens were not passed through the agent or written to the repository.

The browser window must be closed after login so its profile lock is released. The authenticated state remains in the profile for later runs.

## Failures and friction

- `agent-browser` was not installed globally. `npx --yes agent-browser` worked as a fallback.
- The live deployment URL had to be found through the GitHub deployments API.
- The first retry failed because the headed login browser still owned the profile's `SingletonLock`.
- `--auto-connect` could not attach because the running browser had no remote-debugging port.
- After closing the login browser, the persistent profile authenticated successfully and loaded Hatchery.
- The initial snapshot showed the `New chat` button, but its element reference was unavailable in the next invocation.
- Semantic lookup also failed because the session's active page had changed to `about:blank`.
- Reusing the same session with a cached binary did not resolve the problem.
- Supplying `--profile` on later commands produced warnings that it was ignored because a daemon was already running.
- The Homebrew retry used `agent-browser 0.35.2`; `agent-browser doctor` passed 8 checks with 0 warnings and 0 failures.
- The apparent daemon/session failure was caused by running each browser command in a separate foreground terminal execution. This environment cleans up child processes after each execution, including the agent-browser daemon.
- Keeping one persistent terminal session fixed the issue. The named browser session and active page then survived across commands.
- The correct tab command is `agent-browser tab list`, not `agent-browser tabs`.
- The exact quick-start URL supplied later returned HTTP 404. The installed, version-matched guide from `agent-browser skills get core --full` was usable.
- The specialized `protected-vercel-deployments` skill should have been loaded before the first attempt. The existing authenticated profile worked, so no OIDC or static bypass secret was needed for this run.
- The first dispatcher `create_sandbox` call used a setup script with `/home/vercel-sandbox/hatchery`, which failed with exit status 2. The dispatcher recovered by creating the sandbox without that setup script.
- The initial subagent prompt also used the stale `/home/vercel-sandbox/hatchery/README.md` path. The checkout was actually at `/vercel/hatchery`; a follow-up message recovered the run.
- Waiting for visible text `# hatchery` timed out because the UI rendered the final answer as `hatchery`. Reading the page body and task status was more reliable.

## Braintrust verification

- Root trace: `8a7e0816d2d4eb01472ff97e84d28ca2`
- Root span: `hatchery.turn`
- Deployment commit: `d5e47dbfc29426524bc0ca10b7181cb9b1820bfe`
- Duration: about 66 seconds
- LLM calls: 9
- Tool calls: 11
- LLM errors: 0
- Tool errors: 1
- Estimated model cost: `$0.064222`

The trace showed the failed setup-script sandbox provision, successful fallback provision, subagent creation, failed reads from the stale `/home/vercel-sandbox/hatchery` path, the corrective message, successful `read_file` from `/vercel/hatchery/README.md`, `fx.task.completed`, dispatcher final output, and successful `channel.deliver`.

## Comparison with Playwright

The Playwright experiment in `playwright.md` was more reliable for this stateful end-to-end test.

| Area | agent-browser | Playwright |
| --- | --- | --- |
| Authentication | Manual persistent browser profile | One browser context for the whole run |
| Process model | Multiple CLI calls communicating with a daemon | One Node process owned the browser and page |
| Page state | Stable when run inside one persistent terminal execution | Page remained stable |
| Element access | Snapshot refs and semantic locators worked | Stable locators |
| Installation | Homebrew installation passed all doctor checks | Temporary installation required more setup |
| Dispatcher result | Completed successfully | Completed successfully |
| Braintrust result | Complete trace verified | Complete trace verified |

Both tools completed the stateful authenticated flow. For this CLI environment, agent-browser must run inside one persistent terminal session so its daemon is not cleaned up between commands.

## Recommended agent-browser workflow

Install one stable version instead of invoking it through an ephemeral `npx` environment:

```sh
npm install -g agent-browser
agent-browser install
agent-browser doctor
```

Close stale agent-browser sessions before beginning a new run:

```sh
agent-browser close --all
```

Open one persistent shell, export the profile and generated session once, then run all browser commands inside that shell:

```sh
export AGENT_BROWSER_SESSION="$(agent-browser session id --scope worktree --prefix hatchery-dx)"
export AGENT_BROWSER_PROFILE="$HOME/.agent-browser/hatchery-vercel"
agent-browser open https://hatchery-prod.playground-vercel.tools
```

Prefer semantic locators for important controls instead of carrying snapshot refs across commands:

```sh
agent-browser --session hatchery-dx \
  find role button click --name "New chat"
```

If the active page changes to `about:blank`, first verify that the host execution wrapper is not terminating the daemon between commands. Use `agent-browser session info --json` and `agent-browser tab list` inside the same persistent shell.

## Suggested product improvements

- Document whether global flags such as `--profile` must be repeated on every command.
- Make daemon, profile, session, and tab ownership visible in status output.
- Preserve the active tab reliably across commands.
- Report when a command attaches to a different daemon or blank page.
- Explain why a profile flag is ignored and provide a safe restart command.
- Offer a single-process script or transaction mode for stateful multi-step flows.
- Make stale references distinguishable from references belonging to another page or daemon.

## Repository impact

This document is the only intended repository change from this experiment. No application code was changed.

## From `playwright.md`

# Playwright live dispatcher experiment

## Goal

Use browser automation against the live Vercel production deployment to submit one dispatcher turn, wait for its subagent, and verify the complete trace in Braintrust.

## Result

The run completed successfully.

- Deployment: `https://hatchery-prod.playground-vercel.tools`
- Deployment ID: `dpl_81UFKza7DrYYbojnTXxzYVbrDcAd`
- Marker: `pw-e2e-1788216095110`
- Chat: `chat_9c893c92d289`
- Sandbox: `wrk_ad4ba4a2ac81`
- Started: `2026-08-31T22:41:36Z`
- Final answer appeared: `2026-08-31T22:42:03Z`
- Result: the exact first heading in `README.md` was `# hatchery`

The run created a fresh sandbox, launched a read-only subagent, read `README.md`, returned the heading, and delivered the dispatcher’s final response.

## Authentication

The deployment is protected by Vercel SSO. Playwright used the automation bypass secret stored in macOS Keychain under:

```text
triangle/VERCEL_AUTOMATION_BYPASS_SECRET
```

The secret was read at runtime and sent as an HTTP header:

```text
x-vercel-protection-bypass: <secret>
```

It was not printed, placed in a URL, or written to the repository.

## Braintrust verification

- Project: `braintrust-coffee-flame`
- Root trace: `d22aaf8ac10fece617a7521149481289`
- Root span: `hatchery.turn`
- Deployment commit: `d5e47dbfc29426524bc0ca10b7181cb9b1820bfe`
- LLM calls: 5
- Dispatcher tool calls: 4
- Tool errors: 0
- LLM errors: 0
- Reported duration: about 24 seconds
- Estimated model cost: `$0.0468828`

The trace contained the expected chain:

1. The dispatcher received the UI message.
2. `create_sandbox` provisioned a fresh sandbox.
3. `create_subagent` launched the task.
4. `worker.command` delivered it.
5. `hatchery.agent_run` started.
6. fx called `read_file` on `README.md`.
7. The tool result showed line 1 as `# hatchery`.
8. fx returned `# hatchery`.
9. `fx.task.completed` was recorded.
10. The dispatcher produced the final response.
11. `channel.deliver` completed without failures.

## Gaps and friction

- `README.md` points toward local `vercel dev` and a public reverse proxy. That was misleading for a live-deployment browser test.
- Live deployment authentication and the Keychain-backed automation bypass are not documented in `docs/`.
- Playwright is not installed in the project.
- `pnpm dlx playwright` exposes the CLI but does not make `@playwright/test` importable by a temporary script. The runner had to be installed in a temporary directory.
- The automation appeared to hang after the application had already completed. Its completion condition expected two assistant messages, but the messages API merged the initial and final dispatcher responses into one updated assistant message.
- Braintrust free-text search found the run by `chat.id`, but not by the marker embedded in the user prompt. Metadata correlation is more reliable.
- Braintrust summary and span-list views showed confusing root-span timing. Async child spans extended beyond one displayed root end time.
- A later standalone curl was redirected to SSO even though the same Keychain secret worked in Playwright and earlier curl checks. Bypass behavior across isolated requests may need investigation.

## Repository impact

No application code was changed. Playwright and its test runner were installed only in temporary directories.

## From `hatchery-suggestion.md`

python software factory

## goal

run unattended, make prs, issues notifications to slack

## recommended setup

install uv in the sandbox

main repo is in `/vercel/hatchery`, link it with

```bash
vercel link --yes --project hatchery --scope vercel-internal-playground
```

install agent-browser for subagents to verify their work:

```bash
mkdir -p "$HOME/.local"
npm install --global --prefix "$HOME/.local" agent-browser@0.35.2

export PATH="$HOME/.local/bin:$PATH"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"

agent-browser install --with-deps
```
