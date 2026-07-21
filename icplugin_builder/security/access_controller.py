"""Optional local access guard (design Access_Controller; Req 17, 18.1, 18.5).

The Plugin_Builder runs for a single local operator, so instead of a multi-user
account system it offers an *optional* passphrase guard. When access protection
is enabled in configuration, the operator must supply the configured passphrase
before any protected function runs; a wrong passphrase denies access and
executes nothing (Req 17.1, 17.2). When protection is disabled, access is
granted without prompting (Req 17.3). Every authentication outcome is recorded
to the append-only :class:`~icplugin_builder.persistence.audit_log.AuditLog`
(Req 18.1, 18.5).

The passphrase itself is never stored in plaintext: configuration holds only a
salted ``scrypt`` hash (design Config File ``access.passphrase_hash``). This
module owns the hash format and verification, using the ``cryptography``
library's ``scrypt`` KDF -- a strong, memory-hard function -- and a
constant-time comparison so verification does not leak timing information.

Design intent for downstream tasks:

* Task 21.4 / Property 32 (*wrong passphrase denies access and runs nothing*)
  builds on :meth:`AccessController.guard`, which authenticates first and only
  invokes the protected callable when access is granted.
* Task 21.5 (*disabled access grants without a prompt*) builds on
  :meth:`AccessController.authenticate` returning a granted session with no
  passphrase required when protection is disabled.

Binding the network interface to a configurable address that defaults to
loopback is handled by :class:`~icplugin_builder.api.config.NetworkConfig`
(Req 17.4); :meth:`AccessController.bind_address` surfaces that decision here so
callers wiring up the server have a single access-related entry point.
"""

from __future__ import annotations

import base64
import hmac
import os
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ..api.config import AccessConfig, NetworkConfig
from ..persistence.audit_log import AuditLog

__all__ = [
    "AccessError",
    "AccessDenied",
    "AccessConfigurationError",
    "Session",
    "AccessController",
    "hash_passphrase",
    "verify_passphrase",
]

T = TypeVar("T")

#: Identifier of the hashing scheme embedded in a stored passphrase hash so the
#: format is self-describing and future schemes can be distinguished.
_SCHEME = "scrypt"

#: ``scrypt`` cost parameters used when hashing a passphrase. ``n`` must be a
#: power of two; these match the interactive-login parameters used by the
#: credential store and are strong for a single local operator while remaining
#: fast enough to verify on each access.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

#: Length of the derived hash in bytes.
_KEY_LENGTH = 32

#: Length of the random per-hash salt in bytes.
_SALT_LENGTH = 16

#: Default identity recorded in the audit log for the single local operator.
DEFAULT_USER_IDENTITY = "local-operator"


class AccessError(Exception):
    """Base class for access-guard failures."""


class AccessDenied(AccessError):
    """Raised when a protected function is invoked without valid access.

    Raised by :meth:`AccessController.guard` when authentication does not grant
    access, guaranteeing the protected callable is never invoked (Req 17.2).
    """


class AccessConfigurationError(AccessError):
    """Raised when access protection is enabled but misconfigured.

    For example, protection is enabled but no passphrase hash is configured, so
    no passphrase could ever match. Loading configuration via
    :func:`~icplugin_builder.api.config.load_config` already rejects this case;
    this guards direct construction from a hand-built :class:`AccessConfig`.
    """


def _b64encode(raw: bytes) -> str:
    """Return the urlsafe-base64 text for ``raw`` bytes without padding issues."""
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64decode(text: str) -> bytes:
    """Decode urlsafe-base64 ``text`` back to bytes."""
    return base64.urlsafe_b64decode(text.encode("ascii"))


