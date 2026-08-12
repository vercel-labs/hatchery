# fabricator

Daily e2e parity bot for Vercel's JavaScript and Python SDKs.

## Setup

Install dependencies and link this checkout to the Vercel project:

```sh
uv sync
vercel link --project fabricator
```

Create the Slack connector. Vercel opens a browser to install the Slack app.

```sh
vercel connect create slack --name fabricator --triggers
```

Create the GitHub connector. In the browser flow, name the GitHub App
`fabricator-bot` and install it on the repositories it should access.

```sh
vercel connect create github --name fabricator --triggers
```

The connector UIDs are:

```text
slack/fabricator
github/fabricator
```

Creating a connector from a linked checkout attaches it to the project with a
default trigger route. Detach and attach it again to set this app's routes:

```sh
vercel connect detach slack/fabricator --yes
vercel connect attach slack/fabricator \
  --triggers \
  --trigger-path /chat/v1/slack \
  --yes

vercel connect detach github/fabricator --yes
vercel connect attach github/fabricator \
  --triggers \
  --trigger-path /chat/v1/github \
  --yes
```

## Environment variables

Connect normally adds the connector variables during setup. Check them:

```sh
vercel env ls --project fabricator
```

The final values must be:

```text
SLACK_CONNECTOR=slack/fabricator
GITHUB_CONNECTOR=github/fabricator
GITHUB_APP_SLUG=fabricator-bot
```

`GITHUB_APP_SLUG` is the GitHub App name used in mentions, not the connector
UID. Add it manually:

```sh
vercel env add GITHUB_APP_SLUG \
  production,preview \
  --project fabricator \
  --value fabricator-bot \
  --yes
```

If it already exists, update it instead:

```sh
vercel env update GITHUB_APP_SLUG \
  --project fabricator \
  --value fabricator-bot \
  --yes
```

Only add the connector variables manually if setup did not create them:

```sh
vercel env add SLACK_CONNECTOR production,preview,development \
  --project fabricator \
  --value slack/fabricator \
  --yes

vercel env add GITHUB_CONNECTOR production,preview \
  --project fabricator \
  --value github/fabricator \
  --yes
```

Connect owns the provider credentials and mints short-lived tokens. Do not add
GitHub or Slack tokens.

## Deploy and point triggers

Deploy before pointing triggers at a branch:

```sh
vercel --prod
```

The trigger script detaches and reattaches both connectors. On `main` it sends
events to production. On any other branch it sends them to that branch's latest
preview deployment.

```sh
./scripts/triggers.sh
```

Create the preview deployment before running the script on a feature branch:

```sh
vercel
./scripts/triggers.sh
```

## Remove old connectors

After the new Slack and GitHub paths work, delete the old connectors:

```sh
vercel connect remove github/e2e-bot --disconnect-all --yes
vercel connect remove slack/e2e-bot --disconnect-all --yes
vercel connect list --all-projects
```
