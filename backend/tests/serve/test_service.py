"""Serving published routes from `main` through the one Hatchery app, with real Git,
the durable runtime, and the real SDK in a subprocess. Ported from the serve and secret
parts of agentmesh `tests/integration/test_gateway.py` and `test_thread.py`.
"""

import json

import ai
import ai.testing

from hatchery import vault
from hatchery.agent import tools
from hatchery.serve import service
from hatchery.store import chats, events
from hatchery.workspace import files

from tests.agent import conftest as agent_conftest
from tests.serve import conftest

call = ai.testing.tool_call
AGENT = conftest.AGENT
ASK = "Publish a greeting endpoint"
GREETING = (
    '"""Greet someone by name."""\n'
    "def GET(request):\n"
    "    return {'hello': request.params['name']}\n"
)


def source(text: str) -> files.File:
    return files.File(text.encode())


# Phase 6 gate


async def test_a_reviewed_route_goes_live_without_a_redeploy_and_an_unreviewed_one_does_not(
    serve: conftest.Serve, repo
) -> None:
    script = [
        ai.user_message(ASK),
        ai.assistant_message(
            call(tools.bash, command="publish-route"), call(tools.idle, note="route ready")
        ),
    ]
    commands = {"publish-route": agent_conftest.edit("self/api/hello/[name]/route.py", GREETING)}
    async with serve(script, commands=commands) as app:
        chat_id = await app.chat(ASK)
        [proposal] = (await app.details(chat_id))["proposals"]
        assert (proposal["section"], proposal["merged"]) == ("workspace", False)

        # Unreviewed: the thread branch has the route, main does not, so nothing serves it.
        async with app.public() as public:
            assert (await public.get("/api/hello/ada")).status_code == 404
        listed = (await app.app.get(f"/api/agents/{AGENT}/routes")).json()
        assert listed["routes"] == []
        assert app.sandboxes.serve_sandboxes() == []

        # Reviewed: the operator approves the exact commit; the same app serves it now.
        approved = await app.app.post(
            f"/api/chats/{chat_id}/thread/approve",
            json={"branch": proposal["branch"], "expected_sha": proposal["sha"]},
        )
        assert approved.status_code == 200, approved.text
        async with app.public() as public:
            live = await public.get("/api/hello/ada")
        assert live.status_code == 200
        assert live.json() == {"hello": "ada"}
        assert live.headers["cache-control"] == "no-store"
        listed = (await app.app.get(f"/api/agents/{AGENT}/routes")).json()
        assert listed["revision"] == approved.json()["commit"]
        assert listed["routes"] == [
            {
                "path": "/api/hello/[name]",
                "methods": ["GET"],
                "url": f"http://{AGENT}.localhost/api/hello/[name]",
                "description": "Greet someone by name.",
                "available": True,
                "error": None,
            }
        ]

        # A later merge redeploys the same serve sandbox on the next request.
        revision = await app.merge_tree(
            AGENT,
            {
                "self/api/hello/[name]/route.py": source(
                    "def GET(request):\n    return {'hi': request.params['name']}\n"
                )
            },
            "rename-greeting",
        )
        async with app.public() as public:
            assert (await public.get("/api/hello/grace")).json() == {"hi": "grace"}
        [sandbox] = app.sandboxes.serve_sandboxes()
        assert sandbox.inspection_files[".hatchery/serve-revision"] == revision.encode()
        status = (await app.app.get(f"/api/agents/{AGENT}/serve")).json()
        assert status["revision"] == status["deployed_revision"] == revision
        assert status["sandbox"] == sandbox.name and status["retired"] is False


