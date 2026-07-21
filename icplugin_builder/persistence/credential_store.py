"""Encrypted credential store (design Req 14.1, 14.2, 14.5; Property 27).

The builder stores user-supplied secrets -- InsightConnect tenant API keys and
git credentials for the private production-plugin repository -- so the operator
does not have to re-enter them on every session. Because these are secrets on a
local filesystem, the design mandates that they are **never** written in
plaintext: each value is stored as `Fernet <https://cryptography.io/>`_
ciphertext (AES-128-CBC + HMAC) under a key derived with ``scrypt`` from a
*master secret*. The master secret itself is never persisted by this module; it
comes either from an OS keyring entry or from the operator's access passphrase
(see :meth:`CredentialStore.from_passphrase` and
:meth:`CredentialStore.from_keyring`).

Guarantees provided here:

* **No plaintext at rest** (Req 14.1) -- ``store`` encrypts the value before it
  touches the disk and the on-disk blob contains only the salt/KDF parameters
  and Fernet tokens, never the secret or the master secret.
* **Reusable across restarts** (Req 14.2) -- the per-store ``scrypt`` salt is
  persisted alongside the ciphertext, so a fresh :class:`CredentialStore` built
  from the same master secret re-derives the same key and decrypts previously
  stored credentials.
* **Deletion removes plaintext and ciphertext** (Req 14.5) -- ``delete`` rewrites
  the store without the named entry, so neither a plaintext nor a ciphertext
  copy of the deleted credential remains on disk.

The store file is a small JSON document written atomically (temp file +
``os.replace``) so a crash mid-write can never leave a half-written store or a
partially stored credential.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Dict, List, Union

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

__all__ = [
    "CredentialStoreError",
    "CredentialNotFoundError",
    "CredentialDecryptionError",
    "CredentialStore",
]

#: On-disk format version, stored in the blob so future changes can migrate.
_FORMAT_VERSION = 1

#: ``scrypt`` cost parameters. ``n`` must be a power of two; these are the
#: interactive-login parameters recommended by the ``cryptography`` docs and are
#: comfortably strong for a single local operator while staying fast enough to
#: derive on demand.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

#: Length of the derived key material in bytes. Fernet requires a 32-byte key.
_KEY_LENGTH = 32

#: Length of the random per-store salt in bytes.
_SALT_LENGTH = 16


class CredentialStoreError(Exception):
    """Base class for credential-store failures."""


class CredentialNotFoundError(CredentialStoreError, KeyError):
    """Raised when a credential name is not present in the store."""


class CredentialDecryptionError(CredentialStoreError):
    """Raised when a stored credential cannot be decrypted.

    This usually means the master secret (passphrase or keyring entry) does not
    match the one used to store the credential, or the on-disk ciphertext has
    been tampered with (Fernet authenticates its ciphertext).
    """


def _derive_key(master_secret: bytes, salt: bytes) -> bytes:
    """Derive a urlsafe-base64 Fernet key from ``master_secret`` and ``salt``.

    Args:
        master_secret: The secret bytes obtained from the keyring or passphrase.
        salt: The per-store random salt persisted with the ciphertext.

    Returns:
        A 32-byte urlsafe-base64-encoded key suitable for :class:`Fernet`.
    """
    kdf = Scrypt(salt=salt, length=_KEY_LENGTH, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    raw = kdf.derive(master_secret)
    return base64.urlsafe_b64encode(raw)


def _coerce_secret(master_secret: Union[str, bytes]) -> bytes:
    """Normalize a master secret to bytes."""
    if isinstance(master_secret, str):
        return master_secret.encode("utf-8")
    return bytes(master_secret)


class CredentialStore:
    """Persist and retrieve secrets as Fernet ciphertext on the local disk.

    A store owns a single JSON file at ``path``. The master secret is held only
    in memory for the lifetime of the instance; it is never written to disk.
    Construct a store directly with an in-memory ``master_secret`` (used by
    tests and callers that manage their own secret) or via
    :meth:`from_passphrase` / :meth:`from_keyring`.
    """

    def __init__(self, path: Union[str, Path], master_secret: Union[str, bytes]) -> None:
        """Create a store backed by ``path`` using ``master_secret`` for the key.

        Args:
            path: Filesystem path of the JSON store file. It need not exist yet;
                it is created on the first successful :meth:`store`.
            master_secret: The secret used to derive the encryption key. Must be
                non-empty. May be a ``str`` (encoded as UTF-8) or raw ``bytes``.

        Raises:
            CredentialStoreError: If ``master_secret`` is empty.
        """
        secret = _coerce_secret(master_secret)
        if not secret:
            raise CredentialStoreError("master secret must be non-empty")
        self._path = Path(path)
        self._master_secret = secret

    @classmethod
    def from_passphrase(cls, path: Union[str, Path], passphrase: str) -> "CredentialStore":
        """Build a store whose key is derived from the operator's ``passphrase``.

        Args:
            path: Filesystem path of the JSON store file.
            passphrase: The access passphrase. Must be non-empty.

        Returns:
            A :class:`CredentialStore` keyed on ``passphrase``.

        Raises:
            CredentialStoreError: If ``passphrase`` is empty.
        """
        if not passphrase:
            raise CredentialStoreError("passphrase must be non-empty")
        return cls(path, passphrase)

    @classmethod
    def from_keyring(
        cls,
        path: Union[str, Path],
        service_name: str,
        username: str,
    ) -> "CredentialStore":
        """Build a store whose master secret lives in the OS keyring.

        The secret is read from the OS keyring under (``service_name``,
        ``username``); if none exists yet a fresh random secret is generated and
        stored in the keyring so it persists across restarts. This keeps the
        master secret out of the store file and off the command line.

        Args:
            path: Filesystem path of the JSON store file.
            service_name: The keyring service namespace.
            username: The keyring entry username.

        Returns:
            A :class:`CredentialStore` keyed on the keyring-held secret.

        Raises:
            CredentialStoreError: If the ``keyring`` package is not installed or
                the keyring cannot be accessed.
        """
        try:
            import keyring  # type: ignore
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise CredentialStoreError(
                "the 'keyring' package is required for keyring-backed credential storage"
            ) from exc

        try:
            secret = keyring.get_password(service_name, username)
            if not secret:
                secret = base64.urlsafe_b64encode(os.urandom(_KEY_LENGTH)).decode("ascii")
                keyring.set_password(service_name, username, secret)
        except Exception as exc:  # pragma: no cover - environment dependent
            raise CredentialStoreError(f"could not access the OS keyring: {exc}") from exc

        return cls(path, secret)

    # -- reads ---------------------------------------------------------------

    def _load(self) -> Dict[str, object]:
        """Read and parse the on-disk store, or return an empty structure."""
        if not self._path.exists():
            return {"version": _FORMAT_VERSION, "salt": None, "entries": {}}
        with self._path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _fernet_for(self, salt_b64: str) -> Fernet:
        """Build a :class:`Fernet` for the given persisted salt."""
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        return Fernet(_derive_key(self._master_secret, salt))

    def names(self) -> List[str]:
        """Return the names of all stored credentials, sorted."""
        data = self._load()
        entries = data.get("entries", {})
        return sorted(entries.keys())

    def has(self, name: str) -> bool:
        """Return whether a credential named ``name`` is stored."""
        return name in self._load().get("entries", {})

    def retrieve(self, name: str) -> str:
        """Return the decrypted plaintext of the credential named ``name``.

        Args:
            name: The credential name used at :meth:`store` time.

        Returns:
            The original plaintext secret value.

        Raises:
            CredentialNotFoundError: If no credential named ``name`` exists.
            CredentialDecryptionError: If the ciphertext cannot be decrypted
                (wrong master secret or tampered blob).
        """
        data = self._load()
        entries = data.get("entries", {})
        if name not in entries:
            raise CredentialNotFoundError(name)
        salt_b64 = data.get("salt")
        if not salt_b64:
            raise CredentialDecryptionError("store is missing its key-derivation salt")
        fernet = self._fernet_for(salt_b64)
        token = entries[name].encode("ascii")
        try:
            plaintext = fernet.decrypt(token)
        except InvalidToken as exc:
            raise CredentialDecryptionError(
                f"could not decrypt credential '{name}'; wrong passphrase or corrupted store"
            ) from exc
        return plaintext.decode("utf-8")

    # -- writes --------------------------------------------------------------

    def _atomic_write(self, data: Dict[str, object]) -> None:
        """Serialize ``data`` and replace the store file atomically."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(data, indent=2, sort_keys=True)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self._path)

    def store(self, name: str, value: str) -> None:
        """Encrypt ``value`` and persist it under ``name``, no plaintext at rest.

        The value is encrypted in memory *before* anything is written; the store
        file is then replaced atomically, so a failure leaves no plaintext and no
        partially written credential on disk (Req 14.1). An existing credential
        with the same name is overwritten.

        Args:
            name: The credential name (for example ``"tenant_api_key"``).
            value: The plaintext secret to encrypt and persist.

        Raises:
            CredentialStoreError: If ``name`` is empty or ``value`` is not a
                string.
        """
        if not name:
            raise CredentialStoreError("credential name must be non-empty")
        if not isinstance(value, str):
            raise CredentialStoreError("credential value must be a string")

        data = self._load()
        salt_b64 = data.get("salt")
        if not salt_b64:
            salt = os.urandom(_SALT_LENGTH)
            salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
            data["salt"] = salt_b64
        data.setdefault("version", _FORMAT_VERSION)
        entries = data.setdefault("entries", {})

        fernet = self._fernet_for(salt_b64)
        token = fernet.encrypt(value.encode("utf-8")).decode("ascii")
        entries[name] = token

        self._atomic_write(data)

    def delete(self, name: str) -> bool:
        """Remove the credential named ``name``, leaving no plaintext or ciphertext.

        The store file is rewritten without the named entry, so neither a
        plaintext nor an encrypted copy of the deleted credential remains
        (Req 14.5). Deleting a name that is not present is a no-op.

        Args:
            name: The credential name to remove.

        Returns:
            ``True`` if a credential was removed; ``False`` if none existed.
        """
        data = self._load()
        entries = data.get("entries", {})
        if name not in entries:
            return False
        del entries[name]
        data["entries"] = entries
        self._atomic_write(data)
        return True
