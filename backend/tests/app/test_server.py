import httpx

import ai
import channels
from app import server
from store import chats, events


def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=server.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_spaces_seed_default():
    async with client() as c:
        listed = (await c.get("/api/spaces")).json()
    assert [s["id"] for s in listed] == ["spc_fabricator"]


async def test_chat_create_and_list():
    async with client() as c:
        created = (await c.post("/api/chats", json={})).json()
        assert created["space_id"] == "spc_fabricator"
        assert created["title"] == "new chat"
        listed = (await c.get("/api/chats")).json()
    assert [x["id"] for x in listed] == [created["id"]]


async def test_chat_messages_from_store():
    for message in (ai.user_message("hi"), ai.assistant_message("hello")):
        await events.append("chat_x", "messages", message.model_dump(mode="json"))
    async with client() as c:
        ui = (await c.get("/api/chats/chat_x/messages")).json()
        empty = (await c.get("/api/chats/chat_empty/messages")).json()
    assert [m["role"] for m in ui] == ["user", "assistant"]
    assert ui[0]["parts"][0]["text"] == "hi"
    assert empty == []


async def test_hub_lands_inbound_in_one_chat():
    hub = server.bot.hub
    await hub.dispatch(
        "slack",
        channels.Inbound(token="C1:1.0", text="from slack", state={"channel_id": "C1"}, title="a thread"),
    )
    await hub.dispatch("slack", channels.Inbound(token="C1:1.0", text="again", state={}))
    [chat] = await chats.list_all()
    assert chat.trigger == "slack:C1:1.0"
    assert chat.title == "a thread"
    stored = await events.read(chat.id, "messages")
    assert len(stored) == 2


async def test_hub_dedupe_is_durable():
    hub = server.bot.hub
    assert await hub.dedupe("slack:ev1") is True
    assert await hub.dedupe("slack:ev1") is False
