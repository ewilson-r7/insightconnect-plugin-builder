"""Property test for complete, append-only audit records (task 10.2).

# Feature: insightconnect-plugin-builder, Property 33: Audit records are complete and append-only

The unit tests in ``test_audit_log.py`` pin specific examples; this module
covers the universal property across generated sequences of auditable events.

For every auditable event (authentication success/failure, build, export,
credential store/use), the ``AuditLog`` must:

* append **exactly one** record per event (completeness of count);
* populate that record with the **required fields** for its event kind plus a
  **UTC timestamp with at least second-level precision** (Req 18.1, 18.2, 18.5,
  18.6, and the timestamp/masked-field portions of 18.3); and
* **never alter any previously written record** when a new one is appended
  (append-only form, Req 18.4).

The property drives a randomized sequence of events against a fresh
temp-file-backed log and, after each append, checks the count grew by exactly
one, the new record carries its required fields and a valid UTC timestamp, and
every prior record on disk is byte-for-byte identical to a snapshot taken
before the append.
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.core.masking import mask_secret
from icplugin_builder.persistence.audit_log import AuditEvent, AuditLog

#: Values for identities, names, versions, reasons, and targets. Printable and
#: non-empty so a required field is always meaningfully populated.
_text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=24,
)

#: Secret values fed to credential events; non-empty so the masked field is a
#: present placeholder rather than the empty ("absent") representation.
_secret = st.text(min_size=1, max_size=24)

#: Optional target for export / credential-use events.
_maybe_target = st.one_of(st.none(), _text)

#: The fields each event kind must carry in addition to the always-present
#: ``seq``, ``event``, ``utc``, ``prev_hash``, and ``hash``.
_REQUIRED_FIELDS = {
    AuditEvent.AUTH_SUCCESS: ("user_identity",),
    AuditEvent.AUTH_FAILURE: ("user_identity", "reason"),
    AuditEvent.BUILD: ("plugin_name", "version"),
    AuditEvent.EXPORT: ("plugin_name", "version"),
    AuditEvent.CREDENTIAL_STORE: ("masked_secret",),
    AuditEvent.CREDENTIAL_USE: ("masked_secret",),
}


@st.composite
def _audit_plans(draw: st.DrawFn) -> List[Dict[str, Any]]:
    """Generate a non-empty sequence of audit-event plans.

    Each plan names the ``AuditLog`` method to call, its arguments, the event
    it produces, and the field values that must appear in the resulting record.
    """
    kinds = list(_REQUIRED_FIELDS)
    count = draw(st.integers(min_value=1, max_value=12))
    plans: List[Dict[str, Any]] = []
    for _ in range(count):
        kind = draw(st.sampled_from(kinds))
        if kind == AuditEvent.AUTH_SUCCESS:
            uid = draw(_text)
            plan = {
                "method": "record_auth_success",
                "args": (uid,),
                "kwargs": {},
                "event": kind,
                "expected": {"user_identity": uid},
            }
        elif kind == AuditEvent.AUTH_FAILURE:
            uid, reason = draw(_text), draw(_text)
            plan = {
                "method": "record_auth_failure",
                "args": (uid, reason),
                "kwargs": {},
                "event": kind,
                "expected": {"user_identity": uid, "reason": reason},
            }
        elif kind == AuditEvent.BUILD:
            name, version = draw(_text), draw(_text)
            plan = {
                "method": "record_build",
                "args": (name, version),
                "kwargs": {},
                "event": kind,
                "expected": {"plugin_name": name, "version": version},
            }
        elif kind == AuditEvent.EXPORT:
            name, version, target = draw(_text), draw(_text), draw(_maybe_target)
            expected = {"plugin_name": name, "version": version}
            if target is not None:
                expected["target"] = target
            plan = {
                "method": "record_export",
                "args": (name, version),
                "kwargs": {"target": target},
                "event": kind,
                "expected": expected,
            }
        elif kind == AuditEvent.CREDENTIAL_STORE:
            secret = draw(_secret)
            plan = {
                "method": "record_credential_store",
                "args": (secret,),
                "kwargs": {},
                "event": kind,
                "expected": {"masked_secret": mask_secret(secret)},
            }
        else:  # AuditEvent.CREDENTIAL_USE
            secret, target = draw(_secret), draw(_maybe_target)
            expected = {"masked_secret": mask_secret(secret)}
            if target is not None:
                expected["target"] = target
            plan = {
                "method": "record_credential_use",
                "args": (secret,),
                "kwargs": {"target": target},
                "event": kind,
                "expected": expected,
            }
        plans.append(plan)
    return plans


def _assert_utc_second_precision(utc: str) -> None:
    """Assert ``utc`` is a UTC timestamp with at least second-level precision."""
    parsed = datetime.fromisoformat(utc)
    # Timezone-aware and anchored to UTC (zero offset).
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    # The serialized form carries at least HH:MM:SS (second-level precision).
    time_part = utc.split("T", 1)[1]
    assert re.match(r"\d{2}:\d{2}:\d{2}", time_part) is not None


@settings(max_examples=200)
@given(plans=_audit_plans())
def test_events_append_complete_records_without_altering_prior(plans):
    """Property 33: each event appends exactly one complete record with a UTC
    timestamp, and appending never alters any previously written record.

    **Validates: Requirements 18.1, 18.2, 18.4, 18.5, 18.6**
    """
    with tempfile.TemporaryDirectory() as tmp:
        log = AuditLog(Path(tmp) / "audit.log")

        for index, plan in enumerate(plans):
            before = log.records()
            assert len(before) == index
            before_dicts = [record.to_dict() for record in before]

            method = getattr(log, plan["method"])
            appended = method(*plan["args"], **plan["kwargs"])

            after = log.records()

            # Exactly one record was appended (completeness of count).
            assert len(after) == index + 1

            # Every prior record is byte-for-byte unchanged (append-only).
            assert [record.to_dict() for record in after[:index]] == before_dicts

            # The returned record is the one persisted at the tail.
            newest = after[-1]
            assert newest.to_dict() == appended.to_dict()

            # Always-present fields are correct for this event.
            assert newest.seq == index + 1
            assert newest.event == plan["event"]
            _assert_utc_second_precision(newest.utc)

            # Event-specific required fields are populated as recorded.
            for field in _REQUIRED_FIELDS[plan["event"]]:
                assert getattr(newest, field) is not None
            for field, value in plan["expected"].items():
                assert getattr(newest, field) == value
