import contextlib
import copy
import json
import urllib.parse

import httpx
import pytest

from channels import destinations
from store import chats, events, spaces


@pytest.fixture
async def directory(monkeypatch):
    monkeypatch.delenv("SLACK_CONNECTOR", raising=False)
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
