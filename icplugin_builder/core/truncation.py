"""Error-output truncation utility (design Property 36; Req 19.1, 19.5).

Build- and export-step failures display the full error output emitted by the
failing step (Req 19.1). When that output is large, the UI shows a bounded
prefix while keeping the complete output accessible for retrieval (Req 19.5):

* When the output length exceeds :data:`MAX_DISPLAY_CHARS` (10,000), the
  displayed portion is exactly the first :data:`MAX_DISPLAY_CHARS` characters
  and the full output remains available through the returned handle.
* Otherwise the full output is displayed and the handle equals that same output.

This module is a pure function with no I/O: it partitions a string into a
"what to display" view plus a handle that always yields the complete original
output, so no error text is ever lost.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["MAX_DISPLAY_CHARS", "TruncatedOutput", "truncate_error_output"]

MAX_DISPLAY_CHARS = 10_000


@dataclass(frozen=True)
class TruncatedOutput:
    """A display view of error output plus a handle to the complete output.

    Attributes:
        displayed: The portion shown to the user. When the source output
            exceeds :data:`MAX_DISPLAY_CHARS`, this is exactly its first
            :data:`MAX_DISPLAY_CHARS` characters; otherwise it is the full
            output.
        full: The complete, untruncated error output. This is always
            retained so the full output remains accessible even when
            ``displayed`` is truncated.
        truncated: ``True`` when ``displayed`` is a truncated prefix of
            ``full``; ``False`` when ``displayed`` equals ``full``.
    """

    displayed: str
    full: str
    truncated: bool

    @property
    def omitted_char_count(self) -> int:
        """Number of characters present in ``full`` but not in ``displayed``."""
        return len(self.full) - len(self.displayed)


def truncate_error_output(output: str, limit: int = MAX_DISPLAY_CHARS) -> TruncatedOutput:
    """Partition ``output`` into a bounded display view with full-access handle.

    Args:
        output: The complete error output emitted by a failing build or
            export step.
        limit: The maximum number of characters to display before truncating.
            Defaults to :data:`MAX_DISPLAY_CHARS` (10,000).

    Returns:
        A :class:`TruncatedOutput` whose ``displayed`` value is the first
        ``limit`` characters when ``output`` exceeds ``limit`` (with
        ``truncated`` set to ``True``), or the full ``output`` otherwise (with
        ``truncated`` set to ``False``). ``full`` always carries the complete
        original output so no error text is lost.

    Raises:
        ValueError: If ``limit`` is negative.
    """
    if limit < 0:
        raise ValueError("limit must be non-negative")

    if len(output) > limit:
        return TruncatedOutput(displayed=output[:limit], full=output, truncated=True)
    return TruncatedOutput(displayed=output, full=output, truncated=False)