async def test_secrets_stay_out_of_transcripts_and_reach_only_their_agents_handlers(
    serve: conftest.Serve,
) -> None:
    value = "whsec_private_value_17"
    script = [
        ai.user_message("Handle Stripe webhooks"),
        ai.assistant_message(
            call(
                tools.secret_request,
                name="STRIPE_WEBHOOK_SECRET",
                note="Paste the endpoint signing secret from Stripe.",
            ),
            call(tools.idle, note="waiting for configuration"),
        ),
    ]
    probe = (
        "import os\n"
        "def GET(request):\n"
        "    return {'stripe': os.environ.get('STRIPE_WEBHOOK_SECRET') == '%s',\n"
        "            'other': 'OTHER_TOKEN' in os.environ}\n" % value
    )
    async with serve(script) as app:
        chat_id = await app.chat("Handle Stripe webhooks")
        details = await app.details(chat_id)
        assert app.tool_results(details)[0] == {"name": "STRIPE_WEBHOOK_SECRET", "status": "requested"}
        listed = (await app.app.get(f"/api/agents/{AGENT}/secrets")).json()
        assert listed == {
            "secrets": [
                {
                    "name": "STRIPE_WEBHOOK_SECRET",
                    "stored": False,
                    "updated_at": None,
                    "note": "Paste the endpoint signing secret from Stripe.",
                }
            ]
        }

        stored = await app.app.post(
            f"/api/agents/{AGENT}/secrets/STRIPE_WEBHOOK_SECRET",
            json={"request_id": "secret-set-1", "value": value},
        )
        assert stored.status_code == 200 and value not in stored.text
        created = await app.app.post("/api/agents", json={"name": "Other", "id": "other"})
        assert created.status_code == 200, created.text
        await app.app.post(
            "/api/agents/other/secrets/OTHER_TOKEN",
            json={"request_id": "secret-set-other", "value": "other-private"},
        )
        await app.rt.drain()
        await app.merge_tree(AGENT, {"self/api/probe/route.py": source(probe)}, "probe")
        await app.merge_tree("other", {"self/api/probe/route.py": source(probe)}, "probe-other")

        async with app.public() as public:
            assert (await public.get("/api/probe")).json() == {"stripe": True, "other": False}
        async with app.public("other") as public:
            assert (await public.get("/api/probe")).json() == {"stripe": False, "other": True}

        # The serve sandbox of each agent received only that agent's values.
        serving = service.current()
        by_name = {s.name: s for s in app.sandboxes.serve_sandboxes()}
        mine = by_name[serving.sandbox_name(AGENT)].environments[-1]
        theirs = by_name[serving.sandbox_name("other")].environments[-1]
        assert mine["STRIPE_WEBHOOK_SECRET"] == value and "OTHER_TOKEN" not in mine
        assert theirs["OTHER_TOKEN"] == "other-private" and "STRIPE_WEBHOOK_SECRET" not in theirs
        assert mine["HATCHERY_DATA"] == mine["HOME"] == "/workspace/data"

        # The value is nowhere the agent or a transcript can see it.
        thread_envs = [
            environment
            for sandbox in app.sandboxes.sandboxes.values()
            if sandbox not in app.sandboxes.serve_sandboxes()
            for environment in sandbox.environments
        ]
        seen = json.dumps(
            {
                "transcript": await events.read(chat_id, "messages"),
                "details": await app.details(chat_id),
                "roster": await app.roster(),
                "model": [m.model_dump(mode="json") for c in app.model.calls for m in c],
                "thread_envs": thread_envs,
                "listed": (await app.app.get(f"/api/agents/{AGENT}/secrets")).json(),
            },
            default=str,
        )
        assert value not in seen
        assert (await app.roster())["secret_requests"] == {}
        envelope, _ = await app.rt.client.query(
            vault._id(AGENT), vault.SecretVault.encrypted, "STRIPE_WEBHOOK_SECRET"
        )
        assert value.encode() not in envelope

        # Only an explicit operator reveal returns it.
        revealed = await app.app.post(
            f"/api/agents/{AGENT}/secrets/STRIPE_WEBHOOK_SECRET/reveal",
            json={"request_id": "reveal-1"},
        )
        assert revealed.json() == {"name": "STRIPE_WEBHOOK_SECRET", "value": value}
        assert revealed.headers["cache-control"] == "no-store"


