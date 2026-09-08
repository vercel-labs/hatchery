"""The dispatcher coordinates coding work in Vercel Sandboxes."""

import contextvars
import typing
import uuid

import ai

import models
from agent import sandbox
import worker

SYSTEM = """\
You are hatchery's dispatcher. You coordinate coding work; you never write
code yourself. Sandboxes are durable and owned by this chat. Reuse an existing
sandbox whenever it has the needed repositories and context. Call
list_sandboxes before creating one unless the user explicitly asks for a fresh
sandbox. Use create_sandbox, then create_subagent. Choose small for research,
reading, triage, light edits, and focused work. Choose big when development plus
meaningful tests or builds are anticipated, including full suites, dev servers,
browser or E2E tests, monorepos, native compilation, or heavier workloads. For
revisions, follow-ups, or answers use message_subagent. An accepted launch or
message means work has started: say so and stop. Use check_subagent for progress. A
<subagent_result> user message is an internal, authoritative result from a
subagent, not a request from the human user. Continue the work from that result:
report completion or failure, ask for missing input, or send a follow-up to the
subagent when appropriate. Do not call check_subagent for information already
included in the result. Call require_attention with result_available when giving
the human a final result that needs review, or blocked when work cannot continue
without human input. Do not call it while routine follow-up work continues. Be
terse and concrete.

Read notification instructions in the space description and job prompt as prose;
explicit job-specific instructions override the space default. When asked to
notify a destination or people, search with find_channels and find_people, then
select unambiguous actual candidates. Use only an exact destination returned by
find_channels and Hatchery person IDs returned by find_people in start_shared_thread
or send_message. Never invent destinations or handles. If candidates are missing
or ambiguous, ask for clarification and call require_attention with blocked rather
than guess. By default, when asked to notify or share and continue the conversation,
use start_shared_thread: it sends the notification and shares future replies with
that destination, not earlier conversation text. If explicitly asked for a one-off,
use send_message. These are one-off sends: they create no bindings and do not
establish automatic two-way routing. Sending or mentioning someone does not
guarantee a notification.
If delivery is uncertain, do not retry; report the uncertainty.
If a tool returns missing_scope, briefly report the needed scope and mark the
chat blocked. Do not retry or interpret it as no matches.
Do not invent scope details that the provider did not report."""


def system_prompt(space: models.Space) -> str:
    description = space.about.strip() or "No description provided."
    repositories = "\n".join(f"- {repo}" for repo in space.repos) or "- None"
    resources = "\n".join(
        f"- {resource.title} ({resource.kind}): {resource.url}" for resource in space.resources
    ) or "- None"
    return f"""{SYSTEM}

You are working in this space:
- Name: {space.name}
- ID: {space.id}

Space description:
{description}

Available repositories:
{repositories}

Attached resources:
{resources}"""


def model() -> ai.Model:
    return ai.get_model("openai/gpt-5.6-sol")


