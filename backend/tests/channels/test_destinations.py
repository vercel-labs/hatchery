import asyncio
import contextlib
import copy
import json
import urllib.parse

import httpx
import pytest

from channels import destinations
from store import chats, events, spaces, turns


@pytest.fixture
async def directory(monkeypatch):
    monkeypatch.delenv("SLACK_CONNECTOR", raising=False)
    # Each test has a fresh store and event loop; do not reuse contended locks.
    monkeypatch.setattr(turns, "_locks", {})
    people = [
        {"id": "andrey", "name": "Andrey Buzin", "username": "andrey",
         "email": "andrey@example.com", "slack": {"team_id": "T1", "user_id": "U1", "user": "andrey-slack"},
         "github": {"id": "42", "login": "anbuzin", "name": "Andrey"}, "secret": "never-return"},
        {"id": "jane", "name": "Jane Smith", "username": "jane",
         "email": "jane@example.com", "slack": {"team_id": "T1", "user_id": "U2", "user": "jane-s"},
         "github": {"id": "43", "login": "janesmith"}},
        {"id": "jane2", "name": "Jane Smith", "email": "jane2@example.com",
         "slack": {"team_id": "T2", "user_id": "U3", "user": "other-jane"}},
        {"id": "disallowed", "name": "Jane Smith", "email": "removed@example.com"},
    ]
    monkeypatch.setenv("HATCHERY_ALLOWED_EMAILS", "andrey@example.com,jane@example.com,jane2@example.com")

    async def get_user(identifier):
        return next((copy.deepcopy(person) for person in people if person["id"] == identifier), None)

    async def list_people():
        return copy.deepcopy(people)

    monkeypatch.setattr(destinations.auth_store, "get_user", get_user)
    monkeypatch.setattr(destinations.auth_store, "list_people", list_people)
    space = await spaces.create("Hatchery")
    space.repos = ["acme/hatchery"]
    await spaces.save(space)
    chat = await chats.create(space.id, "notify people", user_id="andrey")
    return chat, people


@pytest.fixture
def provider(monkeypatch):
    requests = []
    overrides = {}

    def respond(request):
        path = request.url.path.removeprefix("/api/").lstrip("/")
        form = dict(urllib.parse.parse_qsl(request.content.decode(), keep_blank_values=True)) if request.method == "POST" else {}
        requests.append((path, request, form))
        if path in overrides:
            override = overrides[path]
            return override(request, form) if callable(override) else httpx.Response(200, json=override)
        responses = {
            "auth.test": {"ok": True, "team_id": "T1", "user_id": "UBOT"},
            "conversations.info": {"ok": True, "channel": {"id": "C1", "name": "hatchery-updates", "is_member": True}},
            "users.info": {"ok": True, "user": {"id": form.get("user"), "deleted": False, "is_bot": False}},
            "conversations.members": {"ok": True, "members": ["U1", "U2", "UBOT"]},
            "chat.postMessage": {"ok": True, "ts": "100.123", "channel": "C1"},
            "chat.getPermalink": {"ok": True, "permalink": "https://acme.slack.com/archives/C1/p100000123"},
            "repos/acme/hatchery": {"id": 123, "full_name": "acme/hatchery"},
            "repos/acme/hatchery/issues/7": {"number": 7, "title": "Fix notifications", "html_url": "https://github.com/acme/hatchery/issues/7"},
            "users/masquerader": {"id": 99, "login": "masquerader"},
            "users/janesmith": {"id": 43, "login": "janesmith"},
            "repos/acme/hatchery/issues/7/comments": {"id": 99, "html_url": "https://github.com/acme/hatchery/issues/7#issuecomment-99"},
        }
        if path == "users.conversations":
            if not form.get("cursor"):
                return httpx.Response(200, json={"ok": True, "channels": [
                    {"id": "C1", "name": "hatchery-updates"},
                    {"id": "COLD", "name": "hatchery-old", "is_archived": True},
                ], "response_metadata": {"next_cursor": "page2"}})
            return httpx.Response(200, json={"ok": True, "channels": [
                {"id": "C2", "name": "hatchery-releases"},
                {"id": "CPRIVATE", "name": "hatchery-private", "is_private": True},
                {"id": "COTHER", "name": "random"},
            ]})
        if path not in responses:
            raise AssertionError(f"unexpected provider request: {request.method} {request.url}")
        return httpx.Response(200, json=responses[path])

    @contextlib.asynccontextmanager
    async def client(provider):
        base_url = "https://slack.com/api/" if provider == "slack" else "https://api.github.com/"
        async with httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(respond)) as http:
            yield http

    monkeypatch.setattr(destinations, "_client", client)
    return requests, overrides


async def test_channel_search_paginates_public_channels_and_returns_fuzzy_candidates(directory, provider):
    chat, _ = directory
    requests, _ = provider
    found = await destinations.find_channels(chat.id, "slack", "#hatchery")
    assert {item["id"] for item in found} == {"T1/C1", "T1/C2"}
    assert all(not item["private"] for item in found)
    pages = [form for path, _, form in requests if path == "users.conversations"]
    assert [page["cursor"] for page in pages] == ["", "page2"]
    assert all(page["types"] == "public_channel" for page in pages)
    assert not any(path == "conversations.members" for path, _, _ in requests)
    assert all("aliases" not in item for item in found)
    exact = await destinations.find_channels(chat.id, "slack", "#HATCHERY-UPDATES")
    assert exact[0]["id"] == "T1/C1"
    assert exact[0]["match"] == "exact"
    fuzzy = await destinations.find_channels(chat.id, "slack", "hatchrey-updates")
    assert fuzzy[0]["id"] == "T1/C1"
    assert fuzzy[0]["match"] == "fuzzy"
    assert await destinations.find_channels(chat.id, "slack", "zzzzzzzzzz") == []


