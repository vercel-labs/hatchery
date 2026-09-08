"""Discover linked people and destinations; send notifications or share threads.

Descriptions stay plain text. Tools return candidates, never choose fuzzy matches.
The default Connect installation is intentional: v1 does not route across tenants.
"""

import contextlib
import difflib
import hashlib
import html
import json
import logging
import os
import re
import typing
import unicodedata

import httpx
from vercel import connect

import auth
from channels import github
from store import auth as auth_store
from store import chats, events, spaces, turns

Provider = typing.Literal["slack", "github"]
LIMIT = 10
log = logging.getLogger(__name__)


class _UncertainDelivery(RuntimeError):
    pass


class SlackScopeRequired(RuntimeError):
    """A permanent permission failure with safe, provider-reported diagnostics."""

    def __init__(self, method: str, body: dict, headers: httpx.Headers):
        scopes = {}
        provided = body.get("provided")
        for field, value in {
            "needed": body.get("needed"),
            "provided": provided if provided is not None else headers.get("x-oauth-scopes"),
            "accepted_scopes": headers.get("x-accepted-oauth-scopes"),
        }.items():
            parsed = sorted(set(value.replace(",", " ").split())) if isinstance(value, str) else None
            scopes[field] = parsed
        connector = os.environ.get("SLACK_CONNECTOR", "unconfigured")
        message = json.dumps({
            field: body[field] for field in ("error", "needed", "provided")
            if isinstance(body.get(field), str)
        })
        super().__init__(message)
        self.result = {
            "status": "failed", "provider": "slack", "error": "missing_scope",
            "method": method, "connector": connector, **scopes, "detail": message,
        }


def _rank(query: str, candidates: list[dict]) -> list[dict]:
    query = unicodedata.normalize("NFKC", query).strip().lstrip("@#").casefold()
    if not query:
        raise ValueError("provide a name, handle, ID, or destination")
    ranked = []
    for candidate in candidates:
        score = 0.0
        for alias in candidate["aliases"]:
            value = unicodedata.normalize("NFKC", str(alias or "")).casefold()
            if not value:
                continue
            if query == value:
                score = max(score, 3.0)
            elif query in value:
                score = max(score, 2.0)
            else:
                score = max(score, difflib.SequenceMatcher(None, query, value).ratio())
        if score >= 0.65:
            ranked.append((score, candidate))
    ranked.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [
        {**{key: value for key, value in candidate.items() if key != "aliases"},
         "match": "exact" if score == 3 else "partial" if score == 2 else "fuzzy"}
        for score, candidate in ranked[:LIMIT]
    ]


async def _context(chat_id: str):
    chat = await chats.get(chat_id)
    user = await auth_store.get_user(chat.user_id) if chat and chat.user_id else None
    if not auth.allowed_user(user):
        raise ValueError("communication requires a chat owned by an allowed Hatchery user")
    space = await spaces.get(chat.space_id) if chat.space_id else None
    return user, space


@contextlib.asynccontextmanager
async def _client(provider: Provider):
    if provider not in {"slack", "github"}:
        raise ValueError("unsupported communication provider")
    connector = os.environ.get(f"{provider.upper()}_CONNECTOR")
    if not connector:
        raise ValueError(f"{provider} connector is not configured")
    token = await connect.get_token(connector, subject=connect.ConnectAppTokenSubject())
    async with httpx.AsyncClient(
        base_url="https://slack.com/api/" if provider == "slack" else "https://api.github.com/",
        headers={
            "authorization": f"Bearer {token}",
            "accept": "application/json",
            "x-github-api-version": "2022-11-28",
        },
        timeout=30,
    ) as client:
        yield client


