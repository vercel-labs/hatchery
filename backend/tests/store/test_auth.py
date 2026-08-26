import datetime

from store import auth


async def test_session_ids_are_hashed_and_expire(local_store):
    await auth.save_user({"id": "user_1", "name": "Ada"})
    session_id = await auth.create_session("user_1", datetime.timedelta(days=1))

    assert not (local_store / "auth" / "sessions" / f"{session_id}.json").exists()
    assert (await auth.session_user(session_id))["name"] == "Ada"

    await auth.delete_session(session_id)
    assert await auth.session_user(session_id) is None


async def test_oauth_state_is_one_time():
    await auth.save_oauth_state(
        "state", {"nonce": "nonce", "redirect_uri": "http://localhost/callback"}, datetime.timedelta(minutes=1)
    )

    assert (await auth.consume_oauth_state("state"))["nonce"] == "nonce"
    assert await auth.consume_oauth_state("state") is None