async def test_private_channel_send_still_requires_owner_membership(directory, provider):
    chat, _ = directory
    requests, overrides = provider
    overrides["conversations.members"] = {"ok": True, "members": ["U2", "UBOT"]}
    overrides["conversations.info"] = {"ok": True, "channel": {"is_member": True, "is_private": True}}
    for people in [[], ["jane"]]:
        with pytest.raises(ValueError, match="owner cannot access"):
            await destinations.send_message(chat.id, "slack", "T1/CPRIVATE", "Done", people, delivery_key="turn1")
    assert not any(path == "chat.postMessage" for path, _, _ in requests)

    def members(request, form):
        if not form.get("cursor"):
            return httpx.Response(200, json={"ok": True, "members": ["UBOT"], "response_metadata": {"next_cursor": "next"}})
        return httpx.Response(200, json={"ok": True, "members": ["U1", "U2"]})

    overrides["conversations.members"] = members
    result = await destinations.send_message(chat.id, "slack", "T1/CPRIVATE", "Done", ["jane"], delivery_key="turn1")
    assert result["status"] == "sent"


async def test_people_search_links_provider_handles_and_preserves_ambiguity(directory):
    chat, _ = directory
    [person] = await destinations.find_people(chat.id, "@anbuzin")
    assert person["id"] == "andrey"
    assert person["slack"]["user_id"] == "U1"
    assert person["github"]["login"] == "anbuzin"
    assert "email" not in person and "secret" not in person
    assert "never-return" not in json.dumps(person)
    found = await destinations.find_people(chat.id, "Jane Smith")
    assert {person["id"] for person in found if person["match"] == "exact"} == {"jane", "jane2"}
    assert "disallowed" not in {person["id"] for person in found}
    assert (await destinations.find_people(chat.id, "me"))[0]["id"] == "andrey"
    assert (await destinations.find_people(chat.id, "andrey-slack"))[0]["id"] == "andrey"
    assert (await destinations.find_people(chat.id, "Andrye Buzin"))[0]["id"] == "andrey"
    assert await destinations.find_people(chat.id, "zzzzzzzzzz") == []


async def test_people_search_enriches_current_slack_names_but_only_for_linked_accounts(directory, provider, monkeypatch):
    chat, _ = directory
    requests, overrides = provider
    monkeypatch.setenv("SLACK_CONNECTOR", "slack/test")

    def profiles(request, form):
        if not form.get("cursor"):
            return httpx.Response(200, json={"ok": True, "members": [
                {"id": "UNLINKED", "name": "release-captain", "profile": {"display_name": "Release Captain"}},
                {"id": "U1", "name": "andrey", "profile": {}},
            ], "response_metadata": {"next_cursor": "next"}})
        return httpx.Response(200, json={"ok": True, "members": [
            {"id": "U2", "name": "jane-current", "profile": {"display_name": "Release Captain"}},
        ]})

    overrides["users.list"] = profiles
    [found] = await destinations.find_people(chat.id, "Release Captain")
    assert found["id"] == "jane"
    assert found["slack"]["user"] == "jane-current"
    assert found["match"] == "exact"
    assert sum(path == "users.list" for path, _, _ in requests) == 2
    requests.clear()
    assert (await destinations.find_people(chat.id, "anbuzin"))[0]["id"] == "andrey"
    assert not requests


@pytest.mark.parametrize("failure", ["missing_scope", "wrong_workspace"])
async def test_optional_slack_enrichment_does_not_break_local_people_search(directory, provider, monkeypatch, failure):
    chat, _ = directory
    _, overrides = provider
    monkeypatch.setenv("SLACK_CONNECTOR", "slack/test")
    if failure == "missing_scope":
        overrides["users.list"] = {"ok": False, "error": "missing_scope"}
    else:
        overrides["auth.test"] = {"ok": True, "team_id": "TOTHER"}
    found = await destinations.find_people(chat.id, "and")
    assert found[0]["id"] == "andrey"
    assert found[0]["match"] == "partial"


async def test_unauthorized_context_and_empty_queries_do_not_call_provider(directory, provider):
    requests, _ = provider
    chat, _ = directory
    for query in ["", " ", "#"]:
        with pytest.raises(ValueError, match="provide"):
            await destinations.find_channels(chat.id, "slack", query)
    anonymous = await chats.create(None, "anonymous")
    with pytest.raises(ValueError, match="owned"):
        await destinations.find_channels(anonymous.id, "slack", "hatchery")
    with pytest.raises(ValueError, match="owned"):
        await destinations.find_people(anonymous.id, "Jane")
    assert not requests


async def test_slack_workspace_must_match_the_chat_owner(directory, provider):
    chat, people = directory
    people[0]["slack"]["team_id"] = "T2"
    with pytest.raises(ValueError, match="workspace"):
        await destinations.find_channels(chat.id, "slack", "hatchery")
    assert [path for path, _, _ in provider[0]] == ["auth.test"]


async def test_slack_send_mentions_verified_people_and_records_receipt_without_binding(directory, provider):
    chat, _ = directory
    requests, _ = provider
    result = await destinations.send_message(
        chat.id, "slack", "T1/C1", "Done <!channel> <@UFAKE>", ["jane"], delivery_key="turn1",
    )
    assert result["status"] == "sent"
    assert result["message_id"] == "100.123"
    assert result["url"].endswith("p100000123")
    [sent] = [form for path, _, form in requests if path == "chat.postMessage"]
    assert sent["text"] == "<@U2> Done &lt;!channel&gt; &lt;@UFAKE&gt;"
    assert sent["parse"] == "none"
    assert await chats.bindings(chat.id) == []
    repeat = await destinations.send_message(
        chat.id, "slack", "T1/C1", "Done <!channel> <@UFAKE>", ["jane"], delivery_key="turn1",
    )
    assert repeat == result
    assert sum(path == "chat.postMessage" for path, _, _ in requests) == 1
    records = await events.read(chat.id, "notifications")
    assert records[-1][1]["result"] == result


