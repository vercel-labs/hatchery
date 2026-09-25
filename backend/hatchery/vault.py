"""Agent-scoped encrypted secrets, stored in durable Rotor state (so in the DB).

Ported from agentmesh `vault.py`. One `SecretVault` process per agent holds AES-GCM
envelopes; associated data binds agent and name, so ciphertext cannot move between
agents. The root key is `HATCHERY_SECRETS_KEY` (base64url, 32 bytes), or outside Vercel
a generated `<data dir>/secrets-key`. Values are decrypted only for the operator's
reveal and for that agent's serve sandbox; they never enter tool arguments, model
history, or transcripts. Retiring an agent clears its vault and leaves a tombstone.
"""

import base64
import dataclasses
import os
import pathlib
import re
import secrets
import typing
from typing import Any

import cryptography.hazmat.primitives.ciphers.aead
import rotor

from hatchery import environment, store

SCOPE = "agents"
SECRET_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
RESERVED_SECRET_NAMES = {"HATCHERY_DATA", "HOME", "LANG", "PATH", "PYTHONPATH"}
MAX_SECRET_BYTES = 16 * 1024


def validate_secret_name(name: str) -> str:
    if not SECRET_NAME.fullmatch(name):
        raise ValueError("secret name must be 1-64 uppercase letters, digits, or underscores")
    if name in RESERVED_SECRET_NAMES or name.startswith(("HATCHERY_", "VERCEL_")):
        raise ValueError("secret name uses a reserved runtime environment name")
    return name


class SecretCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("secret encryption key must be 32 bytes")
        self._cipher = cryptography.hazmat.primitives.ciphers.aead.AESGCM(key)

    @classmethod
    def decode(cls, value: str) -> SecretCipher:
        try:
            key = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except ValueError as error:
            raise ValueError("HATCHERY_SECRETS_KEY must be base64url") from error
        return cls(key)

    def encrypt(self, agent_id: str, name: str, value: str) -> bytes:
        raw = value.encode()
        if not raw or len(raw) > MAX_SECRET_BYTES:
            raise ValueError(f"secret value must be 1-{MAX_SECRET_BYTES} UTF-8 bytes")
        nonce = secrets.token_bytes(12)
        return b"\x01" + nonce + self._cipher.encrypt(nonce, raw, _aad(agent_id, name))

    def decrypt(self, agent_id: str, name: str, envelope: bytes) -> str:
        if len(envelope) < 30 or envelope[0] != 1:
            raise ValueError("secret envelope is invalid")
        nonce = envelope[1:13]
        return self._cipher.decrypt(nonce, envelope[13:], _aad(agent_id, name)).decode()


def _aad(agent_id: str, name: str) -> bytes:
    return f"hatchery-secret-v1\0{agent_id}\0{name}".encode()


def local_secret_key(root: pathlib.Path, *, create: bool) -> str | None:
    """Load or create the local server key without following a symlink."""
    path = root / "secrets-key"
    if path.is_symlink():
        raise ValueError("secrets-key must not be a symlink")
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        if not create:
            return None
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        value = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(value + "\n")
    SecretCipher.decode(value)
    return value


@rotor.message
class PutEncryptedSecret:
    name: str
    envelope: bytes
    request_id: str


@rotor.message
class DeleteSecret:
    name: str
    request_id: str


@rotor.message
class RetireSecrets:
    request_id: str


@rotor.state
class SecretVaultState:
    agent_id: str = ""
    secrets: dict[str, bytes] = dataclasses.field(default_factory=dict)
    updated_at: dict[str, float] = dataclasses.field(default_factory=dict)
    retired: bool = False


