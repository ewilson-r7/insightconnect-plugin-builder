"""Boundary secret-masking routine (design Property 28; Req 14.3, 14.4, 18.3).

This module is the *single source of truth* for masking secret values. The
security design mandates that a single masking routine replace every character
of any secret with a fixed placeholder before it can reach the UI, logs,
documentation, or a ``.plg`` artifact, so that no code path can accidentally
emit a raw secret.

Two complementary operations are provided:

* :func:`mask_secret` masks a *standalone* secret value (for example when a
  credential field is rendered on its own). Any non-empty secret becomes the
  fixed :data:`MASK_PLACEHOLDER` regardless of its length, so neither the
  characters nor the length of the original value is revealed. An empty or
  ``None`` secret masks to the empty string (the secret is *absent* rather than
  masked).
* :func:`redact_secret` / :func:`redact_secrets` mask *occurrences* of one or
  more secret values embedded inside a larger string (for example a log line or
  an error message that may contain a secret). Every occurrence of a known
  secret substring is replaced with :data:`MASK_PLACEHOLDER`.

In both cases the emitted representation is either absent or fully masked such
that no character of the original secret value appears (Property 28).
"""

from __future__ import annotations

from typing import Iterable, Optional

__all__ = [
    "MASK_PLACEHOLDER",
    "mask_secret",
    "redact_secret",
    "redact_secrets",
]

#: The fixed placeholder emitted in place of a secret value. It is a constant
#: string independent of the secret so that neither the characters nor the
#: length of the original secret is leaked.
MASK_PLACEHOLDER = "********"


def mask_secret(secret: Optional[str]) -> str:
    """Return a fully masked representation of a standalone ``secret`` value.

    Args:
        secret: The secret value to mask, or ``None``/empty when unset.

    Returns:
        The empty string when ``secret`` is ``None`` or empty (the secret is
        absent); otherwise the fixed :data:`MASK_PLACEHOLDER`. No character of
        the original secret value appears in the result.
    """
    if not secret:
        return ""
    return MASK_PLACEHOLDER


def redact_secret(text: str, secret: Optional[str]) -> str:
    """Replace every occurrence of ``secret`` inside ``text`` with the placeholder.

    Args:
        text: Arbitrary text that may contain the secret (for example a log
            line, error message, or serialized document).
        secret: The secret value to redact. Empty or ``None`` secrets are
            ignored because they would otherwise match everywhere.

    Returns:
        ``text`` with every occurrence of ``secret`` replaced by
        :data:`MASK_PLACEHOLDER`. When ``secret`` is empty or ``None``, ``text``
        is returned unchanged.
    """
    if not secret:
        return text
    return text.replace(secret, MASK_PLACEHOLDER)


def redact_secrets(text: str, secrets: Iterable[Optional[str]]) -> str:
    """Redact every secret in ``secrets`` from ``text``.

    Secrets are redacted longest-first so that a secret which is a substring of
    another secret cannot leave a partial fragment of the longer secret behind.

    Args:
        text: Arbitrary text that may contain any of the secrets.
        secrets: An iterable of secret values to redact. Empty or ``None``
            entries are ignored.

    Returns:
        ``text`` with every occurrence of every non-empty secret replaced by
        :data:`MASK_PLACEHOLDER`.
    """
    ordered = sorted((s for s in secrets if s), key=len, reverse=True)
    for secret in ordered:
        text = text.replace(secret, MASK_PLACEHOLDER)
    return text
