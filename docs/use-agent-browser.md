# Use agent-browser

Use `agent-browser` to reproduce and inspect behavior on the live Hatchery deployment.

## Authenticate

Use the persistent profile at `~/.agent-browser/hatchery-vercel`. If it is not authenticated, ask the user to open a headed browser with that profile, complete Vercel login, and close the browser to release the profile lock.

## Inspect Hatchery

Run all commands in one persistent shell so the browser session survives:

```sh
export AGENT_BROWSER_SESSION="$(agent-browser session id --scope worktree --prefix hatchery-live)"
export AGENT_BROWSER_PROFILE="$HOME/.agent-browser/hatchery-vercel"

agent-browser open https://hatchery-prod.playground-vercel.tools
agent-browser get text body
```

Prefer semantic locators:

```sh
agent-browser find role button click --name "New chat"
agent-browser find placeholder "What should we build?" fill "<prompt>"
agent-browser find role button click --name "Submit"
agent-browser wait 30000
agent-browser get text body
```

Check the page until the task is complete. Record the `chat_...`, `task_...`, and `wrk_...` IDs for correlation in Braintrust. Close the browser when done:

```sh
agent-browser close
```

If the page becomes `about:blank` or references go stale, confirm every command is running in the same persistent shell, then inspect `agent-browser session info --json` and `agent-browser tab list`.