@pytest.mark.parametrize("change,recipient,error", [
    ("wrong_destination", "jane", "workspace"),
    ("not_member", "jane", "bot must be a member"),
    ("archived", "jane", "active channel"),
    ("none", "jane2", "this workspace"),
    ("none", "disallowed", "allowed linked"),
    ("none", "unknown", "allowed linked"),
    ("inactive", "jane", "inactive"),
    ("missing_member", "jane", "not in this Slack channel"),
])
async def test_slack_send_rejects_invalid_destinations_and_recipients(directory, provider, change, recipient, error):
    chat, _ = directory
    requests, overrides = provider
    destination = "T2/C1" if change == "wrong_destination" else "T1/C1"
    if change in {"not_member", "archived"}:
        overrides["conversations.info"] = {"ok": True, "channel": {
            "is_member": change != "not_member", "is_archived": change == "archived",
        }}
    elif change == "inactive":
        overrides["users.info"] = {"ok": True, "user": {"deleted": True}}
    elif change == "missing_member":
        overrides["conversations.members"] = {"ok": True, "members": ["U1"]}
    with pytest.raises(ValueError, match=error):
        await destinations.send_message(chat.id, "slack", destination, "Done", [recipient], delivery_key="turn1")
    assert not any(path == "chat.postMessage" for path, _, _ in requests)


