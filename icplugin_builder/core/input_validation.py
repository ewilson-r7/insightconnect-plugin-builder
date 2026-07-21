"""Conversation input validation (design Properties 3 and 4; Req 1.1, 1.6).

Before a user's chat message reaches the ``Orchestrator`` it must pass a purely
syntactic gate on the raw text. This module is the single source of truth for
that gate and is a pure function with no I/O, so it can be exhaustively
property-tested:

* **Length acceptance boundary** (Req 1.1, design Property 4) -- the
  ``Conversation_Interface`` accepts input for processing *if and only if* its
  length is between :data:`MIN_INPUT_LENGTH` (1) and :data:`MAX_INPUT_LENGTH`
  (10,000) characters inclusive. :func:`is_acceptable_length` is the length-only
  predicate expressing exactly that boundary.
* **Empty/whitespace rejection** (Req 1.6, design Property 3) -- input that is
  empty or contains only whitespace is rejected, the message indicates that a
  non-empty description is required, and the current draft is left unchanged.
  :func:`is_blank` is the emptiness predicate.

:func:`validate_conversation_input` combines both checks into one decision and
returns a structured :class:`InputValidationResult`. The result carries no draft
state and performs no mutation; a rejected result is the caller's signal to
leave the draft untouched (Req 1.6), which keeps the "draft unchanged" guarantee
a property of *not acting* rather than of undoing a partial action.

A "character" is a Python string code point, so :func:`len` is the character
count used throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

__all__ = [
    "MIN_INPUT_LENGTH",
    "MAX_INPUT_LENGTH",
    "InputRejectionReason",
    "InputValidationResult",
    "is_acceptable_length",
    "is_blank",
    "validate_conversation_input",
]

#: Minimum accepted input length, inclusive (Req 1.1).
MIN_INPUT_LENGTH = 1

#: Maximum accepted input length, inclusive (Req 1.1).
MAX_INPUT_LENGTH = 10_000

#: Message returned when input is empty or whitespace-only (Req 1.6).
_EMPTY_MESSAGE = "A non-empty description is required."

#: Message returned when input exceeds the maximum accepted length (Req 1.1).
_TOO_LONG_MESSAGE = f"Description must be at most {MAX_INPUT_LENGTH} characters."


class InputRejectionReason(Enum):
    """Why a submitted conversation input was rejected."""

    #: Input is empty or contains only whitespace characters (Req 1.6).
    EMPTY_OR_WHITESPACE = "empty_or_whitespace"
    #: Input length exceeds :data:`MAX_INPUT_LENGTH` characters (Req 1.1).
    TOO_LONG = "too_long"


@dataclass(frozen=True)
class InputValidationResult:
    """The outcome of validating a raw conversation input string.

    Attributes:
        accepted: ``True`` when the input passes every gate and may be
            forwarded to the ``Orchestrator`` for processing; ``False`` when it
            must be rejected, in which case the caller leaves the draft
            unchanged (Req 1.6).
        reason: The specific rejection reason when ``accepted`` is ``False``;
            ``None`` when the input is accepted.
        message: A user-facing message describing the rejection when
            ``accepted`` is ``False``; ``None`` when the input is accepted.
    """

    accepted: bool
    reason: Optional[InputRejectionReason] = None
    message: Optional[str] = None


def is_acceptable_length(text: str) -> bool:
    """Return ``True`` iff ``text``'s length is within the accepted bounds.

    This is the length-only gate of Req 1.1 (design Property 4): the input is
    accepted for processing *if and only if* its character count is in the
    inclusive range ``1..10,000``.

    Args:
        text: The raw input string to measure.

    Returns:
        ``True`` when ``MIN_INPUT_LENGTH <= len(text) <= MAX_INPUT_LENGTH``;
        ``False`` otherwise.
    """
    return MIN_INPUT_LENGTH <= len(text) <= MAX_INPUT_LENGTH


def is_blank(text: str) -> bool:
    """Return ``True`` iff ``text`` is empty or contains only whitespace.

    This is the emptiness gate of Req 1.6 (design Property 3). A string is blank
    when it has no characters or when every character is whitespace, so
    ``str.strip`` reduces it to the empty string.

    Args:
        text: The raw input string to test.

    Returns:
        ``True`` when ``text`` is empty or whitespace-only; ``False`` otherwise.
    """
    return text.strip() == ""


def validate_conversation_input(text: str) -> InputValidationResult:
    """Validate a raw conversation input string against the submission gates.

    The input is accepted for processing only when it is non-blank (Req 1.6)
    *and* its length is within the accepted bounds (Req 1.1). Rejection is
    reported with a specific reason and a user-facing message; the caller leaves
    the current draft unchanged on any rejection (Req 1.6).

    Args:
        text: The raw input string submitted by the user.

    Returns:
        An :class:`InputValidationResult`. When accepted, ``reason`` and
        ``message`` are ``None``. When rejected, ``accepted`` is ``False`` with
        :attr:`InputRejectionReason.EMPTY_OR_WHITESPACE` for empty/whitespace
        input and :attr:`InputRejectionReason.TOO_LONG` for input longer than
        :data:`MAX_INPUT_LENGTH` characters.
    """
    if is_blank(text):
        return InputValidationResult(
            accepted=False,
            reason=InputRejectionReason.EMPTY_OR_WHITESPACE,
            message=_EMPTY_MESSAGE,
        )
    if not is_acceptable_length(text):
        return InputValidationResult(
            accepted=False,
            reason=InputRejectionReason.TOO_LONG,
            message=_TOO_LONG_MESSAGE,
        )
    return InputValidationResult(accepted=True)