async def _api(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    response = await client.request(method, path, **kwargs)
    if response.status_code >= 500:
        response.raise_for_status()
    if response.status_code >= 300:
        raise RuntimeError(f"provider request failed (HTTP {response.status_code}); check permissions or rate limits")
    body = response.json()
    if body.get("ok") is False:
        if body.get("error") == "missing_scope":
            error = SlackScopeRequired(path, body, response.headers)
            log.warning("%s", error)
            raise error
        if path == "chat.postMessage" and body.get("error") in {"internal_error", "fatal_error", "request_timeout"}:
            raise _UncertainDelivery("Slack may have accepted the message")
        raise RuntimeError(f"Slack {path} failed: {body.get('error', 'unknown_error')}")
    return body


async def _slack_team(client: httpx.AsyncClient, user: dict) -> str:
    identity = await _api(client, "POST", "auth.test")
    team = identity["team_id"]
    connection = user.get("slack") or {}
    if connection.get("team_id") != team or not connection.get("user_id"):
        raise ValueError("connect Slack in the bot's workspace before using Slack destinations")
    return team


async def _slack_members(client: httpx.AsyncClient, channel_id: str) -> set[str]:
    members = set()
    cursor = ""
    while True:
        body = await _api(client, "POST", "conversations.members", data={
            "channel": channel_id, "limit": "200", "cursor": cursor,
        })
        members.update(body.get("members", []))
        cursor = (body.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor:
            return members


def _github_ref(query: str) -> tuple[str, int] | None:
    match = re.fullmatch(
        r"(?:https://github\.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"
        r"(?:#|/(?:issues|pull)/)([1-9][0-9]*)(?:#[A-Za-z0-9_-]+)?/?",
        query.strip(),
    )
    return (match[1], int(match[2])) if match else None


async def find_channels(chat_id: str, provider: Provider, query: str) -> list[dict]:
    """Find public Slack bot-member channels or GitHub issues/PRs in this space."""
    _rank(query, [])  # reject empty queries before making provider calls
    user, space = await _context(chat_id)
    candidates = []
    async with _client(provider) as client:
        if provider == "slack":
            team = await _slack_team(client, user)
            cursor = ""
            while True:
                body = await _api(client, "POST", "users.conversations", data={
                    "types": "public_channel", "exclude_archived": "true",
                    "limit": "200", "cursor": cursor,
                })
                for channel in body.get("channels", []):
                    if channel.get("is_archived") or channel.get("is_private"):
                        continue
                    identifier = f"{team}/{channel['id']}"
                    candidates.append({
                        "id": identifier, "provider": "slack", "name": channel["name"],
                        "team_id": team, "channel_id": channel["id"],
                        "private": bool(channel.get("is_private")),
                        "aliases": [identifier, channel["id"], channel["name"]],
                    })
                cursor = (body.get("response_metadata") or {}).get("next_cursor", "")
                if not cursor:
                    break
        else:
            repos = {repo.casefold(): repo for repo in space.repos} if space else {}
            reference = _github_ref(query)
            if reference:
                repo, number = reference
                if repo.casefold() not in repos:
                    raise ValueError("GitHub destination must belong to this space's repositories")
                items = [await _api(client, "GET", f"repos/{repo}/issues/{number}")]
            else:
                items = []
                # Quoted words prevent a model-supplied qualifier from widening scope.
                words = re.findall(r"[\w.-]+", query)
                if not words:
                    raise ValueError("provide an issue title or owner/repo#number")
                terms = " ".join(f'"{word}"' for word in words)
                for repo in repos.values():
                    body = await _api(client, "GET", "search/issues", params={
                        "q": f"repo:{repo} {terms} in:title", "per_page": LIMIT,
                    })
                    if body.get("incomplete_results"):
                        raise RuntimeError("GitHub search was incomplete; narrow the query")
                    items.extend(body.get("items", []))
            for item in items:
                ref = _github_ref(item.get("html_url", ""))
                if not ref or ref[0].casefold() not in repos:
                    continue
                repo, number = ref
                identifier = f"{repo}#{number}"
                candidates.append({
                    "id": identifier, "provider": "github", "name": item["title"],
                    "repo": repo, "number": number, "url": item["html_url"],
                    "kind": "pull" if "pull_request" in item else "issue",
                    "match": "exact" if reference else "search",
                })
            return candidates[:LIMIT]
    return _rank(query, candidates)


async def find_people(chat_id: str, query: str) -> list[dict]:
    """Search linked identities, not unrelated global provider directories."""
    _rank(query, [])
    user, _ = await _context(chat_id)
    candidates = []
    for person in await auth_store.list_people():
        if not auth.allowed_user(person):
            continue
        slack = person.get("slack") or {}
        gh = person.get("github") or {}
        candidates.append({
            "id": person["id"], "name": person.get("name"), "username": person.get("username"),
            "slack": {key: slack.get(key) for key in ("team_id", "user_id", "user")}
            if slack.get("team_id") and slack.get("user_id") else None,
            "github": {key: gh.get(key) for key in ("id", "login", "name")}
            if gh.get("id") and gh.get("login") else None,
            "aliases": [person["id"], person.get("name"), person.get("username"),
                        slack.get("user"), gh.get("login"), gh.get("name"),
                        *( ["me", "myself"] if person["id"] == user["id"] else [])],
        })
    found = _rank(query, candidates)
    # Exact linked handles need no directory scan. On a miss, enrich only linked
    # people in the owner's workspace with current Slack names (never new users).
    if not any(item["match"] == "exact" for item in found) and os.environ.get("SLACK_CONNECTOR") and user.get("slack"):
        try:
            async with _client("slack") as client:
                team = await _slack_team(client, user)
                linked = {
                    item["slack"]["user_id"]: item for item in candidates
                    if item["slack"] and item["slack"]["team_id"] == team
                }
                cursor = ""
                while linked:
                    body = await _api(client, "POST", "users.list", data={"limit": "200", "cursor": cursor})
                    for profile in body.get("members", []):
                        candidate = linked.pop(profile.get("id"), None)
                        if candidate is None or profile.get("deleted") or profile.get("is_bot"):
                            continue
                        details = profile.get("profile") or {}
                        candidate["slack"]["user"] = profile.get("name")
                        candidate["slack"]["display_name"] = details.get("display_name") or details.get("real_name")
                        candidate["aliases"].extend([
                            profile.get("name"), details.get("display_name"), details.get("real_name"),
                        ])
                    cursor = (body.get("response_metadata") or {}).get("next_cursor", "")
                    if not cursor:
                        break
        except (httpx.HTTPError, connect.ConnectError, RuntimeError, ValueError) as error:
            log.warning("Slack profile enrichment unavailable (%s); using linked names", type(error).__name__)
        found = _rank(query, candidates)
    return found


async def start_shared_thread(
    chat_id: str, provider: Provider, destination: str, text: str,
    people: list[str] | None = None, *, delivery_key: str,
    tool_call_id: str | None = None, excluded_message_ids: list[str] | None = None,
) -> dict:
    """Post a root notification and attach its external conversation to this chat."""
    return await send_message(
        chat_id, provider, destination, text, people, delivery_key=delivery_key,
        _sharing={"tool_call_id": tool_call_id, "excluded_message_ids": list(excluded_message_ids or [])},
    )


async def recover_shared_thread(token: str) -> chats.Binding | None:
    """Repair an exact thread from a durable sent receipt, never from inbound data.

    Inbound callers hold the destination lock before recovering and claiming.
    Scanning chat receipts is intentional for this small application.
    """
    existing = await chats.binding(token)
    if existing is not None:
        return existing
    match = None
    for chat in await chats.list_all():
        for _, receipt in reversed(await events.read(chat.id, "notifications")):
            if receipt.get("result", {}).get("status") != "sent" or receipt.get("sharing", {}).get("token") != token:
                continue
            if match is not None:
                raise ValueError("destination has conflicting sent receipts in different chats")
            match = (chat.id, receipt)
            break
    if match is not None:
        chat_id, receipt = match
        await _finalize_sharing(chat_id, receipt["result"], receipt["sharing"])
    return await chats.binding(token)


async def _finalize_sharing(chat_id: str, result: dict, record: dict) -> dict:
    # The sent receipt is already durable. Every following write can be repaired
    # from it, even when the independent sharing record was never written.
    state = record["state"]
    lock_key = (
        f"sharing:slack:{state['team_id']}:{state['channel_id']}"
        if record["provider"] == "slack" else f"sharing:{record['token']}"
    )
    # Always destination/token -> chat, including recovery. Inbound releases its
    # destination/token lock before taking the chat lock; never reverse this order.
    async with turns.run(lock_key), turns.run(chat_id):
        # A replay may have read the first receipt while permalink enrichment was
        # still in progress. Prefer the newest durable version after waiting.
        for _, receipt in reversed(await events.read(chat_id, "notifications")):
            if (
                receipt.get("sharing", {}).get("id") == record["id"]
                and receipt["sharing"].get("token") == record["token"]
                and receipt.get("result", {}).get("status") == "sent"
            ):
                result, record = receipt["result"], receipt["sharing"]
                break
        await chats.bind(record["token"], chat_id, record["provider"], record["state"])
        if not any(saved.get("id") == record["id"] for _, saved in await events.read(chat_id, "sharing")):
            await events.append(chat_id, "sharing", record)
        if not any(
            saved.get("type") == "messages.changed" and saved.get("sharing_id") == record["id"]
            for _, saved in await events.read(chat_id, "ui")
        ):
            await events.append(chat_id, "ui", {"type": "messages.changed", "sharing_id": record["id"]})
    return result


async def send_message(
    chat_id: str, provider: Provider, destination: str, text: str,
    people: list[str] | None = None, *, delivery_key: str,
    _sharing: dict | None = None,
) -> dict:
    """Post once with verified linked mentions; one-off unless sharing is requested."""
    user, space = await _context(chat_id)
    if not text.strip():
        raise ValueError("message cannot be empty")
    people = sorted(set(people or []))
    recipients = []
    for person_id in people:
        person = await auth_store.get_user(person_id)
        if not auth.allowed_user(person):
            raise ValueError("recipient must be an allowed linked Hatchery user")
        recipients.append(person)
    digest = hashlib.sha256(json.dumps(
        [delivery_key, provider, destination, text, people] + (["shared"] if _sharing is not None else []),
        ensure_ascii=False,
    ).encode()).hexdigest()
    # A provider may accept a request whose response is lost. Never blindly retry it.
    for _, record in reversed(await events.read(chat_id, "notifications")):
        if record.get("key") == digest and "result" in record:
            if record.get("sharing") and record["result"]["status"] == "sent":
                return await _finalize_sharing(chat_id, record["result"], record["sharing"])
            return record["result"]
    sharing = None
    if _sharing is not None:
        sharing = {
            "id": f"sharing_{digest[:24]}", "provider": provider, "text": text,
            "tool_call_id": _sharing["tool_call_id"],
            "state": {"sharing_id": f"sharing_{digest[:24]}",
                      "excluded_message_ids": _sharing["excluded_message_ids"]},
        }
    async with _client(provider) as client, contextlib.AsyncExitStack() as locks:
        mentions = []
        if provider == "slack":
            team = await _slack_team(client, user)
            match = re.fullmatch(r"([A-Z0-9]+)/([A-Z0-9]+)", destination)
            if not match or match[1] != team:
                raise ValueError("use a Slack destination ID returned by find_channels in this workspace")
            channel_id = match[2]
            body = await _api(client, "POST", "conversations.info", data={"channel": channel_id})
            channel = body["channel"]
            if channel.get("is_archived") or not channel.get("is_member"):
                raise ValueError("the bot must be a member of an active channel")
            for person in recipients:
                identity = person.get("slack") or {}
                if identity.get("team_id") != team or not identity.get("user_id"):
                    raise ValueError("recipient has no connected Slack identity in this workspace")
                body = await _api(client, "POST", "users.info", data={"user": identity["user_id"]})
                profile = body["user"]
                if profile.get("deleted") or profile.get("is_bot"):
                    raise ValueError("recipient's Slack identity is inactive or a bot")
                mentions.append(f"<@{identity['user_id']}>")
            if recipients or channel.get("is_private"):
                members = await _slack_members(client, channel_id)
                if channel.get("is_private") and user["slack"]["user_id"] not in members:
                    raise ValueError("the chat owner cannot access this private Slack channel")
                if any(person["slack"]["user_id"] not in members for person in recipients):
                    raise ValueError("a recipient is not in this Slack channel; choose a shared channel")
            # Only the verified recipients above can generate mentions, not arbitrary text.
            content = " ".join([*mentions, html.escape(text, quote=False)])
            if len(content) > 40_000:
                raise ValueError("Slack message exceeds 40000 characters")
            if sharing is not None:
                sharing["label"] = f"#{channel.get('name', channel_id)}"
                sharing["state"].update({
                    "team_id": team, "channel_id": channel_id, "user_id": user["slack"]["user_id"],
                })
            path = "chat.postMessage"
            params = {"data": {"channel": channel_id, "text": content, "parse": "none",
                               "unfurl_links": "false", "unfurl_media": "false"}}
        else:
            reference = _github_ref(destination)
            repos = {repo.casefold() for repo in space.repos} if space else set()
            if not reference or reference[0].casefold() not in repos:
                raise ValueError("use owner/repo#number in this space's repositories")
            repo, number = reference
            issue = await _api(client, "GET", f"repos/{repo}/issues/{number}")
            if sharing is not None:
                repository = await _api(client, "GET", f"repos/{repo}")
                owner, name = repository["full_name"].split("/", 1)
                kind = "pull" if issue.get("pull_request") else "issue"
                token = f"github:repo:{repository['id']}:{kind}:{number}"
                # Serialize competing outbound shares of the same issue, even
                # across chats. The atomic bind remains the final collision guard.
                await locks.enter_async_context(turns.run(f"sharing:{token}"))
                existing = await recover_shared_thread(token)
                if existing is not None and existing.chat_id != chat_id:
                    raise ValueError("destination is already bound to another chat")
                for _, receipt in reversed(await events.read(chat_id, "notifications")):
                    if receipt.get("sharing", {}).get("token") == token and receipt.get("result", {}).get("status") == "sent":
                        return await _finalize_sharing(chat_id, receipt["result"], receipt["sharing"])
                if existing is not None:
                    # This conversation was linked by inbound, not by this send.
                    # Do not cut off its history or suppress delayed older replies.
                    return {
                        "status": "already_shared", "provider": provider, "destination": destination,
                        "url": issue.get("html_url", f"https://github.com/{owner}/{name}/issues/{number}"),
                        "detail": "This thread is already linked to this chat; no notification was sent.",
                    }
                sharing.update({"token": token, "label": f"{owner}/{name}#{number}"})
                sharing["state"].update({
                    "owner": owner, "repo": name, "repository_id": repository["id"],
                    "kind": kind, "number": number, "root_comment_id": None,
                    "sender_id": str((user.get("github") or {}).get("id", "")),
                })
            for person in recipients:
                identity = person.get("github") or {}
                login = identity.get("login", "")
                if not re.fullmatch(r"[A-Za-z0-9-]+", login) or not identity.get("id"):
                    raise ValueError("recipient has no connected GitHub identity")
                profile = await _api(client, "GET", f"users/{login}")
                if str(profile["id"]) != str(identity["id"]):
                    raise ValueError("GitHub identity changed; reconnect the recipient's account")
                mentions.append(f"@{profile['login']}")
            # Neutralize unverified user/team mentions in the body.
            content = " ".join([*mentions, text.replace("@", "@\u200b")]) + f"\n\n{github.MARKER}"
            if len(content) > github.COMMENT_LIMIT:
                raise ValueError("GitHub comment is too long")
            path = f"repos/{repo}/issues/{number}/comments"
            params = {"json": {"body": content}}
        if sharing is not None:
            if provider == "slack":
                # Slack's root ts is only known after posting. Inbound claims use
                # this channel lock too, so they cannot steal the post -> bind gap.
                await locks.enter_async_context(turns.run(f"sharing:slack:{team}:{channel_id}"))
            # A concurrent send may have completed while this call waited.
            for _, receipt in reversed(await events.read(chat_id, "notifications")):
                if receipt.get("key") == digest and "result" in receipt:
                    if receipt.get("sharing") and receipt["result"]["status"] == "sent":
                        return await _finalize_sharing(chat_id, receipt["result"], receipt["sharing"])
                    return receipt["result"]
        if not await chats.dedupe(f"notification:{chat_id}:{digest}"):
            return {"status": "unknown", "detail": "Delivery already attempted. Check the destination; do not resend blindly."}
        if sharing is not None:
            # Freeze the boundary before posting, not when a receipt is replayed.
            # Model context can lag behind persisted inbound messages. Retain its
            # order, then append any newer messages and previous sharing roots.
            async with turns.run(chat_id):
                excluded = list(sharing["state"]["excluded_message_ids"])
                for namespace in ("messages", "pending_messages"):
                    excluded.extend(
                        message["id"] for _, message in await events.read(chat_id, namespace) if message.get("id")
                    )
                excluded.extend(f"sharing:{saved['id']}" for _, saved in await events.read(chat_id, "sharing") if saved.get("id"))
                excluded.extend(
                    f"sharing:{receipt['sharing']['id']}"
                    for _, receipt in await events.read(chat_id, "notifications")
                    if receipt.get("sharing", {}).get("id") and receipt.get("result", {}).get("status") == "sent"
                )
                sharing["state"]["excluded_message_ids"] = list(dict.fromkeys(excluded))
        await events.append(chat_id, "notifications", {
            "key": digest, "provider": provider, "destination": destination, "people": people, "text": content,
        })
        try:
            posted = await _api(client, "POST", path, **params)
            if provider == "slack":
                ts = posted["ts"]
                result = {"status": "sent", "provider": provider, "destination": destination,
                          "message_id": ts, "url": f"https://app.slack.com/client/{team}/{channel_id}"}
            else:
                result = {"status": "sent", "provider": provider, "destination": destination,
                          "message_id": str(posted["id"]), "url": posted["html_url"]}
        except (httpx.HTTPError, ValueError, KeyError, _UncertainDelivery):
            result = {"status": "unknown", "detail": "Provider acceptance is uncertain. Check the destination; do not resend blindly."}
        except SlackScopeRequired as error:
            result = error.result
        except RuntimeError as error:
            result = {"status": "failed", "detail": str(error)}
        receipt = {"key": digest, "result": result, "text": content}
        if sharing is not None and result["status"] == "sent":
            sharing["state"]["start_message_id"] = result["message_id"]
            if provider == "slack":
                sharing["token"] = f"slack:{team}:{channel_id}:{result['message_id']}"
                sharing["state"]["thread_ts"] = result["message_id"]
            else:
                sharing["state"]["comment_id"] = posted["id"]
            sharing.update({"url": result["url"], "message_id": result["message_id"]})
            result["sharing"] = {key: value for key, value in sharing.items() if key not in {"token", "state"}}
            receipt["sharing"] = sharing
        # Do not include local finalization in the provider exception handler:
        # a storage/binding failure must never overwrite a known-sent receipt.
        await events.append(chat_id, "notifications", receipt)
        if provider == "slack" and result["status"] == "sent":
            # Persist acceptance before the optional, potentially slow lookup.
            # A crash here is recoverable using the fallback URL and frozen state.
            try:
                link = await _api(client, "POST", "chat.getPermalink", data={
                    "channel": channel_id, "message_ts": result["message_id"],
                })
                permalink = link["permalink"]
            except (httpx.HTTPError, RuntimeError, ValueError, KeyError):
                permalink = result["url"]
            if permalink != result["url"]:
                result["url"] = permalink
                if "sharing" in receipt:
                    receipt["sharing"]["url"] = permalink
                    result["sharing"]["url"] = permalink
                await events.append(chat_id, "notifications", receipt)
        if "sharing" in receipt:
            return await _finalize_sharing(chat_id, result, receipt["sharing"])
        return result