def hash_passphrase(
    passphrase: str,
    *,
    n: int = _SCRYPT_N,
    r: int = _SCRYPT_R,
    p: int = _SCRYPT_P,
) -> str:
    """Return a self-describing salted ``scrypt`` hash of ``passphrase``.

    The returned string encodes the scheme, cost parameters, salt, and derived
    key so that :func:`verify_passphrase` can re-derive the hash without any
    external state. A fresh random salt is generated on every call, so hashing
    the same passphrase twice yields different strings that both verify.

    The format is ``scrypt$<n>$<r>$<p>$<salt_b64>$<derived_b64>`` using
    urlsafe-base64 for the salt and derived key.

    Args:
        passphrase: The plaintext passphrase to hash. Must be non-empty.
        n: ``scrypt`` CPU/memory cost (a power of two).
        r: ``scrypt`` block size.
        p: ``scrypt`` parallelization.

    Returns:
        The encoded passphrase hash, suitable for ``access.passphrase_hash``.

    Raises:
        AccessConfigurationError: If ``passphrase`` is empty.
    """
    if not passphrase:
        raise AccessConfigurationError("passphrase must be non-empty")
    salt = os.urandom(_SALT_LENGTH)
    kdf = Scrypt(salt=salt, length=_KEY_LENGTH, n=n, r=r, p=p)
    derived = kdf.derive(passphrase.encode("utf-8"))
    return "$".join([_SCHEME, str(n), str(r), str(p), _b64encode(salt), _b64encode(derived)])


def verify_passphrase(passphrase: str, stored_hash: str) -> bool:
    """Return whether ``passphrase`` matches the encoded ``stored_hash``.

    The salt and cost parameters are read from ``stored_hash``, the candidate
    passphrase is re-derived with the same parameters, and the two derived keys
    are compared in constant time (:func:`hmac.compare_digest`) so verification
    does not leak how many leading bytes matched.

    A malformed ``stored_hash`` or an empty ``passphrase`` returns ``False``
    rather than raising, so a corrupted configuration denies access instead of
    crashing the guard.

    Args:
        passphrase: The candidate plaintext passphrase.
        stored_hash: A hash previously produced by :func:`hash_passphrase`.

    Returns:
        ``True`` if the passphrase matches; ``False`` otherwise.
    """
    if not passphrase or not stored_hash:
        return False
    try:
        scheme, n_str, r_str, p_str, salt_b64, derived_b64 = stored_hash.split("$")
        if scheme != _SCHEME:
            return False
        n, r, p = int(n_str), int(r_str), int(p_str)
        salt = _b64decode(salt_b64)
        expected = _b64decode(derived_b64)
    except (ValueError, TypeError):
        return False

    kdf = Scrypt(salt=salt, length=len(expected), n=n, r=r, p=p)
    try:
        candidate = kdf.derive(passphrase.encode("utf-8"))
    except Exception:  # pragma: no cover - defensive: bad parameters deny access
        return False
    return hmac.compare_digest(candidate, expected)


@dataclass(frozen=True)
class Session:
    """The outcome of an authentication attempt.

    Attributes:
        granted: ``True`` when access is granted (protection disabled, or an
            enabled protection matched the passphrase).
        protection_enabled: Whether access protection was enabled for this
            attempt.
        user_identity: The identity recorded for the attempt.
        reason: A short description of why access was denied; ``None`` when
            granted.
    """

    granted: bool
    protection_enabled: bool
    user_identity: str
    reason: Optional[str] = None


