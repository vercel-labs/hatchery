import contextlib
import copy
import json
import urllib.parse

import httpx
import pytest

from channels import destinations
from store import chats, spaces


@pytest.fixture
async def directory(monkeypatch):
    people = [
        {
            "id": "andrey",
            "name": "Andrey Buzin",
            "username": "andrey",
            "email": "andrey@example.com",
            "slack": {"team_id": "T1", "user_id": "U1", "user": "at-andrey"},
            "github": {"id": "42", "login": "anbuzin", "name": "Andrey"},
        },
        {
            "id": "john",
            "name": "John Business",
            "username": "john",
            "email": "john@example.com",
            "slack": {"team_id": "T1", "user_id": "U2", "user": "john-business"},
            "github": {"id": "43", "login": "johnbusiness", "name": "John"},
        },
    ]
    monkeypatch.setenv(
        "HATCHERY_ALLOWED_EMAILS", "andrey@example.com,john@example.com"
    )

    async def get_user(user_id):
        return next(
            (copy.deepcopy(person) for person in people if person["id"] == user_id),
            None,
        )

    async def list_people():
        return copy.deepcopy(people)

    monkeypatch.setattr(destinations.auth_store, "get_user", get_user)
    monkeypatch.setattr(destinations.auth_store, "list_people", list_people)
    space = await spaces.create("Hatchery")
    space.repos = ["acme/hatchery"]
    await spaces.save(space)
    return await chats.create(space.id, "notify", user_id="andrey")


def slack_client(requests):
    def respond(request: httpx.Request) -> httpx.Response:
        form = dict(
            urllib.parse.parse_qsl(request.content.decode(), keep_blank_values=True)
        )
        path = request.url.path.removeprefix("/api")
        requests.append((path, form))
        responses = {
            "/auth.test": {"ok": True, "team_id": "T1", "user_id": "UBOT"},
            "/users.conversations": {
                "ok": True,
                "channels": [
                    {
                        "id": "C1",
                        "name": "hatchery-updates",
                        "is_member": True,
                        "is_archived": False,
                        "is_private": False,
                    }
                ],
            },
            "/conversations.info": {
                "ok": True,
                "channel": {
                    "id": "C1",
                    "name": "hatchery-updates",
                    "is_member": True,
                },
            },
            "/users.info": {
                "ok": True,
                "user": {"id": form.get("user"), "deleted": False, "is_bot": False},
            },
            "/conversations.members": {"ok": True, "members": ["U1", "U2"]},
            "/chat.postMessage": {"ok": True, "ts": "100.123", "channel": "C1"},
        }
        return httpx.Response(200, json=responses[path])

    return httpx.AsyncClient(
        base_url="https://slack.com/api/", transport=httpx.MockTransport(respond)
    )


async def test_finds_linked_people_without_exposing_credentials(directory):
    found = await destinations.find_people(directory.id, "john business")
    assert found == [
        {
            "id": "john",
            "name": "John Business",
            "username": "john",
            "slack": {"team_id": "T1", "user_id": "U2", "user": "john-business"},
            "github": {"id": "43", "login": "johnbusiness", "name": "John"},
            "match": "exact",
        }
    ]


async def test_start_slack_thread_mentions_person_and_binds(directory, monkeypatch):
    requests = []

    @contextlib.asynccontextmanager
    async def client(_provider):
        async with slack_client(requests) as value:
            yield value

    monkeypatch.setattr(destinations, "_client", client)
    result = await destinations.start_thread(
        directory.id,
        "slack",
        "T1/C1",
        "Build finished",
        ["john"],
        delivery_key="turn_1",
    )

    assert result["status"] == "sent"
    assert result["message_id"] == "100.123"
    binding = await chats.binding("slack:T1:C1:100.123")
    assert binding is not None
    assert binding.chat_id == directory.id
    assert binding.state["thread_ts"] == "100.123"
    posted = next(form for path, form in requests if path == "/chat.postMessage")
    assert posted["text"] == "<@U2> Build finished"


async def test_start_github_thread_mentions_person_and_binds_issue(directory, monkeypatch):
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        responses = {
            "/repos/acme/hatchery/issues/7": {
                "number": 7,
                "title": "Notifications",
                "html_url": "https://github.com/acme/hatchery/issues/7",
            },
            "/repos/acme/hatchery": {"id": 123, "full_name": "acme/hatchery"},
            "/users/johnbusiness": {"id": 43, "login": "johnbusiness"},
            "/repos/acme/hatchery/issues/7/comments": {
                "id": 99,
                "html_url": "https://github.com/acme/hatchery/issues/7#issuecomment-99",
            },
        }
        return httpx.Response(200, json=responses[request.url.path.removeprefix("/api")])

    @contextlib.asynccontextmanager
    async def client(_provider):
        async with httpx.AsyncClient(
            base_url="https://api.github.com/", transport=httpx.MockTransport(respond)
        ) as value:
            yield value

    monkeypatch.setattr(destinations, "_client", client)
    result = await destinations.start_thread(
        directory.id,
        "github",
        "acme/hatchery#7",
        "Please review @team",
        ["john"],
        delivery_key="turn_2",
    )

    assert result["status"] == "sent"
    binding = await chats.binding("github:repo:123:issue:7")
    assert binding is not None
    assert binding.chat_id == directory.id
    posted = next(
        request for request in requests if request.url.path.endswith("/comments")
    )
    assert "@johnbusiness Please review @\u200bteam" in json.loads(posted.read())["body"]


async def test_start_thread_replay_does_not_post_twice(directory, monkeypatch):
    requests = []

    @contextlib.asynccontextmanager
    async def client(_provider):
        async with slack_client(requests) as value:
            yield value

    monkeypatch.setattr(destinations, "_client", client)
    arguments = (directory.id, "slack", "T1/C1", "Done", ["john"])
    first = await destinations.start_thread(*arguments, delivery_key="turn_1")
    second = await destinations.start_thread(*arguments, delivery_key="turn_1")

    assert second == first
    assert [path for path, _ in requests].count("/chat.postMessage") == 1

    with pytest.raises(ValueError, match="reply inline"):
        await destinations.start_thread(
            directory.id,
            "slack",
            "T1/C1",
            "A different notification",
            ["john"],
            delivery_key="turn_2",
        )
