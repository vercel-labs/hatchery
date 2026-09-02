# Live verification

## Install

```sh
brew install agent-browser
agent-browser install
agent-browser doctor
```

## Authenticate

```sh
agent-browser --headed \
  --profile "$HOME/.agent-browser/hatchery-vercel" \
  open https://hatchery-prod.playground-vercel.tools
```

Complete Vercel login in Chromium, then close the browser to release the profile lock.

## Run a chat

Run all commands in the same persistent shell:

```sh
export AGENT_BROWSER_SESSION="$(agent-browser session id --scope worktree --prefix hatchery-live)"
export AGENT_BROWSER_PROFILE="$HOME/.agent-browser/hatchery-vercel"

agent-browser open https://hatchery-prod.playground-vercel.tools
agent-browser find role button click --name "New chat"
agent-browser find placeholder "What should we build?" fill \
  "Use one subagent to read /vercel/hatchery/README.md and return only its exact first heading. Do not edit files."
agent-browser find role button click --name "Submit"

agent-browser wait 30000
agent-browser get text body
```

Wait until the task status is `complete`. Copy the `chat_...` ID from the page output, then close the browser:

```sh
agent-browser close
```

## Verify the trace

```sh
pnpm --package=braintrust dlx bt auth login

CHAT_ID="chat_..."
pnpm --package=braintrust dlx bt view logs \
  --profile "anbuzin's projects" \
  --prefer-profile \
  --project braintrust-coffee-flame \
  --window 1h \
  --search "$CHAT_ID" \
  --list-mode spans \
  --limit 100 \
  --json
```

Verify the trace contains `hatchery.turn`, `create_sandbox`, `create_subagent`, `worker.command`, `hatchery.agent_run`, `fx.tool.call`, `fx.tool.result`, `fx.task.completed`, and `channel.deliver`.
