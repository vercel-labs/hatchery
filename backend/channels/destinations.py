"""Discover linked people and bot destinations; send one-off notifications.

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
from store import chats, events, spaces

Provider = typing.Literal["slack", "github"]
LIMIT = 10
log = logging.getLogger(__name__)


class _UncertainDelivery(RuntimeError):
    pass


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
    """Find Slack bot-member channels or GitHub issues/PRs in this space."""
    _rank(query, [])  # reject empty queries before making provider calls
    user, space = await _context(chat_id)
    candidates = []
    async with _client(provider) as client:
        if provider == "slack":
            team = await _slack_team(client, user)
            cursor = ""
            while True:
                body = await _api(client, "POST", "users.conversations", data={
                    "types": "public_channel,private_channel", "exclude_archived": "true",
                    "limit": "200", "cursor": cursor,
                })
                for channel in body.get("channels", []):
                    if channel.get("is_archived"):
                        continue
                    if channel.get("is_private") and user["slack"]["user_id"] not in await _slack_members(client, channel["id"]):
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


async def send_message(
    chat_id: str, provider: Provider, destination: str, text: str,
    people: list[str] | None = None, *, delivery_key: str,
) -> dict:
    """Post once, with verified linked mentions; never bind or transfer a chat."""
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
        [delivery_key, provider, destination, text, people], ensure_ascii=False,
    ).encode()).hexdigest()
    # A provider may accept a request whose response is lost. Never blindly retry it.
    for _, record in await events.read(chat_id, "notifications"):
        if record.get("key") == digest and "result" in record:
            return record["result"]
    async with _client(provider) as client:
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
            path = "chat.postMessage"
            params = {"data": {"channel": channel_id, "text": content, "parse": "none",
                               "unfurl_links": "false", "unfurl_media": "false"}}
        else:
            reference = _github_ref(destination)
            repos = {repo.casefold() for repo in space.repos} if space else set()
            if not reference or reference[0].casefold() not in repos:
                raise ValueError("use owner/repo#number in this space's repositories")
            repo, number = reference
            await _api(client, "GET", f"repos/{repo}/issues/{number}")
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
        if not await chats.dedupe(f"notification:{chat_id}:{digest}"):
            return {"status": "unknown", "detail": "Delivery already attempted. Check the destination; do not resend blindly."}
        await events.append(chat_id, "notifications", {
            "key": digest, "provider": provider, "destination": destination, "people": people,
        })
        try:
            posted = await _api(client, "POST", path, **params)
            if provider == "slack":
                ts = posted["ts"]
                result = {"status": "sent", "provider": provider, "destination": destination,
                          "message_id": ts, "url": f"https://app.slack.com/client/{team}/{channel_id}"}
                try:
                    link = await _api(client, "POST", "chat.getPermalink", data={
                        "channel": channel_id, "message_ts": ts,
                    })
                    result["url"] = link["permalink"]
                except (httpx.HTTPError, RuntimeError, ValueError, KeyError):
                    pass  # The post succeeded even if permalink lookup did not.
            else:
                result = {"status": "sent", "provider": provider, "destination": destination,
                          "message_id": str(posted["id"]), "url": posted["html_url"]}
        except (httpx.HTTPError, ValueError, KeyError, _UncertainDelivery):
            result = {"status": "unknown", "detail": "Provider acceptance is uncertain. Check the destination; do not resend blindly."}
        except RuntimeError as error:
            result = {"status": "failed", "detail": str(error)}
        await events.append(chat_id, "notifications", {"key": digest, "result": result})
        return result