async def test_a_route_prompt_lands_as_a_turn_in_one_chat_per_conversation_and_retire_ends_serving(
    serve: conftest.Serve,
) -> None:
    webhook = (
        '"""Receive test events."""\n'
        "from hatchery.sdk import Response, prompt\n"
        "def POST(request):\n"
        "    event = request.json()\n"
        "    prompt(f\"event {event['id']} arrived\", key=f\"event-{event['id']}\")\n"
        "    return Response({'accepted': True}, status=202)\n"
    )
    script = [
        ai.user_message("event 17 arrived"),
        ai.assistant_message("Noted 17."),
        ai.user_message("event 18 arrived"),
        ai.assistant_message("Noted 18."),
    ]
    async with serve(script) as app:
        await app.merge_tree(AGENT, {"self/api/webhook/route.py": source(webhook)}, "webhook")
        async with app.public() as public:
            first = await public.post("/api/webhook", json={"id": 17})
            await app.rt.drain()
            retried = await public.post("/api/webhook", json={"id": 17})
            await app.rt.drain()
            second = await public.post("/api/webhook", json={"id": 18})
            await app.rt.drain()
        assert [first.status_code, retried.status_code, second.status_code] == [202, 202, 202]
        assert first.json() == {"accepted": True}

        [chat] = [c for c in await chats.list_all() if c.trigger == "api"]
        assert (chat.agent_id, chat.title) == (AGENT, "api: /api/webhook")
        transcript = [(m["role"], m["parts"][0]["text"]) for _, m in await events.read(chat.id, "messages")]
        assert transcript == [
            ("user", "event 17 arrived"),
            ("assistant", "Noted 17."),
            ("user", "event 18 arrived"),
            ("assistant", "Noted 18."),
        ]
        assert len((await app.roster())["threads"]) == 1
        assert not app.model.unused

        retired = await app.app.post(
            f"/api/agents/{AGENT}/retire", json={"request_id": "retire-routes"}
        )
        assert retired.status_code == 202
        async with app.public() as public:
            assert (await public.post("/api/webhook", json={"id": 19})).status_code == 410
        assert app.sandboxes.serve_sandboxes() == []
        await app.rt.drain()
        assert await vault.retired(AGENT) and await vault.inventory(AGENT) == []


async def test_limits_busy_sandbox_methods_headers_and_persistent_data(
    serve: conftest.Serve,
) -> None:
    counter = (
        "import pathlib\n"
        "from hatchery.sdk import Response\n"
        "def POST(request):\n"
        "    path = pathlib.Path('count.txt')\n"
        "    count = int(path.read_text()) + 1 if path.exists() else 1\n"
        "    path.write_text(str(count))\n"
        "    names = sorted({name.lower() for name, _ in request.headers})\n"
        "    return Response({'count': count, 'headers': names},\n"
        "                    headers=[('Set-Cookie', 'owner=1'), ('X-Kind', 'counter')])\n"
    )
    async with serve() as app:
        await app.merge_tree(AGENT, {"self/api/count/route.py": source(counter)}, "counter")
        async with app.public() as public:
            first = await public.post(
                "/api/count", headers={"Cookie": "session=secret", "X-Hatchery-Agent": "other"}
            )
            wrong = await public.get("/api/count")
            missing = await public.post("/api/nothing")
            large = await public.post("/api/count", content=b"x" * (1024 * 1024 + 1))
        assert first.status_code == 200
        assert first.json()["count"] == 1
        assert {"cookie", "host", "x-hatchery-agent"}.isdisjoint(first.json()["headers"])
        assert "set-cookie" not in first.headers and first.headers["x-kind"] == "counter"
        assert (wrong.status_code, wrong.headers["allow"]) == (405, "POST")
        assert missing.status_code == 404
        assert large.status_code == 413

        # One request at a time per agent: a held lock answers 429.
        [sandbox] = app.sandboxes.serve_sandboxes()
        sandbox.busy = True
        async with app.public() as public:
            busy = await public.post("/api/count")
        assert (busy.status_code, busy.headers["retry-after"]) == (429, "1")
        sandbox.busy = False

        # Relative files live in /workspace/data and survive a new deployment.
        await app.merge_tree(AGENT, {"self/README.md": source("changed\n")}, "unrelated")
        async with app.public() as public:
            assert (await public.post("/api/count")).json()["count"] == 2
        assert app.sandboxes.serve_sandboxes() == [sandbox]