class AccessController:
    """Guards protected functions behind an optional local passphrase.

    The controller reads its behaviour from an
    :class:`~icplugin_builder.api.config.AccessConfig`: when
    ``protection_enabled`` is ``False`` it grants access without prompting
    (Req 17.3); when ``True`` it requires a passphrase matching the configured
    ``passphrase_hash`` (Req 17.1) and denies otherwise (Req 17.2). Each attempt
    is recorded to the audit log when one is supplied (Req 18.1, 18.5).
    """

    def __init__(
        self,
        access_config: AccessConfig,
        *,
        audit_log: Optional[AuditLog] = None,
        network_config: Optional[NetworkConfig] = None,
        user_identity: str = DEFAULT_USER_IDENTITY,
    ) -> None:
        """Create a controller for the given access configuration.

        Args:
            access_config: The access-protection settings.
            audit_log: Optional audit log; when provided, every authentication
                success/failure is recorded (Req 18.1, 18.5).
            network_config: Optional network settings used only to surface the
                configured bind address (Req 17.4).
            user_identity: The identity recorded for the single local operator.

        Raises:
            AccessConfigurationError: If protection is enabled but no passphrase
                hash is configured (no passphrase could ever match).
        """
        if access_config.protection_enabled and not access_config.passphrase_hash:
            raise AccessConfigurationError("access protection is enabled but no passphrase hash is configured")
        self._config = access_config
        self._audit_log = audit_log
        self._network_config = network_config
        self._user_identity = user_identity

    @property
    def protection_enabled(self) -> bool:
        """Whether the passphrase guard is active (Req 17.1, 17.3)."""
        return self._config.protection_enabled

    @property
    def bind_address(self) -> str:
        """The configured network bind address, defaulting to loopback (Req 17.4)."""
        if self._network_config is not None:
            return self._network_config.bind_address
        return NetworkConfig().bind_address

    def authenticate(self, passphrase: Optional[str] = None) -> Session:
        """Attempt to authenticate and return the resulting :class:`Session`.

        When protection is disabled, access is granted immediately without a
        passphrase and no prompt is required (Req 17.3). When protection is
        enabled, ``passphrase`` is verified against the configured hash: a match
        grants access (Req 17.1) and a mismatch (or a missing passphrase) denies
        it (Req 17.2). The outcome is recorded to the audit log when one was
        supplied (Req 18.1, 18.5).

        Args:
            passphrase: The candidate passphrase; ignored when protection is
                disabled.

        Returns:
            A :class:`Session` whose :attr:`~Session.granted` flag reports the
            outcome. This method never raises for a wrong passphrase; use
            :meth:`guard` to enforce that a protected function runs only on a
            granted session.
        """
        if not self._config.protection_enabled:
            # Access protection disabled: grant without prompting (Req 17.3).
            # No authentication occurred, so nothing is recorded.
            return Session(
                granted=True,
                protection_enabled=False,
                user_identity=self._user_identity,
            )

        if passphrase and verify_passphrase(passphrase, self._config.passphrase_hash or ""):
            self._record_success()
            return Session(
                granted=True,
                protection_enabled=True,
                user_identity=self._user_identity,
            )

        reason = "no passphrase provided" if not passphrase else "incorrect passphrase"
        self._record_failure(reason)
        return Session(
            granted=False,
            protection_enabled=True,
            user_identity=self._user_identity,
            reason=reason,
        )

    def guard(
        self,
        passphrase: Optional[str],
        func: Callable[..., T],
        /,
        *args: object,
        **kwargs: object,
    ) -> T:
        """Run ``func`` only if ``passphrase`` grants access, else deny.

        This is the single choke point that guarantees a protected function is
        never executed without access (Req 17.2, design Property 32). It
        authenticates first; on a granted session it invokes ``func`` and
        returns its result; on a denied session it raises :class:`AccessDenied`
        and never touches ``func``.

        Args:
            passphrase: The candidate passphrase (ignored when protection is
                disabled).
            func: The protected callable to run only when access is granted.
            *args: Positional arguments forwarded to ``func``.
            **kwargs: Keyword arguments forwarded to ``func``.

        Returns:
            Whatever ``func`` returns.

        Raises:
            AccessDenied: If authentication does not grant access; ``func`` is
                not called.
        """
        session = self.authenticate(passphrase)
        if not session.granted:
            raise AccessDenied(session.reason or "access denied")
        return func(*args, **kwargs)

    def _record_success(self) -> None:
        """Record a successful authentication to the audit log (Req 18.1)."""
        if self._audit_log is not None:
            self._audit_log.record_auth_success(self._user_identity)

    def _record_failure(self, reason: str) -> None:
        """Record a failed authentication with its reason (Req 18.5)."""
        if self._audit_log is not None:
            self._audit_log.record_auth_failure(self._user_identity, reason)
