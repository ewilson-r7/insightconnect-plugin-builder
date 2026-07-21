"""Property test for audit-log tamper detection (task 10.3).

# Feature: insightconnect-plugin-builder, Property 34: Audit tamper detection

The :class:`AuditLog` links every record to the previous record through a
SHA-256 hash chain, so any *external* mutation of the underlying JSONL file --
altering a field, deleting a record, or reordering records -- must break the
chain and be reported by :meth:`AuditLog.verify` (Req 18.7). The append API
exposes no way to change a prior record; this property covers the detection
guarantee that backs that append-only promise.

The property builds a log with several records, snapshots that it verifies as
valid, then applies one arbitrary tamper directly to the file on disk and
asserts that a fresh ``verify()`` reports ``valid is False``. It also checks the
untampered log verifies as valid so the detection result is meaningful.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.persistence.audit_log import AuditLog

#: Printable, non-empty free text for identities / names / versions / reasons.
_text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=16,
)

#: The auditable-event methods and how to build their positional arguments.
#: Each entry is (method_name, arg_builder) where arg_builder draws its args.
_EVENT_KINDS = ("auth_success", "auth_failure", "build", "export", "credential_store", "credential_use")


@st.composite
def _event_plans(draw: st.DrawFn) -> List[Dict[str, Any]]:
    """Generate at least two audit-event plans so a *prior* record exists.

    A tamper of "a prior record" is only meaningful when the log holds more than
    one record, so the sequence length starts at two.
    """
    count = draw(st.integers(min_value=2, max_value=8))
    plans: List[Dict[str, Any]] = []
    for _ in range(count):
        kind = draw(st.sampled_from(_EVENT_KINDS))
        if kind == "auth_success":
            plan = {"method": "record_auth_success", "args": (draw(_text),), "kwargs": {}}
        elif kind == "auth_failure":
            plan = {"method": "record_auth_failure", "args": (draw(_text), draw(_text)), "kwargs": {}}
        elif kind == "build":
            plan = {"method": "record_build", "args": (draw(_text), draw(_text)), "kwargs": {}}
        elif kind == "export":
            plan = {"method": "record_export", "args": (draw(_text), draw(_text)), "kwargs": {}}
        elif kind == "credential_store":
            plan = {"method": "record_credential_store", "args": (draw(_text),), "kwargs": {}}
        else:  # credential_use
            plan = {"method": "record_credential_use", "args": (draw(_text),), "kwargs": {}}
        plans.append(plan)
    return plans


def _write_lines(path: Path, records: List[Dict[str, Any]]) -> None:
    """Rewrite the log file from ``records`` using the log's canonical form."""
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            line = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            handle.write(line + "\n")


def _read_lines(path: Path) -> List[Dict[str, Any]]:
    """Read the on-disk records as raw dictionaries, in file order."""
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# The choice of which tamper to apply. Each requires a certain minimum record
# count, which the strategy honours below.
_TAMPER_KINDS = ("alter_field", "delete_record", "reorder_records")


@settings(max_examples=150)
@given(plans=_event_plans(), tamper=st.data())
def test_any_alter_delete_or_reorder_is_detected(plans, tamper):
    """Property 34: an untampered log verifies as valid, and any alter, delete,
    or reorder of a prior record is detected by hash-chain verification.

    **Validates: Requirements 18.7**
    """
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "audit.log"
        log = AuditLog(log_path)

        for plan in plans:
            getattr(log, plan["method"])(*plan["args"], **plan["kwargs"])

        # Baseline: the freshly written chain must verify as valid, otherwise a
        # later "detected" result would be meaningless.
        assert AuditLog(log_path).verify().valid is True

        records = _read_lines(log_path)
        assert len(records) == len(plans)

        # Pick a tamper applicable to this log. Reordering needs a genuine change
        # in order, which requires at least two distinct records.
        kind = tamper.draw(st.sampled_from(_TAMPER_KINDS))

        if kind == "alter_field":
            # Alter one field of a randomly chosen record. The record keeps its
            # stored hash, so recomputation must no longer match.
            index = tamper.draw(st.integers(min_value=0, max_value=len(records) - 1))
            target = records[index]
            # Alterable payload keys (never the stored ``hash`` itself, which
            # would be the trivial case; and not ``seq``, handled by delete).
            alterable = [k for k in target if k not in ("hash", "seq")]
            field = tamper.draw(st.sampled_from(alterable))
            original = target[field]
            new_value = tamper.draw(_text.filter(lambda v: v != original))
            target[field] = new_value
            _write_lines(log_path, records)

        elif kind == "delete_record":
            # Remove a *non-final* record. Deleting a prior record leaves the
            # following records with a stale ``prev_hash`` and a gap in ``seq``,
            # which breaks the chain. (Truncating the tail alone leaves a valid
            # prefix, so it is out of scope for chain-based detection.)
            index = tamper.draw(st.integers(min_value=0, max_value=len(records) - 2))
            del records[index]
            _write_lines(log_path, records)

        else:  # reorder_records
            # Produce a genuinely different ordering of the same records.
            permutation = tamper.draw(
                st.permutations(list(range(len(records)))).filter(lambda p: p != list(range(len(records))))
            )
            reordered = [records[i] for i in permutation]
            _write_lines(log_path, reordered)

        # The tamper must be detected on a fresh read of the file.
        result = AuditLog(log_path).verify()
        assert result.valid is False
        assert result.broken_seq is not None