@pytest.mark.parametrize("failure", ["timeout", "server_error", "invalid_response", "internal_error", "fatal_error"])
async def test_uncertain_delivery_is_not_retried(directory, provider, failure):
    chat, _ = directory
    requests, overrides = provider

    def fail(request, form):
        if failure == "timeout":
            raise httpx.ReadTimeout("lost response", request=request)
        if failure == "server_error":
            return httpx.Response(503, json={"error": "unavailable"})
        if failure in {"internal_error", "fatal_error"}:
            return httpx.Response(200, json={"ok": False, "error": failure})
        return httpx.Response(200, content="broken response")

    overrides["chat.postMessage"] = fail
    for _ in range(2):
        result = await destinations.send_message(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")
        assert result["status"] == "unknown"
    assert sum(path == "chat.postMessage" for path, _, _ in requests) == 1


async def test_lost_receipt_does_not_duplicate_a_post(directory, provider, monkeypatch):
    chat, _ = directory
    requests, _ = provider
    append = events.append

    async def lose_receipt(chat_id, stream, data):
        if stream == "notifications" and "result" in data:
            raise RuntimeError("storage unavailable")
        return await append(chat_id, stream, data)

    monkeypatch.setattr(events, "append", lose_receipt)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await destinations.send_message(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")
    result = await destinations.send_message(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")
    assert result["status"] == "unknown"
    assert sum(path == "chat.postMessage" for path, _, _ in requests) == 1


async def test_missing_scope_and_permalink_failure_are_not_reported_as_success_and_failure_respectively(directory, provider):
    chat, _ = directory
    _, overrides = provider
    overrides["users.conversations"] = {"ok": False, "error": "missing_scope"}
    with pytest.raises(RuntimeError, match="missing_scope"):
        await destinations.find_channels(chat.id, "slack", "hatchery")
    overrides["chat.getPermalink"] = {"ok": False, "error": "rate_limited"}
    result = await destinations.send_message(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")
    assert result["status"] == "sent"
    assert result["url"] == "https://app.slack.com/client/T1/C1"
    overrides["chat.postMessage"] = {"ok": False, "error": "restricted_action"}
    result = await destinations.send_message(chat.id, "slack", "T1/C1", "Different", delivery_key="turn1")
    assert result["status"] == "failed"


async def test_scope_error_preserves_provider_details_without_logging_credentials(directory, provider, monkeypatch, caplog):
    chat, _ = directory
    _, overrides = provider
    monkeypatch.setenv("SLACK_CONNECTOR", "slack/hatchery")
    overrides["users.conversations"] = {
        "ok": False, "error": "missing_scope", "needed": "channels:read,groups:read",
        "provided": "chat:write,channels:read", "token": "do-not-log-this-token",
        "private_data": "do-not-log-this-either",
    }
    with pytest.raises(destinations.SlackScopeRequired) as caught:
        await destinations.find_channels(chat.id, "slack", "general")
    result = caught.value.result
    assert result["connector"] == "slack/hatchery"
    assert result["method"] == "users.conversations"
    assert result["needed"] == ["channels:read", "groups:read"]
    assert result["provided"] == ["channels:read", "chat:write"]
    assert json.loads(str(caught.value)) == {
        "error": "missing_scope", "needed": "channels:read,groups:read",
        "provided": "chat:write,channels:read",
    }
    assert str(caught.value) in caplog.text
    assert "do-not-log" not in caplog.text + json.dumps(result)


@pytest.mark.parametrize("body,headers,needed,provided,accepted", [
    ({"needed": "groups:read"}, {"x-oauth-scopes": "chat:write", "x-accepted-oauth-scopes": "channels:read,groups:read"},
     ["groups:read"], ["chat:write"], ["channels:read", "groups:read"]),
    ({"provided": None}, {"x-oauth-scopes": "chat:write"}, None, ["chat:write"], None),
    ({}, {"x-accepted-oauth-scopes": "channels:read,groups:read"}, None, None, ["channels:read", "groups:read"]),
    ({"provided": ""}, {"x-oauth-scopes": "chat:write"}, None, [], None),
    ({}, {}, None, None, None),
])
async def test_scope_error_header_fallback_does_not_invent_missing_details(directory, provider, body, headers, needed, provided, accepted):
    chat, _ = directory
    _, overrides = provider
    overrides["users.conversations"] = lambda request, form: httpx.Response(
        200, json={"ok": False, "error": "missing_scope", **body}, headers=headers,
    )
    with pytest.raises(destinations.SlackScopeRequired) as caught:
        await destinations.find_channels(chat.id, "slack", "general")
    result = caught.value.result
    assert result["needed"] == needed
    assert result["provided"] == provided
    assert result["accepted_scopes"] == accepted
    assert json.loads(result["detail"]) == {
        "error": "missing_scope", **{key: value for key, value in body.items() if isinstance(value, str)},
    }


async def test_send_scope_failure_keeps_details_in_durable_receipt(directory, provider):
    chat, _ = directory
    requests, overrides = provider
    overrides["chat.postMessage"] = {
        "ok": False, "error": "missing_scope", "needed": "chat:write", "provided": "channels:read",
    }
    result = await destinations.send_message(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")
    assert result["status"] == "failed"
    assert result["error"] == "missing_scope"
    assert result["needed"] == ["chat:write"]
    assert result["provided"] == ["channels:read"]
    assert (await events.read(chat.id, "notifications"))[-1][1]["result"] == result
    assert await destinations.send_message(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1") == result
    assert sum(path == "chat.postMessage" for path, _, _ in requests) == 1


async def test_github_exact_and_title_search_are_space_scoped(directory, provider):
    chat, _ = directory
    requests, overrides = provider
    found = await destinations.find_channels(chat.id, "github", "https://github.com/acme/hatchery/issues/7")
    assert found[0]["id"] == "acme/hatchery#7"
    assert found[0]["match"] == "exact"
    overrides["search/issues"] = {"items": [
        {"title": "Notifications", "number": 8, "html_url": "https://github.com/acme/hatchery/pull/8", "pull_request": {}},
        {"title": "Notifications", "number": 9, "html_url": "https://github.com/other/private/issues/9"},
    ]}
    found = await destinations.find_channels(chat.id, "github", "Notifications repo:other/private")
    assert [item["id"] for item in found] == ["acme/hatchery#8"]
    assert found[0]["kind"] == "pull"
    assert found[0]["match"] == "search"
    query = next(request.url.params["q"] for path, request, _ in requests if path == "search/issues")
    assert query.startswith('repo:acme/hatchery "Notifications"')
    assert "repo:other/private" not in query
    with pytest.raises(ValueError, match="space's repositories"):
        await destinations.find_channels(chat.id, "github", "other/private#7")


async def test_github_send_validates_linked_identity_and_space(directory, provider):
    chat, people = directory
    requests, _ = provider
    result = await destinations.send_message(
        chat.id, "github", "acme/hatchery#7", "Done @outsider", ["jane"], delivery_key="turn1",
    )
    assert result["status"] == "sent"
    assert result["message_id"] == "99"
    posted = [request for path, request, _ in requests if path.endswith("/comments")]
    assert json.loads(posted[0].content)["body"] == "@janesmith Done @\u200boutsider\n\n<!-- chat:github -->"
    assert await chats.bindings(chat.id) == []
    with pytest.raises(ValueError, match="space's repositories"):
        await destinations.send_message(chat.id, "github", "other/private#7", "Done", delivery_key="turn2")
    people[1]["github"]["login"] = "masquerader"
    with pytest.raises(ValueError, match="identity changed"):
        await destinations.send_message(chat.id, "github", "acme/hatchery#7", "Done", ["jane"], delivery_key="turn2")
    assert sum(path.endswith("/comments") for path, _, _ in requests) == 1


async def test_shared_slack_thread_has_independent_record_and_owner_binding(directory, provider):
    chat, _ = directory
    requests, _ = provider
    result = await destinations.start_shared_thread(
        chat.id, "slack", "T1/C1", "Done <!channel>", ["jane"], delivery_key="turn1",
        tool_call_id="call1", excluded_message_ids=["private1"],
    )
    assert result["status"] == "sent"
    sharing = result["sharing"]
    assert sharing == {"id": sharing["id"], "provider": "slack", "label": "#hatchery-updates",
                       "url": result["url"], "text": "Done <!channel>", "tool_call_id": "call1",
                       "message_id": "100.123"}
    binding = await chats.binding("slack:T1:C1:100.123")
    assert binding.chat_id == chat.id
    assert binding.state == {
        "team_id": "T1", "channel_id": "C1", "thread_ts": "100.123", "user_id": "U1",
        "sharing_id": sharing["id"], "excluded_message_ids": ["private1"], "start_message_id": "100.123",
    }
    assert await events.read(chat.id, "sharing") == [(0, {**sharing, "token": binding.token, "state": binding.state})]
    assert await events.read(chat.id, "messages") == []
    assert await events.read(chat.id, "ui") == [(0, {"type": "messages.changed", "sharing_id": sharing["id"]})]
    receipt = (await events.read(chat.id, "notifications"))[-1][1]
    assert receipt["text"] == "<@U2> Done &lt;!channel&gt;"
    assert receipt["result"] == result
    requests.clear()
    assert await destinations.start_shared_thread(
        chat.id, "slack", "T1/C1", "Done <!channel>", ["jane"], delivery_key="turn1",
    ) == result
    assert not requests
    assert len(await events.read(chat.id, "sharing")) == 1
    assert len(await events.read(chat.id, "ui")) == 1


@pytest.mark.parametrize("kind", ["issue", "pull"])
async def test_shared_github_uses_canonical_token_and_reuses_one_thread(directory, provider, kind):
    chat, _ = directory
    requests, overrides = provider
    if kind == "pull":
        overrides["repos/acme/hatchery/issues/7"] = {"number": 7, "pull_request": {"url": "pull"}}
    result = await destinations.start_shared_thread(
        chat.id, "github", "acme/hatchery#7", "Done", delivery_key="turn1", excluded_message_ids=["private"],
    )
    binding = await chats.binding(f"github:repo:123:{kind}:7")
    assert binding.chat_id == chat.id
    assert binding.state == {
        "owner": "acme", "repo": "hatchery", "repository_id": 123, "kind": kind,
        "number": 7, "root_comment_id": None, "comment_id": 99, "sender_id": "42",
        "sharing_id": result["sharing"]["id"], "excluded_message_ids": ["private"], "start_message_id": "99",
    }
    assert result["sharing"]["label"] == "acme/hatchery#7"
    assert await destinations.start_shared_thread(
        chat.id, "github", "https://github.com/acme/hatchery/issues/7", "Share again", delivery_key="turn2",
    ) == result
    assert sum(path.endswith("/comments") for path, _, _ in requests) == 1
    assert len(await events.read(chat.id, "sharing")) == 1


async def test_existing_inbound_github_thread_keeps_its_original_boundary(directory, provider):
    chat, _ = directory
    requests, _ = provider
    token = "github:repo:123:issue:7"
    original = await chats.bind(token, chat.id, "github", {
        "owner": "acme", "repo": "hatchery", "repository_id": 123, "kind": "issue",
        "number": 7, "root_comment_id": None, "comment_id": 10, "sender_id": "42",
    })
    result = await destinations.start_shared_thread(
        chat.id, "github", "acme/hatchery#7", "Share here", delivery_key="turn1",
        excluded_message_ids=["private"],
    )
    assert result["status"] == "already_shared"
    assert "sharing" not in result
    assert await chats.binding(token) == original
    assert not any(path.endswith("/comments") for path, _, _ in requests)
    assert await events.read(chat.id, "notifications") == []
    assert await events.read(chat.id, "sharing") == []


async def test_shared_github_collision_rejected_before_post(directory, provider):
    chat, _ = directory
    requests, _ = provider
    other = await chats.create(chat.space_id, "other", user_id="jane")
    original = await chats.bind("github:repo:123:issue:7", other.id, "github", {"sender_id": "43"})
    with pytest.raises(ValueError, match="already bound"):
        await destinations.start_shared_thread(chat.id, "github", "acme/hatchery#7", "Done", delivery_key="turn1")
    assert not any(path.endswith("/comments") for path, _, _ in requests)
    assert await chats.binding(original.token) == original
    assert await events.read(chat.id, "notifications") == []
    assert await events.read(chat.id, "sharing") == []


async def test_concurrent_github_shares_do_not_post_into_another_chat(directory, provider):
    chat, _ = directory
    requests, _ = provider
    other = await chats.create(chat.space_id, "other", user_id="jane")
    results = await asyncio.gather(
        *(destinations.start_shared_thread(owner.id, "github", "acme/hatchery#7", "Done", delivery_key="turn1")
          for owner in (chat, other)), return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert sum(isinstance(result, dict) and result["status"] == "sent" for result in results) == 1
    assert sum(path.endswith("/comments") for path, _, _ in requests) == 1


@pytest.mark.parametrize("failure", ["binding", "sharing", "sharing_after_write", "ui"])
@pytest.mark.parametrize("provider_name,destination", [("slack", "T1/C1"), ("github", "acme/hatchery#7")])
async def test_shared_receipt_recovers_finalize_without_provider_calls(directory, provider, monkeypatch, failure, provider_name, destination):
    chat, _ = directory
    requests, _ = provider
    append = events.append

    async def fail_append(chat_id, ns, data):
        if ns == failure or ns == "sharing" and failure == "sharing_after_write":
            if failure == "sharing_after_write":
                await append(chat_id, ns, data)
            raise RuntimeError("storage unavailable")
        return await append(chat_id, ns, data)

    async def fail_bind(*args, **kwargs):
        raise RuntimeError("binding unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(events, "append", fail_append)
        if failure == "binding":
            patch.setattr(chats, "bind", fail_bind)
        with pytest.raises(RuntimeError, match="unavailable"):
            await destinations.start_shared_thread(
                chat.id, provider_name, destination, "Done", delivery_key="turn1", tool_call_id="call1",
                excluded_message_ids=["private"],
            )
    receipt = (await events.read(chat.id, "notifications"))[-1][1]
    assert receipt["result"]["status"] == "sent"
    assert receipt["text"] == ("Done" if provider_name == "slack" else "Done\n\n<!-- chat:github -->")
    if failure == "binding":
        assert await chats.bindings(chat.id) == []
    requests.clear()
    result = await destinations.start_shared_thread(chat.id, provider_name, destination, "Done", delivery_key="turn1")
    assert result == receipt["result"]
    assert not requests
    assert await events.read(chat.id, "sharing") == [(0, receipt["sharing"])]
    binding = await chats.binding(receipt["sharing"]["token"])
    assert binding.state == receipt["sharing"]["state"]
    assert len(await events.read(chat.id, "ui")) == 1
    # A subsequent replay must not overwrite mutable inbound state either.
    inbound = {"comment_id": 200, **({"user_id": "U2"} if provider_name == "slack" else {"sender_id": "43"})}
    await chats.claim(binding.token, provider_name, None, "reply", inbound,
                      user_id="jane", allow_participants=True)
    assert await destinations.start_shared_thread(chat.id, provider_name, destination, "Done", delivery_key="turn1") == result
    assert (await chats.binding(binding.token)).state["comment_id"] == 200
    if provider_name == "slack":
        assert (await chats.binding(binding.token)).state["user_id"] == "U1"
    else:
        assert (await chats.binding(binding.token)).state["sender_id"] == "43"


@pytest.mark.parametrize("provider_name,destination,path", [
    ("slack", "T1/C1", "chat.postMessage"),
    ("github", "acme/hatchery#7", "repos/acme/hatchery/issues/7/comments"),
])
async def test_uncertain_shared_delivery_has_no_binding_or_sharing(directory, provider, provider_name, destination, path):
    chat, _ = directory
    requests, overrides = provider

    def fail(request, form):
        raise httpx.ReadTimeout("lost response", request=request)

    overrides[path] = fail
    for _ in range(2):
        result = await destinations.start_shared_thread(chat.id, provider_name, destination, "Done", delivery_key="turn1")
        assert result["status"] == "unknown"
        assert "sharing" not in result
    assert sum(request_path == path for request_path, _, _ in requests) == 1
    assert await chats.bindings(chat.id) == []
    assert await events.read(chat.id, "sharing") == []
    assert await events.read(chat.id, "ui") == []


async def test_shared_lost_receipt_never_binds_or_reposts(directory, provider, monkeypatch):
    chat, _ = directory
    requests, _ = provider
    append = events.append

    async def fail_receipt(chat_id, ns, data):
        if ns == "notifications" and "result" in data:
            raise RuntimeError("lost receipt")
        return await append(chat_id, ns, data)

    with monkeypatch.context() as patch:
        patch.setattr(events, "append", fail_receipt)
        with pytest.raises(RuntimeError, match="lost receipt"):
            await destinations.start_shared_thread(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")
    result = await destinations.start_shared_thread(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")
    assert result["status"] == "unknown"
    assert sum(path == "chat.postMessage" for path, _, _ in requests) == 1
    assert await chats.bindings(chat.id) == []
    assert await events.read(chat.id, "sharing") == []


async def test_one_off_and_shared_deliveries_have_separate_receipts(directory, provider):
    chat, _ = directory
    requests, _ = provider
    one_off = await destinations.send_message(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")
    shared = await destinations.start_shared_thread(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")
    assert "sharing" not in one_off and "sharing" in shared
    assert sum(path == "chat.postMessage" for path, _, _ in requests) == 2
    assert await destinations.send_message(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1") == one_off


async def test_slack_returned_thread_collision_keeps_sent_receipt_without_transfer(directory, provider):
    chat, _ = directory
    requests, _ = provider
    other = await chats.create(None, "other", user_id="jane")
    original = await chats.bind("slack:T1:C1:100.123", other.id, "slack", {"user_id": "U2"})
    for _ in range(2):
        with pytest.raises(ValueError, match="already bound"):
            await destinations.start_shared_thread(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")
    assert sum(path == "chat.postMessage" for path, _, _ in requests) == 1
    assert (await events.read(chat.id, "notifications"))[-1][1]["result"]["status"] == "sent"
    assert await chats.binding(original.token) == original
    assert await events.read(chat.id, "sharing") == []


async def test_shared_finalize_waits_for_chat_lock_after_saving_sent_receipt(directory, provider, monkeypatch):
    chat, _ = directory
    saved = asyncio.Event()
    locked = asyncio.Event()
    release = asyncio.Event()
    append = events.append

    async def observe_receipt(chat_id, ns, data):
        index = await append(chat_id, ns, data)
        if ns == "notifications" and "result" in data:
            saved.set()
            await locked.wait()
        return index

    async def hold_lock():
        await saved.wait()  # Let the pre-post snapshot finish first.
        async with turns.run(chat.id):
            locked.set()
            await release.wait()

    monkeypatch.setattr(events, "append", observe_receipt)
    # Separate tasks avoid inheriting turns.run's context-local reentrancy.
    holder = asyncio.create_task(hold_lock())
    sender = asyncio.create_task(destinations.start_shared_thread(
        chat.id, "slack", "T1/C1", "Done", delivery_key="turn1",
    ))
    try:
        await asyncio.wait_for(saved.wait(), 2)
        assert not sender.done()
        assert await chats.bindings(chat.id) == []
        assert await events.read(chat.id, "sharing") == []
    finally:
        release.set()
        await holder
        result = await sender
    assert result["status"] == "sent"
    assert len(await chats.bindings(chat.id)) == 1
    assert len(await events.read(chat.id, "sharing")) == 1


async def test_slack_webhook_waits_for_post_and_binding_under_channel_lock(directory, provider, monkeypatch):
    chat, _ = directory
    posted, webhook_waiting, finish_post = asyncio.Event(), asyncio.Event(), asyncio.Event()
    api = destinations._api

    async def pause_post(client, method, path, **kwargs):
        result = await api(client, method, path, **kwargs)
        if path == "chat.postMessage":
            posted.set()
            await finish_post.wait()
        return result

    async def webhook():
        await posted.wait()
        webhook_waiting.set()
        async with turns.run("sharing:slack:T1:C1"), turns.run("sharing:slack:T1:C1:100.123"):
            owner, created = await chats.claim(
                "slack:T1:C1:100.123", "slack", None, "reply",
                {"team_id": "T1", "channel_id": "C1", "thread_ts": "100.123", "user_id": "U2"},
                user_id="jane", allow_participants=True,
            )
        async with turns.run(owner.id):
            await events.append(owner.id, "messages", {"id": "future_reply"})
        return owner, created

    monkeypatch.setattr(destinations, "_api", pause_post)
    # Both tasks start outside any lock context; no inherited reentrancy.
    inbound = asyncio.create_task(webhook())
    outbound = asyncio.create_task(destinations.start_shared_thread(
        chat.id, "slack", "T1/C1", "Done", delivery_key="turn1",
    ))
    try:
        await asyncio.wait_for(webhook_waiting.wait(), 2)
        assert not inbound.done()
        assert await chats.binding("slack:T1:C1:100.123") is None
    finally:
        finish_post.set()
        result, (owner, created) = await asyncio.gather(outbound, inbound)
    assert result["status"] == "sent"
    assert not created and owner.id == chat.id and owner.user_id == "andrey"
    assert len(await chats.list_all()) == 1
    bound = await chats.binding("slack:T1:C1:100.123")
    assert bound.state["user_id"] == "U1"
    assert bound.state["start_message_id"] == "100.123"
    assert "future_reply" not in bound.state["excluded_message_ids"]


@pytest.mark.parametrize("crash", ["receipt", "permalink", "enriched_receipt"])
async def test_slack_early_sent_receipt_recovers_frozen_boundary(directory, provider, monkeypatch, crash):
    chat, _ = directory
    requests, _ = provider
    await events.append(chat.id, "messages", {"id": "context"})
    await events.append(chat.id, "messages", {"id": "arrived_during_model"})
    await events.append(chat.id, "sharing", {"id": "previous"})
    # A previous root whose independent record has not yet been finalized also
    # predates this destination and must not appear on a delayed fanout replay.
    await events.append(chat.id, "notifications", {"result": {"status": "sent"}, "sharing": {"id": "pending"}})
    append, api = events.append, destinations._api
    snapshots = []

    async def crash_append(chat_id, ns, data):
        if ns == "notifications" and data.get("result", {}).get("status") == "sent":
            snapshots.append(copy.deepcopy(data))
            if crash == "enriched_receipt" and len(snapshots) == 2:
                raise RuntimeError("receipt unavailable")
        index = await append(chat_id, ns, data)
        if crash == "receipt" and ns == "notifications" and "result" in data:
            raise RuntimeError("crashed after receipt")
        return index

    async def crash_permalink(client, method, path, **kwargs):
        if path == "chat.getPermalink":
            receipt = (await events.read(chat.id, "notifications"))[-1][1]
            assert receipt["result"]["status"] == "sent"
            assert receipt["sharing"]["state"]["start_message_id"] == "100.123"
            assert await chats.bindings(chat.id) == []
            if crash == "permalink":
                raise asyncio.CancelledError()
        return await api(client, method, path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(events, "append", crash_append)
        patch.setattr(destinations, "_api", crash_permalink)
        with pytest.raises(asyncio.CancelledError if crash == "permalink" else RuntimeError):
            await destinations.start_shared_thread(
                chat.id, "slack", "T1/C1", "Done", ["jane"], delivery_key="turn1",
                excluded_message_ids=["model_only", "context", "model_only"],
            )
    expected = ["model_only", "context", "arrived_during_model", "sharing:previous", "sharing:pending"]
    receipt = (await events.read(chat.id, "notifications"))[-1][1]
    assert receipt["sharing"]["state"]["excluded_message_ids"] == expected
    assert receipt["text"] == "<@U2> Done"
    assert receipt["result"]["url"] == "https://app.slack.com/client/T1/C1"
    assert await chats.bindings(chat.id) == []
    assert sum(path == "chat.postMessage" for path, _, _ in requests) == 1
    await events.append(chat.id, "messages", {"id": "future_reply"})
    await events.append(chat.id, "sharing", {"id": "future_share"})
    requests.clear()
    result = await destinations.start_shared_thread(
        chat.id, "slack", "T1/C1", "Done", ["jane"], delivery_key="turn1", excluded_message_ids=["future_reply"],
    )
    assert result == receipt["result"]
    assert not requests
    assert (await chats.binding("slack:T1:C1:100.123")).state["excluded_message_ids"] == expected
    records = await events.read(chat.id, "sharing")
    assert records[-1][1] == receipt["sharing"]


async def test_github_exclusion_snapshot_precedes_post_not_finalize(directory, provider, monkeypatch):
    chat, _ = directory
    await events.append(chat.id, "messages", {"id": "persisted_before_post"})
    await events.append(chat.id, "sharing", {"id": "previous"})
    api = destinations._api

    async def inbound_during_post(client, method, path, **kwargs):
        if path.endswith("/comments") and method == "POST":
            # Different inbound destinations may continue while this API awaits.
            async with turns.run(chat.id):
                await events.append(chat.id, "messages", {"id": "future_reply"})
        return await api(client, method, path, **kwargs)

    monkeypatch.setattr(destinations, "_api", inbound_during_post)
    result = await destinations.start_shared_thread(
        chat.id, "github", "acme/hatchery#7", "Done", delivery_key="turn1",
        excluded_message_ids=["context"],
    )
    binding = await chats.binding("github:repo:123:issue:7")
    assert binding.state["excluded_message_ids"] == ["context", "persisted_before_post", "sharing:previous"]
    assert binding.state["start_message_id"] == result["message_id"] == "99"


@pytest.mark.parametrize("pause_path", ["chat.postMessage", "chat.getPermalink"])
async def test_concurrent_slack_replay_uses_enriched_receipt(directory, provider, monkeypatch, pause_path):
    chat, _ = directory
    requests, _ = provider
    lookup, finish_lookup, replay_read = asyncio.Event(), asyncio.Event(), asyncio.Event()
    api, read = destinations._api, events.read

    async def pause_permalink(client, method, path, **kwargs):
        if path == pause_path:
            lookup.set()
            await finish_lookup.wait()
        return await api(client, method, path, **kwargs)

    async def observe_read(chat_id, ns, *args):
        records = await read(chat_id, ns, *args)
        if ns == "notifications" and lookup.is_set():
            replay_read.set()
        return records

    async def replay():
        await lookup.wait()
        return await destinations.start_shared_thread(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")

    monkeypatch.setattr(destinations, "_api", pause_permalink)
    monkeypatch.setattr(events, "read", observe_read)
    repeated = asyncio.create_task(replay())
    original = asyncio.create_task(destinations.start_shared_thread(
        chat.id, "slack", "T1/C1", "Done", delivery_key="turn1",
    ))
    try:
        await asyncio.wait_for(replay_read.wait(), 2)
        assert not repeated.done()
    finally:
        finish_lookup.set()
        first, second = await asyncio.gather(original, repeated)
    assert first == second
    assert first["url"].endswith("p100000123")
    assert sum(path == "chat.postMessage" for path, _, _ in requests) == 1
    assert len(await events.read(chat.id, "sharing")) == 1


@pytest.mark.parametrize("provider_name,destination,token,lock_key", [
    ("slack", "T1/C1", "slack:T1:C1:100.123", "sharing:slack:T1:C1"),
    ("github", "acme/hatchery#7", "github:repo:123:issue:7", "sharing:github:repo:123:issue:7"),
])
async def test_inbound_recovers_sent_thread_and_frozen_pending_boundary(
    directory, provider, monkeypatch, provider_name, destination, token, lock_key,
):
    chat, _ = directory
    requests, _ = provider
    await chats.create(None, "unrelated newer chat", user_id="jane")
    await events.append(chat.id, "messages", {"id": "persisted"})
    await events.append(chat.id, "pending_messages", {"id": "context"})
    await events.append(chat.id, "pending_messages", {"id": "queued_before_share"})
    await events.append(chat.id, "sharing", {"id": "previous"})
    append = events.append

    async def crash_after_receipt(chat_id, namespace, data):
        index = await append(chat_id, namespace, data)
        if namespace == "notifications" and data.get("result", {}).get("status") == "sent":
            raise RuntimeError("crash before binding")
        return index

    with monkeypatch.context() as patch:
        patch.setattr(events, "append", crash_after_receipt)
        with pytest.raises(RuntimeError, match="crash before binding"):
            await destinations.start_shared_thread(
                chat.id, provider_name, destination, "Done", delivery_key="turn1",
                excluded_message_ids=["context", "model_only", "context"],
            )
    receipt = (await events.read(chat.id, "notifications"))[-1][1]
    expected = ["context", "model_only", "persisted", "queued_before_share", "sharing:previous"]
    assert receipt["sharing"]["state"]["excluded_message_ids"] == expected
    assert await chats.binding(token) is None
    assert sum(path == "chat.postMessage" or path.endswith("/comments") for path, _, _ in requests) == 1
    requests.clear()
    # New messages must not be folded into the old boundary during recovery.
    await events.append(chat.id, "messages", {"id": "future_message"})
    await events.append(chat.id, "pending_messages", {"id": "future_pending"})
    await events.append(chat.id, "sharing", {"id": "future_sharing"})
    assert await destinations.recover_shared_thread(token + ":wrong") is None
    assert await chats.binding(token) is None
    async with turns.run(lock_key):
        recovered = await destinations.recover_shared_thread(token)
        assert recovered.chat_id == chat.id
        claimed, created = await chats.claim(
            token, provider_name, None, "participant reply", {"comment_id": 200},
            user_id="jane", allow_participants=True,
        )
    assert not created and claimed.id == chat.id and claimed.user_id == "andrey"
    assert len(await chats.list_all()) == 2  # No separate inbound chat.
    assert recovered.state == receipt["sharing"]["state"]
    assert recovered.state["excluded_message_ids"] == expected
    assert f"sharing:{receipt['sharing']['id']}" not in recovered.state["excluded_message_ids"]
    assert (await events.read(chat.id, "sharing"))[-1][1] == receipt["sharing"]
    assert len(await events.read(chat.id, "ui")) == 1
    assert await destinations.recover_shared_thread(token) == await chats.binding(token)
    assert await destinations.start_shared_thread(
        chat.id, provider_name, destination, "Done", delivery_key="turn1",
    ) == receipt["result"]
    assert (await chats.binding(token)).state["excluded_message_ids"] == expected
    assert len(await events.read(chat.id, "sharing")) == 3
    assert len(await events.read(chat.id, "ui")) == 1
    assert not requests


@pytest.mark.parametrize("status", [None, "unknown", "failed", "one_off"])
async def test_recovery_requires_exact_known_sent_sharing_receipt(directory, provider, status):
    chat, _ = directory
    requests, _ = provider
    token = "slack:T1:C1:100.123"
    receipt = {"sharing": {"token": token}}
    if status == "one_off":
        receipt = {"result": {"status": "sent", "destination": token}}
    elif status is not None:
        receipt["result"] = {"status": status}
    await events.append(chat.id, "notifications", receipt)
    # Neither a separate UI record nor a user message confers ownership.
    await events.append(chat.id, "sharing", {"token": token, "id": "unconfirmed"})
    await events.append(chat.id, "messages", {"id": "inbound", "token": token, "status": "sent"})
    assert await destinations.recover_shared_thread(token) is None
    assert await destinations.recover_shared_thread("github:repo:123:issue:7") is None
    assert await chats.bindings(chat.id) == []
    assert not requests


async def test_recovery_preserves_existing_binding_without_reading_receipts(directory, provider, monkeypatch):
    chat, _ = directory
    requests, _ = provider
    bound = await chats.bind("slack:T1:C1:100.123", chat.id, "slack", {"user_id": "U1"})

    async def unavailable(*args):
        raise RuntimeError("receipts unavailable")

    monkeypatch.setattr(events, "read", unavailable)
    assert await destinations.recover_shared_thread(bound.token) == bound
    assert not requests


@pytest.mark.parametrize("conflict", ["binding_during_scan", "sent_receipt"])
async def test_recovery_never_transfers_a_conflicting_thread(directory, provider, monkeypatch, conflict):
    chat, _ = directory
    requests, _ = provider
    token = "slack:T1:C1:100.123"
    append = events.append

    async def crash_after_receipt(chat_id, namespace, data):
        index = await append(chat_id, namespace, data)
        if namespace == "notifications" and "result" in data:
            raise RuntimeError("crash before binding")
        return index

    with monkeypatch.context() as patch:
        patch.setattr(events, "append", crash_after_receipt)
        with pytest.raises(RuntimeError, match="crash before binding"):
            await destinations.start_shared_thread(chat.id, "slack", "T1/C1", "Done", delivery_key="turn1")
    receipt = (await events.read(chat.id, "notifications"))[-1][1]
    other = await chats.create(None, "other owner", user_id="jane")
    if conflict == "sent_receipt":
        await events.append(other.id, "notifications", receipt)
    else:
        read = events.read

        async def competing_binding(chat_id, namespace, *args):
            records = await read(chat_id, namespace, *args)
            if chat_id == chat.id and namespace == "notifications":
                await chats.bind(token, other.id, "slack", {"user_id": "U2"})
            return records

        monkeypatch.setattr(events, "read", competing_binding)
    requests.clear()
    with pytest.raises(ValueError, match="conflicting sent receipts|already bound"):
        await destinations.recover_shared_thread(token)
    bound = await chats.binding(token)
    if conflict == "binding_during_scan":
        assert bound.chat_id == other.id and bound.state == {"user_id": "U2"}
        assert await destinations.recover_shared_thread(token) == bound
    else:
        assert bound is None
    assert await events.read(chat.id, "sharing") == []
    assert not requests


async def test_github_send_recovers_another_chats_sent_thread_before_posting(directory, provider, monkeypatch):
    chat, _ = directory
    requests, _ = provider
    append = events.append

    async def crash_after_receipt(chat_id, namespace, data):
        index = await append(chat_id, namespace, data)
        if namespace == "notifications" and "result" in data:
            raise RuntimeError("crash before binding")
        return index

    with monkeypatch.context() as patch:
        patch.setattr(events, "append", crash_after_receipt)
        with pytest.raises(RuntimeError, match="crash before binding"):
            await destinations.start_shared_thread(chat.id, "github", "acme/hatchery#7", "Done", delivery_key="turn1")
    other = await chats.create(chat.space_id, "other", user_id="jane")
    with pytest.raises(ValueError, match="already bound"):
        await destinations.start_shared_thread(other.id, "github", "acme/hatchery#7", "Different", delivery_key="turn2")
    assert (await chats.binding("github:repo:123:issue:7")).chat_id == chat.id
    assert sum(path.endswith("/comments") for path, _, _ in requests) == 1
