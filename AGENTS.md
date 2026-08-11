# e2e-bot

vercel has python sdks that match the javascript sdks:

- workflow
- sandbox
- connect
- blob
- oidc

those consume the same backend api.

this is a github bot that automatically detects new e2e tests for this sdk to backend
interaction, both in js and python, and makes a pr that creates a counterpart.

1. deplyed to vercel, uses vercel cron for periodic invocation
2. fastapi
3. dogfoods ai sdk for python, workflows, sandbox

is foundation for the future `chat` sdk that has connectors for github and other places.

## what it does

1. durable agent built on workflows and ai sdk for python
2. on agent invocation, a sandbox is spun up, repos are cloned into it on startup
3. the agent compares e2e tests across repos
4. if there's discrepancy, the agent opens an issue and optionally makes a pr
5. this happens once a day

## answer style

be brief, use simple terse language, do not use jargon. this helps with efficiency of communication.
do not overcomplicate. this is a test application, it should prioritize clarity.

## code guidelines

1. in python, import by module (unless it's `typing`) to improve namespacing and make it read to navigate code.
2. minimize the number of helper functions, prioritize locality of behavior.
3. keep apis as small as possible. keep public apis even smaller, try to shrink them to one function / object.
4. test file structure should mirror app's file structure, e.g. `agent/proto.py` -> `tests/agent/test_proto.py`. this helps project navigation a lot.

## project setup

1. use uv to manage python
2. use pnpm to manage typescript


