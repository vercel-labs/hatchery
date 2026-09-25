"""Operator serving endpoints on the main app. Ported from the secret lifecycle of
agentmesh `tests/integration/test_gateway.py`."""

import pytest

from hatchery.app import server

from tests.serve import conftest

AGENT = conftest.AGENT


async def test_operator_manages_agent_scoped_secrets_without_listing_values(
    serve: conftest.Serve,
) -> None:
    async with serve() as app:
        setting = await app.app.post(
            f"/api/agents/{AGENT}/secrets/STRIPE_WEBHOOK_SECRET",
            json={"request_id": "secret-set-1", "value": "whsec_vaulted_only"},
        )
        assert setting.status_code == 200
        await app.rt.drain()

        listed = await app.app.get(f"/api/agents/{AGENT}/secrets")
        [item] = listed.json()["secrets"]
        assert item == {
            "name": "STRIPE_WEBHOOK_SECRET",
            "stored": True,
            "updated_at": item["updated_at"],
            "note": None,
        }
        assert item["updated_at"] is not None
        assert "whsec_vaulted_only" not in listed.text

        # Setting an existing name rotates it.
        rotated = await app.app.post(
            f"/api/agents/{AGENT}/secrets/STRIPE_WEBHOOK_SECRET",
            json={"request_id": "secret-rotate-1", "value": "whsec_rotated"},
        )
        assert rotated.status_code == 200
        await app.rt.drain()
        revealed = await app.app.post(
            f"/api/agents/{AGENT}/secrets/STRIPE_WEBHOOK_SECRET/reveal",
            json={"request_id": "secret-reveal-1"},
        )
        assert revealed.json() == {"name": "STRIPE_WEBHOOK_SECRET", "value": "whsec_rotated"}
        assert revealed.headers["cache-control"] == "no-store"

        deleted = await app.app.post(
            f"/api/agents/{AGENT}/secrets/STRIPE_WEBHOOK_SECRET/delete",
            json={"request_id": "secret-delete-1"},
        )
        assert deleted.json()["name"] == "STRIPE_WEBHOOK_SECRET"
        assert deleted.json()["outcome"] in ("enqueued", "delivered")
        await app.rt.drain()
        assert (await app.app.get(f"/api/agents/{AGENT}/secrets")).json() == {"secrets": []}
        missing = await app.app.post(
            f"/api/agents/{AGENT}/secrets/STRIPE_WEBHOOK_SECRET/reveal",
            json={"request_id": "secret-reveal-2"},
        )
        assert missing.status_code == 404


async def test_invalid_names_unknown_agents_and_schedules_are_rejected(
    serve: conftest.Serve,
) -> None:
    async with serve() as app:
        reserved = await app.app.post(
            f"/api/agents/{AGENT}/secrets/HATCHERY_DATA",
            json={"request_id": "bad-1", "value": "x"},
        )
        unknown = await app.app.get("/api/agents/nobody/secrets")
        schedule = await app.app.post(
            f"/api/agents/{AGENT}/schedules/nothing/pause", json={"request_id": "pause-1"}
        )
        status = await app.app.get(f"/api/agents/{AGENT}/serve")
        async with app.public("nobody") as public:
            unknown_host = await public.get("/api/anything")

    # An agent host with no agent directory on main fails closed.
    assert unknown_host.status_code == 404
    assert reserved.status_code == 422 and "reserved" in reserved.json()["detail"]
    assert unknown.status_code == 404
    assert schedule.status_code == 404
    assert status.status_code == 200
    assert status.json()["url"] == f"http://{AGENT}.localhost"
    assert status.json()["deployed_revision"] is None and status.json()["routes"] == 0


@pytest.mark.parametrize(
    "path", ["/serve", "/routes", "/schedules", "/secrets"]
)
async def test_serving_endpoints_require_a_session(
    serve: conftest.Serve, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    async def signed_out(_request):
        return None

    monkeypatch.setattr(server.auth, "current_user", signed_out)
    async with serve() as app:
        response = await app.app.get(f"/api/agents/{AGENT}{path}")

    assert response.status_code == 401
