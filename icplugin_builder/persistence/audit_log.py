"""Append-only, hash-chained audit log (design Property 33, 34; Req 18).

The ``Audit_Log`` is the durable record of security- and export-relevant
actions. The security design requires it to be:

* **Append-only** -- new records are only ever appended; the log exposes no
  operation that alters or removes a previously written record (Req 18.4,
  18.7). Any *external* mutation (editing or deleting a line in the underlying
  file) is detectable via chain verification (Req 18.7, Property 34).
* **Hash-chained** -- each record carries the hash of the previous record's
  hash together with its own serialized payload, so the whole log forms a
  tamper-evident chain (Property 34).
* **Complete** -- every auditable event (authentication success/failure, build,
  export, credential store/use) appends a record carrying its required fields
  and a UTC timestamp with at least second-level precision (Req 18.1, 18.2,
  18.3, 18.5, 18.6; Property 33).
* **Secret-safe** -- credential events record only a masked secret produced by
  the single boundary masking routine, so no character of a secret value ever
  appears in a record (Req 18.3).
* **Retained** -- records are retained for at least 90 days; the log never
  removes records on its own (Req 18.4).

Records are stored one JSON object per line (JSONL) in an append-only file so
that a crash mid-write cannot corrupt earlier records.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

from icplugin_builder.core.masking import mask_secret

__all__ = [
    "AuditEvent",
    "AuditRecord",
    "VerificationResult",
    "AuditLogError",
    "AuditLog",
    "GENESIS_HASH",
    "RETENTION_MIN_DAYS",
]

#: The ``prev_hash`` of the very first record; a fixed sentinel that anchors the
#: chain so the first record's hash still depends on a known constant.
GENESIS_HASH = "0" * 64

#: The minimum retention guaranteed by the log. Records are never removed by the
#: log itself, so retention is at least this many days (Req 18.4).
RETENTION_MIN_DAYS = 90


class AuditEvent:
    """The closed set of auditable event names (Req 18.1, 18.2, 18.3, 18.5, 18.6)."""

    AUTH_SUCCESS = "auth_success"
    AUTH_FAILURE = "auth_failure"
    BUILD = "build"
    EXPORT = "export"
    CREDENTIAL_STORE = "credential_store"
    CREDENTIAL_USE = "credential_use"

    ALL = frozenset(
        {
            AUTH_SUCCESS,
            AUTH_FAILURE,
            BUILD,
            EXPORT,
            CREDENTIAL_STORE,
            CREDENTIAL_USE,
        }
    )


class AuditLogError(Exception):
    """Raised when a record cannot be appended (for example an unknown event)."""


# Keys that participate in a record's hashed payload, in addition to the
# always-present ``seq``, ``event``, ``utc`` and ``prev_hash``. Optional keys are
# only present when set.
_OPTIONAL_KEYS = (
    "plugin_name",
    "version",
    "user_identity",
    "reason",
    "target",
    "masked_secret",
)


@dataclass(frozen=True)
class AuditRecord:
    """A single audit-log record.

    Attributes:
        seq: Monotonic sequence number starting at 1.
        event: One of :class:`AuditEvent`.
        utc: ISO-8601 UTC timestamp with at least second-level precision.
        prev_hash: The ``hash`` of the preceding record, or :data:`GENESIS_HASH`
            for the first record.
        hash: SHA-256 over ``prev_hash`` concatenated with the canonical
            serialization of this record's payload.
        plugin_name: The affected plugin (build/export events).
        version: The plugin version (build/export events).
        user_identity: The authenticating user (auth events).
        reason: The failure reason (auth-failure events).
        target: The export target (export events).
        masked_secret: The masked secret value (credential events); never a raw
            secret.
    """

    seq: int
    event: str
    utc: str
    prev_hash: str
    hash: str
    plugin_name: Optional[str] = None
    version: Optional[str] = None
    user_identity: Optional[str] = None
    reason: Optional[str] = None
    target: Optional[str] = None
    masked_secret: Optional[str] = None

    def to_dict(self) -> Dict[str, Union[str, int]]:
        """Return the full on-disk mapping for this record (payload + ``hash``)."""
        data = self._payload_dict()
        data["hash"] = self.hash
        return data

    def _payload_dict(self) -> Dict[str, Union[str, int]]:
        """Return the hashed payload (everything except ``hash``).

        Optional fields are included only when set so that records stay compact
        and the payload of a loaded record can be reconstructed exactly by
        dropping the stored ``hash`` key.
        """
        data: Dict[str, Union[str, int]] = {
            "seq": self.seq,
            "event": self.event,
            "utc": self.utc,
            "prev_hash": self.prev_hash,
        }
        for key in _OPTIONAL_KEYS:
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of verifying the integrity of an audit log.

    Attributes:
        valid: ``True`` when the chain is intact and untampered.
        message: A human-readable description of the result.
        broken_seq: The sequence number at which verification failed, or
            ``None`` when the log is valid.
    """

    valid: bool
    message: str
    broken_seq: Optional[int] = None


