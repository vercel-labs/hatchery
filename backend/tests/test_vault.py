"""The secret cipher and names. Ported from agentmesh `tests/unit/test_vault.py`; the
vault lifecycle runs end to end in `tests/serve/test_api.py` and `test_service.py`."""

import base64
import pathlib

import cryptography.exceptions
import pytest

from hatchery import vault


def test_secret_cipher_binds_ciphertext_to_agent_and_name() -> None:
    cipher = vault.SecretCipher.decode(base64.urlsafe_b64encode(b"k" * 32).rstrip(b"=").decode())
    envelope = cipher.encrypt("ada", "STRIPE_WEBHOOK_SECRET", "whsec_private")

    assert cipher.decrypt("ada", "STRIPE_WEBHOOK_SECRET", envelope) == "whsec_private"
    assert b"whsec_private" not in envelope
    with pytest.raises(cryptography.exceptions.InvalidTag):
        cipher.decrypt("grace", "STRIPE_WEBHOOK_SECRET", envelope)
    with pytest.raises(cryptography.exceptions.InvalidTag):
        cipher.decrypt("ada", "OTHER_SECRET", envelope)


@pytest.mark.parametrize("name", ["lowercase", "HAS-DASH", "_PREFIX", "A" * 65])
def test_secret_names_are_safe_environment_variable_names(name: str) -> None:
    with pytest.raises(ValueError, match="uppercase"):
        vault.validate_secret_name(name)


@pytest.mark.parametrize("name", ["PYTHONPATH", "HATCHERY_DATA", "HATCHERY_ANYTHING", "VERCEL_X"])
def test_secret_names_cannot_replace_runtime_environment(name: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        vault.validate_secret_name(name)


def test_local_key_is_created_once_and_private(tmp_path: pathlib.Path) -> None:
    assert vault.local_secret_key(tmp_path, create=False) is None
    key = vault.local_secret_key(tmp_path, create=True)

    assert key is not None and vault.local_secret_key(tmp_path, create=False) == key
    assert (tmp_path / "secrets-key").stat().st_mode & 0o777 == 0o600