class SecretVault(rotor.DurableProcess[SecretVaultState]):
    @rotor.on
    async def start(self, msg: rotor.Start) -> None:
        self.state.agent_id = msg.input["agent_id"]

    @rotor.on
    async def put(self, msg: PutEncryptedSecret) -> dict[str, Any]:
        name = validate_secret_name(msg.name)
        self.state.secrets[name] = msg.envelope
        self.state.updated_at[name] = environment.Environment.current().now()
        return {"name": name, "stored": True}

    @rotor.on
    async def delete(self, msg: DeleteSecret) -> dict[str, Any]:
        name = validate_secret_name(msg.name)
        existed = self.state.secrets.pop(name, None) is not None
        self.state.updated_at.pop(name, None)
        return {"name": name, "deleted": existed}

    @rotor.on
    async def retire(self, msg: RetireSecrets) -> None:
        self.state.secrets.clear()
        self.state.updated_at.clear()
        self.state.retired = True

    @rotor.query
    def inventory(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "stored": True, "updated_at": self.state.updated_at.get(name)}
            for name in sorted(self.state.secrets)
        ]

    @rotor.query
    def encrypted(self, name: str) -> bytes | None:
        return self.state.secrets.get(validate_secret_name(name))

    @rotor.query
    def status(self) -> dict[str, bool]:
        return {"retired": self.state.retired}


def _client() -> rotor.Client:
    from hatchery.agent import runtime

    return runtime.client


def _id(agent_id: str) -> str:
    return rotor.singleton_id(SecretVault, agent_id, scope=SCOPE)


async def _ensure(agent_id: str) -> str:
    handle = await _client().start(
        SecretVault, input={"agent_id": agent_id}, key=agent_id, scope=SCOPE
    )
    return handle.id


def _cipher() -> SecretCipher:
    key = environment.Environment.current().secrets_key
    if key is None and not os.environ.get("VERCEL"):
        key = local_secret_key(store.data_dir(), create=True)
    if key is None:
        raise RuntimeError("HATCHERY_SECRETS_KEY is not configured")
    return SecretCipher.decode(key)


async def inventory(agent_id: str) -> list[dict[str, Any]]:
    """Stored names and update times; never values."""
    try:
        value, _ = await _client().query(_id(agent_id), SecretVault.inventory)
    except rotor.ProcessNotFound:
        return []
    return typing.cast(list[dict[str, Any]], value)


async def set(agent_id: str, name: str, value: str, request_id: str) -> dict[str, Any]:
    """Store or rotate one value; the request ID deduplicates retries."""
    name = validate_secret_name(name)
    envelope = _cipher().encrypt(agent_id, name, value)
    outcome = await _client().send(
        await _ensure(agent_id),
        PutEncryptedSecret(name, envelope, request_id),
        idempotency_key=f"secret:set:{request_id}",
    )
    return {"name": name, "outcome": outcome}


async def reveal(agent_id: str, name: str) -> str:
    try:
        envelope, _ = await _client().query(
            _id(agent_id), SecretVault.encrypted, validate_secret_name(name)
        )
    except rotor.ProcessNotFound as error:
        raise KeyError(name) from error
    if envelope is None:
        raise KeyError(name)
    return _cipher().decrypt(agent_id, name, envelope)


async def delete(agent_id: str, name: str, request_id: str) -> dict[str, Any]:
    name = validate_secret_name(name)
    outcome = await _client().send(
        await _ensure(agent_id),
        DeleteSecret(name, request_id),
        idempotency_key=f"secret:delete:{request_id}",
    )
    return {"name": name, "outcome": outcome}


async def values(agent_id: str) -> dict[str, str]:
    """Every decrypted value of one agent, for its serve sandbox only."""
    return {
        item["name"]: await reveal(agent_id, item["name"])
        for item in await inventory(agent_id)
    }


async def retired(agent_id: str) -> bool:
    try:
        status, _ = await _client().query(_id(agent_id), SecretVault.status)
    except rotor.ProcessNotFound:
        return False
    return bool(status["retired"])


async def retire(agent_id: str, request_id: str) -> None:
    await _client().send(
        await _ensure(agent_id),
        RetireSecrets(request_id),
        idempotency_key=f"secret:retire:{request_id}",
    )
