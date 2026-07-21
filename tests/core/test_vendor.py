"""Unit tests for the custom vendor-suffix operation (task 3.1; Req 13.1-13.4).

These cover specific examples and edge cases: plain append, idempotency for
values already ending in exact case-sensitive ``_custom``, and the
empty/missing/null vendor case. The universal idempotency property is covered
separately by the property test (task 3.2, Property 26).
"""

import pytest

from icplugin_builder.core.vendor import (
    CUSTOM_VENDOR_SUFFIX,
    apply_custom_vendor_suffix,
)


class TestApplyCustomVendorSuffix:
    def test_appends_suffix_with_no_separator(self):
        # Req 13.1: literal "_custom" appended with no separating characters.
        assert apply_custom_vendor_suffix("rapid7") == "rapid7_custom"

    def test_idempotent_when_already_suffixed(self):
        # Req 13.2: exact suffix present -> unchanged (no duplication).
        assert apply_custom_vendor_suffix("rapid7_custom") == "rapid7_custom"

    def test_applying_twice_equals_once(self):
        once = apply_custom_vendor_suffix("acme")
        twice = apply_custom_vendor_suffix(once)
        assert twice == once == "acme_custom"

    @pytest.mark.parametrize("vendor", ["", None])
    def test_empty_or_null_becomes_suffix(self, vendor):
        # Req 13.4: empty/missing/null vendor becomes exactly "_custom".
        assert apply_custom_vendor_suffix(vendor) == CUSTOM_VENDOR_SUFFIX

    def test_bare_suffix_unchanged(self):
        assert apply_custom_vendor_suffix("_custom") == "_custom"

    def test_case_sensitive_exact_match_only(self):
        # Req 13.2: match is a case-sensitive exact "_custom"; other casings append.
        assert apply_custom_vendor_suffix("acme_Custom") == "acme_Custom_custom"
        assert apply_custom_vendor_suffix("acme_CUSTOM") == "acme_CUSTOM_custom"

    def test_result_always_ends_with_suffix(self):
        for vendor in ["rapid7", "acme_custom", "", None, "x_Custom"]:
            assert apply_custom_vendor_suffix(vendor).endswith(CUSTOM_VENDOR_SUFFIX)

    def test_suffix_as_substring_not_at_end_still_appends(self):
        # "_custom" appearing mid-string is not a suffix, so append.
        assert apply_custom_vendor_suffix("_custom_vendor") == "_custom_vendor_custom"