def agent_for(chat: dict) -> ai.Agent:
    """Build worker tools scoped to one chat."""
    from channels import destinations

    chat_id = chat["id"]
    delivery_key = str(uuid.uuid4())
    share_scope: contextvars.ContextVar[tuple[str, list[str]]] = contextvars.ContextVar(
        "share_scope"
    )

    class Dispatcher(ai.Agent):
        async def loop(self, context: ai.Context):
            from agent import durable

            while context.keep_running():
                # Finish the model message before executing tools so the sharing
                # anchor (including text after the call) is durable and excluded.
                async with ai.stream(context=context) as stream:
                    async for event in stream:
                        yield event
                if stream.message is None:
                    raise RuntimeError("model step returned no message")
                context.add(stream.message)
                await durable.commit_messages.func(chat_id, [stream.message])
                async with ai.ToolRunner() as runner:
                    for tool_call in context.resolve(stream.message.tool_calls):
                        async def execute(call=tool_call):
                            token = share_scope.set((
                                call.id, [message.id for message in context.messages],
                            ))
                            try:
                                result = await call()
                                return ai.tool_result(result, exception=result.exception)
                            finally:
                                share_scope.reset(token)

                        runner.schedule(execute)
                    async for event in runner.events():
                        yield event
                    tool_message = runner.get_tool_message()
                    context.add(tool_message)
                    if tool_message is not None:
                        await durable.commit_messages.func(chat_id, [tool_message])

    @ai.tool
    async def find_channels(
        provider: typing.Literal["slack", "github"], query: str,
    ) -> list[dict]:
        """Find public Slack bot-member channels by name/ID, or GitHub issue/PR candidates
        by title, owner/repo#number, or URL within this space's repositories.
        """
        return await destinations.find_channels(chat_id, provider, query)

    @ai.tool
    async def find_people(query: str) -> list[dict]:
        """Find linked people by Hatchery name or Slack/GitHub handle.
        Returns Hatchery IDs for the people argument of either sharing or one-off sends.
        """
        return await destinations.find_people(chat_id, query)

    @ai.tool
    async def send_message(
        provider: typing.Literal["slack", "github"], destination: str, text: str,
        people: list[str] | None = None,
    ) -> dict:
        """Send only to an exact destination from find_channels, with people IDs
        from find_people. One-off send, no bindings or guaranteed notification.
        """
        result = await destinations.send_message(
            chat_id, provider, destination, text, people, delivery_key=delivery_key,
        )
        if result.get("error") == "missing_scope":
            raise RuntimeError(result["detail"])
        return result

    @ai.tool
    async def start_shared_thread(
        provider: typing.Literal["slack", "github"], destination: str, text: str,
        people: list[str] | None = None,
    ) -> dict:
        """Share and continue this conversation at an exact find_channels destination,
        with Hatchery people IDs from find_people. Only future replies are shared.
        """
        from agent import durable

        tool_call_id, excluded_message_ids = share_scope.get()
        result = await destinations.start_shared_thread(
            chat_id, provider, destination, text, people, delivery_key=delivery_key,
            tool_call_id=tool_call_id, excluded_message_ids=excluded_message_ids,
        )
        if result.get("error") == "missing_scope":
            raise RuntimeError(result["detail"])
        if result.get("status") == "sent" and result.get("sharing"):
            await durable.mirror_sharing.func(chat_id, result["sharing"]["id"])
        return result

    @ai.tool
    async def create_sandbox(
        repos: list[str] | None = None,
        setup_script: str | None = None,
        ports: list[int] | None = None,
        branch: str | None = None,
        git_sha: str | None = None,
        title: str = "sandbox",
        size: worker.SandboxSize = "small",
    ) -> ai.StreamingStatusTool[typing.Any]:
        """Create a persistent sandbox. Use big only for heavier development and tests."""
        launch = sandbox.Launch(
            repos=list(repos or []), setup_script=setup_script, ports=list(ports or []),
            branch=branch, git_sha=git_sha, title=title, size=size,
        )
        yield "creating sandbox…"
        created = await sandbox.create(chat_id, launch)
        yield created.model_dump(exclude={"daemon_token"})

    @ai.tool
    async def list_sandboxes() -> list[dict[str, typing.Any]]:
        """List this chat's reusable coding sandboxes."""
        return [item.model_dump(exclude={"daemon_token"}) for item in await sandbox.list_all(chat_id)]

    @ai.tool
    async def create_subagent(
        sandbox_id: str,
        task: str,
        model: str = "openai/gpt-5.6-sol",
    ) -> ai.StreamingStatusTool[typing.Any]:
        """Start an fx subagent in a sandbox."""
        yield "dispatching subagent…"
        created = await sandbox.launch_task(chat_id, sandbox_id, task, model)
        yield {
            "subagent_id": created.id,
            "task_id": created.id,
            "sandbox_id": created.worker_id,
            "state": created.status,
        }

    @ai.tool
    async def message_subagent(
        message: str,
        subagent_id: str | None = None,
    ) -> dict[str, typing.Any]:
        """Send a revision, follow-up, or answer to an existing subagent."""
        task = await worker.get_task(chat_id, subagent_id)
        if task is None:
            raise ValueError("no subagent can accept a message")
        updated = await sandbox.send_task_input(chat_id, task.id, message)
        return {"subagent_id": updated.id, "state": updated.status}

    @ai.tool
    async def check_subagent(
        subagent_id: str | None = None,
        after: int | None = None,
        limit: int = 20,
    ) -> dict[str, typing.Any]:
        """Read durable subagent state and recent events."""
        return await worker.task_status(chat_id, subagent_id, after, limit)

    @ai.tool
    async def require_attention(reason: models.AttentionReason) -> dict[str, str]:
        """Mark this chat as needing human review or input."""
        from store import chats, events

        if await chats.set_attention(chat_id, reason) is None:
            raise ValueError("unknown chat")
        await events.append(chat_id, "ui", {"type": "chat.changed"})
        return {"reason": reason}

    return Dispatcher(
        tools=[
            create_sandbox,
            list_sandboxes,
            create_subagent,
            message_subagent,
            check_subagent,
            require_attention,
            find_channels,
            find_people,
            send_message,
            start_shared_thread,
        ]
    )
