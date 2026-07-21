"""Custom vendor-suffix operation (design Property 26; Req 13.1-13.4).

Every plugin produced by this tool carries the literal ``_custom`` suffix on its
vendor field so custom plugins are visually distinct from production Rapid7
plugins. This module is the single source of truth for that transformation.

The operation is a pure function with three guarantees:

* It appends the literal string ``_custom`` to the end of the existing vendor
  value with no separating characters (Req 13.1).
* It is idempotent: a vendor that already ends with an exact, case-sensitive
  ``_custom`` is returned unchanged, so the suffix is never duplicated
  (Req 13.2). Because the result always ends in ``_custom``, applying the
  function a second time yields the same value as applying it once.
* An empty, missing, or null vendor becomes exactly ``_custom`` (Req 13.4).
"""

from __future__ import annotations

from typing import Optional

__all__ = ["CUSTOM_VENDOR_SUFFIX", "apply_custom_vendor_suffix"]

CUSTOM_VENDOR_SUFFIX = "_custom"


def apply_custom_vendor_suffix(vendor: Optional[str]) -> str:
    """Return ``vendor`` with the ``_custom`` suffix applied.

    Args:
        vendor: The current vendor value, or ``None``/empty when unset.

    Returns:
        The vendor value ending in the literal ``_custom``. An empty, missing,
        or null vendor yields ``_custom``; a value already ending in an exact,
        case-sensitive ``_custom`` is returned unchanged; otherwise ``_custom``
        is appended with no separator.
    """
    if not vendor:
        return CUSTOM_VENDOR_SUFFIX
    if vendor.endswith(CUSTOM_VENDOR_SUFFIX):
        return vendor
    return vendor + CUSTOM_VENDOR_SUFFIX