def _canonical_payload(payload: Dict[str, Union[str, int]]) -> str:
    """Serialize a payload deterministically for hashing.

    Keys are sorted and separators are compact so the same payload always
    produces the same bytes regardless of insertion order.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _compute_hash(prev_hash: str, payload: Dict[str, Union[str, int]]) -> str:
    """Compute the chained SHA-256 hash for ``payload`` following ``prev_hash``."""
    material = prev_hash + _canonical_payload(payload)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (>= second precision)."""
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    """An append-only, hash-chained audit log backed by a JSONL file.

    The log tracks the last record's hash and the next sequence number in
    memory, seeded from the existing file on construction. Every public
    ``record_*`` method appends exactly one record and returns it. There is no
    method to modify or delete an existing record (Req 18.7); external tampering
    is caught by :meth:`verify` (Property 34).
    """

    def __init__(self, path: Union[str, os.PathLike]) -> None:
        """Open (or prepare to create) the audit log at ``path``.

        The parent directory is created if needed. Any existing records are read
        to seed the chain state so appends continue the existing chain.
        """
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash = GENESIS_HASH
        self._next_seq = 1
        self._seed_from_disk()

    @property
    def path(self) -> Path:
        """The filesystem path backing this log."""
        return self._path

    def _seed_from_disk(self) -> None:
        """Initialize chain state (last hash, next seq) from existing records."""
        records = self.records()
        if records:
            last = records[-1]
            self._last_hash = last.hash
            self._next_seq = last.seq + 1

    # -- append helpers -------------------------------------------------------

    def record_auth_success(self, user_identity: str, *, utc: Optional[str] = None) -> AuditRecord:
        """Record a successful authentication (Req 18.1)."""
        return self._append(
            AuditEvent.AUTH_SUCCESS,
            utc=utc,
            user_identity=user_identity,
        )

    def record_auth_failure(self, user_identity: str, reason: str, *, utc: Optional[str] = None) -> AuditRecord:
        """Record a failed authentication attempt with its reason (Req 18.5)."""
        return self._append(
            AuditEvent.AUTH_FAILURE,
            utc=utc,
            user_identity=user_identity,
            reason=reason,
        )

    def record_build(self, plugin_name: str, version: str, *, utc: Optional[str] = None) -> AuditRecord:
        """Record a plugin build with its name and version (Req 18.2)."""
        return self._append(
            AuditEvent.BUILD,
            utc=utc,
            plugin_name=plugin_name,
            version=version,
        )

    def record_export(
        self,
        plugin_name: str,
        version: str,
        *,
        target: Optional[str] = None,
        utc: Optional[str] = None,
    ) -> AuditRecord:
        """Record a plugin export with its name, version, and target (Req 18.6)."""
        return self._append(
            AuditEvent.EXPORT,
            utc=utc,
            plugin_name=plugin_name,
            version=version,
            target=target,
        )

    def record_export_failure(
        self,
        plugin_name: str,
        version: str,
        reason: str,
        *,
        target: Optional[str] = None,
        utc: Optional[str] = None,
    ) -> AuditRecord:
        """Record a failed export attempt with its failure reason (Req 10.3, 18.6).

        A failed or timed-out tenant upload is still an auditable export event,
        so it is recorded as an :data:`AuditEvent.EXPORT` record that additionally
        carries the failure ``reason``. The presence of a ``reason`` on an export
        record distinguishes a failed attempt from a successful export (which has
        no reason), without enlarging the closed set of auditable events.

        Args:
            plugin_name: The plugin whose export was attempted.
            version: The plugin version whose export was attempted.
            reason: A human-readable description of why the attempt failed.
            target: The export target (tenant region base URL) that was tried.
            utc: Optional explicit timestamp; defaults to the current UTC time.
        """
        return self._append(
            AuditEvent.EXPORT,
            utc=utc,
            plugin_name=plugin_name,
            version=version,
            target=target,
            reason=reason,
        )

    def record_credential_store(self, secret: Optional[str], *, utc: Optional[str] = None) -> AuditRecord:
        """Record a credential-store event with the secret masked (Req 18.3).

        The raw ``secret`` is passed through the boundary masking routine so
        that no character of it ever reaches the log.
        """
        return self._append(
            AuditEvent.CREDENTIAL_STORE,
            utc=utc,
            masked_secret=mask_secret(secret),
        )

    def record_credential_use(
        self,
        secret: Optional[str],
        *,
        target: Optional[str] = None,
        utc: Optional[str] = None,
    ) -> AuditRecord:
        """Record a credential-use (upload) event with the secret masked (Req 18.3)."""
        return self._append(
            AuditEvent.CREDENTIAL_USE,
            utc=utc,
            masked_secret=mask_secret(secret),
            target=target,
        )

    def _append(self, event: str, *, utc: Optional[str] = None, **fields: Optional[str]) -> AuditRecord:
        """Build, hash, and append a single record; update chain state.

        Args:
            event: One of :class:`AuditEvent`.
            utc: Optional explicit timestamp (mainly for testing); defaults to
                the current UTC time.
            **fields: Optional record fields (``plugin_name``, ``version``,
                ``user_identity``, ``reason``, ``target``, ``masked_secret``).

        Returns:
            The appended :class:`AuditRecord`.

        Raises:
            AuditLogError: If ``event`` is not a recognized auditable event.
        """
        if event not in AuditEvent.ALL:
            raise AuditLogError(f"unknown audit event: {event!r}")

        seq = self._next_seq
        prev_hash = self._last_hash
        timestamp = utc if utc is not None else _utc_now_iso()

        payload: Dict[str, Union[str, int]] = {
            "seq": seq,
            "event": event,
            "utc": timestamp,
            "prev_hash": prev_hash,
        }
        for key in _OPTIONAL_KEYS:
            value = fields.get(key)
            if value is not None:
                payload[key] = value

        record_hash = _compute_hash(prev_hash, payload)
        record = AuditRecord(
            seq=seq,
            event=event,
            utc=timestamp,
            prev_hash=prev_hash,
            hash=record_hash,
            plugin_name=fields.get("plugin_name"),
            version=fields.get("version"),
            user_identity=fields.get("user_identity"),
            reason=fields.get("reason"),
            target=fields.get("target"),
            masked_secret=fields.get("masked_secret"),
        )

        self._write_line(record)
        self._last_hash = record_hash
        self._next_seq = seq + 1
        return record

    def _write_line(self, record: AuditRecord) -> None:
        """Append a single record as one JSON line and flush it to disk."""
        line = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # -- reading and verification --------------------------------------------

    def records(self) -> List[AuditRecord]:
        """Return every record currently in the log, in file order.

        Returns:
            A list of :class:`AuditRecord`. An empty list when the log file does
            not yet exist or contains no records.
        """
        if not self._path.exists():
            return []
        records: List[AuditRecord] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                records.append(
                    AuditRecord(
                        seq=data["seq"],
                        event=data["event"],
                        utc=data["utc"],
                        prev_hash=data["prev_hash"],
                        hash=data["hash"],
                        plugin_name=data.get("plugin_name"),
                        version=data.get("version"),
                        user_identity=data.get("user_identity"),
                        reason=data.get("reason"),
                        target=data.get("target"),
                        masked_secret=data.get("masked_secret"),
                    )
                )
        return records

    def verify(self) -> VerificationResult:
        """Verify the integrity of the whole chain (Req 18.7; Property 34).

        Recomputes each record's hash from its payload and checks that:

        * sequence numbers start at 1 and increase by exactly 1,
        * each record's ``prev_hash`` equals the previous record's ``hash``
          (the first record links to :data:`GENESIS_HASH`), and
        * each record's stored ``hash`` matches the recomputed hash.

        Any deviation -- an altered field, a removed record, or a reordered
        record -- breaks the chain and is reported.

        Returns:
            A :class:`VerificationResult`. ``valid`` is ``True`` only when the
            entire chain is intact.
        """
        expected_prev = GENESIS_HASH
        expected_seq = 1
        for record in self.records():
            if record.seq != expected_seq:
                return VerificationResult(
                    valid=False,
                    message=f"sequence break: expected seq {expected_seq}, found {record.seq}",
                    broken_seq=record.seq,
                )
            if record.prev_hash != expected_prev:
                return VerificationResult(
                    valid=False,
                    message=f"chain break at seq {record.seq}: prev_hash does not match prior record",
                    broken_seq=record.seq,
                )
            recomputed = _compute_hash(record.prev_hash, record._payload_dict())
            if recomputed != record.hash:
                return VerificationResult(
                    valid=False,
                    message=f"tampered record at seq {record.seq}: hash mismatch",
                    broken_seq=record.seq,
                )
            expected_prev = record.hash
            expected_seq += 1
        return VerificationResult(valid=True, message="audit log intact")

    # -- retention ------------------------------------------------------------

    def retained_records(self, *, now: Optional[datetime] = None) -> List[AuditRecord]:
        """Return records still within the guaranteed retention window.

        The log never removes records on its own, so every record is retained
        for at least :data:`RETENTION_MIN_DAYS` days (Req 18.4). This helper
        simply reports which records fall inside that guaranteed window relative
        to ``now``; it does not delete anything.

        Args:
            now: The reference time (defaults to the current UTC time).

        Returns:
            The records whose timestamp is within :data:`RETENTION_MIN_DAYS`
            days of ``now``.
        """
        reference = now if now is not None else datetime.now(timezone.utc)
        kept: List[AuditRecord] = []
        for record in self.records():
            recorded_at = datetime.fromisoformat(record.utc)
            age_days = (reference - recorded_at).total_seconds() / 86400.0
            if age_days <= RETENTION_MIN_DAYS:
                kept.append(record)
        return kept
