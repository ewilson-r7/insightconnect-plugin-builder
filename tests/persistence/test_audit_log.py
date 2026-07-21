"""Unit tests for the append-only, hash-chained audit log (task 10.1; Req 18).

These cover specific examples and edge cases: recording each auditable event
with its required fields and a UTC timestamp, secret masking on credential
events, the hash chain linking records, append-only persistence across
reopen, tamper detection for altered/removed records, and the >=90-day
retention guarantee. The universal completeness/append-only property
(Property 33) and tamper-detection property (Property 34) are covered
separately by the property tests (tasks 10.2, 10.3).
"""

from datetime import datetime, timedelta, timezone

import pytest

from icplugin_builder.core.masking import MASK_PLACEHOLDER
from icplugin_builder.persistence.audit_log import (
    GENESIS_HASH,
    RETENTION_MIN_DAYS,
    AuditEvent,
    AuditLog,
    AuditLogError,
)


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "audit.log"


class TestRecordEvents:
    def test_auth_success_records_identity_and_timestamp(self, log_path):
        log = AuditLog(log_path)
        record = log.record_auth_success("alice")
        assert record.event == AuditEvent.AUTH_SUCCESS
        assert record.user_identity == "alice"
        # UTC timestamp with at least second precision (Req 18.1).
        parsed = datetime.fromisoformat(record.utc)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)

    def test_auth_failure_records_identity_and_reason(self, log_path):
        log = AuditLog(log_path)
        record = log.record_auth_failure("mallory", "bad passphrase")
        assert record.event == AuditEvent.AUTH_FAILURE
        assert record.user_identity == "mallory"
        assert record.reason == "bad passphrase"

    def test_build_records_name_and_version(self, log_path):
        log = AuditLog(log_path)
        record = log.record_build("okta", "1.2.3")
        assert record.event == AuditEvent.BUILD
        assert record.plugin_name == "okta"
        assert record.version == "1.2.3"

    def test_export_records_name_version_target(self, log_path):
        log = AuditLog(log_path)
        record = log.record_export("okta", "1.2.3", target="https://us.api.insight.rapid7.com")
        assert record.event == AuditEvent.EXPORT
        assert record.plugin_name == "okta"
        assert record.version == "1.2.3"
        assert record.target == "https://us.api.insight.rapid7.com"

    def test_first_record_links_to_genesis(self, log_path):
        log = AuditLog(log_path)
        record = log.record_build("okta", "1.0.0")
        assert record.seq == 1
        assert record.prev_hash == GENESIS_HASH

    def test_explicit_timestamp_is_honored(self, log_path):
        log = AuditLog(log_path)
        stamp = "2024-01-02T03:04:05+00:00"
        record = log.record_build("okta", "1.0.0", utc=stamp)
        assert record.utc == stamp

    def test_unknown_event_is_rejected(self, log_path):
        log = AuditLog(log_path)
        with pytest.raises(AuditLogError):
            log._append("not_a_real_event")


class TestSecretMasking:
    def test_credential_store_masks_secret(self, log_path):
        log = AuditLog(log_path)
        secret = "super-secret-api-key"
        record = log.record_credential_store(secret)
        assert record.event == AuditEvent.CREDENTIAL_STORE
        assert record.masked_secret == MASK_PLACEHOLDER
        # No character of the raw secret appears anywhere in the record (Req 18.3).
        serialized = str(record.to_dict())
        assert secret not in serialized

    def test_credential_use_masks_secret_and_records_target(self, log_path):
        log = AuditLog(log_path)
        record = log.record_credential_use("token-value", target="tenant-upload")
        assert record.masked_secret == MASK_PLACEHOLDER
        assert record.target == "tenant-upload"

    def test_raw_secret_never_written_to_disk(self, log_path):
        log = AuditLog(log_path)
        secret = "plaintext-should-never-persist"
        log.record_credential_store(secret)
        on_disk = log_path.read_text(encoding="utf-8")
        assert secret not in on_disk
        assert MASK_PLACEHOLDER in on_disk


class TestHashChain:
    def test_each_record_chains_over_previous_hash(self, log_path):
        log = AuditLog(log_path)
        first = log.record_build("p", "1.0.0")
        second = log.record_export("p", "1.0.0")
        third = log.record_auth_success("alice")
        assert second.prev_hash == first.hash
        assert third.prev_hash == second.hash

    def test_sequence_numbers_are_monotonic(self, log_path):
        log = AuditLog(log_path)
        seqs = [
            log.record_build("p", "1.0.0").seq,
            log.record_build("p", "1.0.1").seq,
            log.record_build("p", "1.0.2").seq,
        ]
        assert seqs == [1, 2, 3]

    def test_verify_passes_for_intact_log(self, log_path):
        log = AuditLog(log_path)
        log.record_build("p", "1.0.0")
        log.record_export("p", "1.0.0", target="local")
        result = log.verify()
        assert result.valid


class TestAppendOnlyPersistence:
    def test_records_survive_reopen(self, log_path):
        log = AuditLog(log_path)
        log.record_build("p", "1.0.0")
        log.record_export("p", "1.0.0", target="local")

        reopened = AuditLog(log_path)
        records = reopened.records()
        assert [r.event for r in records] == [AuditEvent.BUILD, AuditEvent.EXPORT]

    def test_reopen_continues_the_chain(self, log_path):
        log = AuditLog(log_path)
        first = log.record_build("p", "1.0.0")

        reopened = AuditLog(log_path)
        second = reopened.record_export("p", "1.0.0", target="local")
        assert second.seq == 2
        assert second.prev_hash == first.hash
        assert reopened.verify().valid

    def test_appending_does_not_alter_prior_records(self, log_path):
        log = AuditLog(log_path)
        first = log.record_build("p", "1.0.0")
        before = first.to_dict()
        log.record_export("p", "1.0.0", target="local")
        # The first record on disk is unchanged after a later append.
        after = log.records()[0].to_dict()
        assert after == before


class TestTamperDetection:
    def test_altered_field_is_detected(self, log_path):
        log = AuditLog(log_path)
        log.record_build("okta", "1.0.0")
        log.record_export("okta", "1.0.0", target="local")

        lines = log_path.read_text(encoding="utf-8").splitlines()
        # Tamper: change the plugin name in the first record without fixing its hash.
        lines[0] = lines[0].replace("okta", "evil")
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = log.verify()
        assert not result.valid
        assert result.broken_seq == 1

    def test_deleted_record_is_detected(self, log_path):
        log = AuditLog(log_path)
        log.record_build("p", "1.0.0")
        log.record_export("p", "1.0.0", target="local")
        log.record_auth_success("alice")

        lines = log_path.read_text(encoding="utf-8").splitlines()
        # Remove the middle record; the chain link and sequence break.
        del lines[1]
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = log.verify()
        assert not result.valid

    def test_reordered_records_are_detected(self, log_path):
        log = AuditLog(log_path)
        log.record_build("p", "1.0.0")
        log.record_export("p", "1.0.0", target="local")

        lines = log_path.read_text(encoding="utf-8").splitlines()
        lines.reverse()
        log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert not log.verify().valid


class TestRetention:
    def test_recent_records_are_within_retention(self, log_path):
        log = AuditLog(log_path)
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        recent = (now - timedelta(days=1)).isoformat()
        log.record_build("p", "1.0.0", utc=recent)
        kept = log.retained_records(now=now)
        assert len(kept) == 1

    def test_retention_window_is_at_least_ninety_days(self, log_path):
        log = AuditLog(log_path)
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        # A record exactly at the 90-day boundary is still retained (Req 18.4).
        boundary = (now - timedelta(days=RETENTION_MIN_DAYS)).isoformat()
        log.record_build("p", "1.0.0", utc=boundary)
        kept = log.retained_records(now=now)
        assert len(kept) == 1

    def test_log_never_removes_records_on_its_own(self, log_path):
        log = AuditLog(log_path)
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        log.record_build("p", "1.0.0", utc=old)
        # Even a 400-day-old record is still present; the log never purges.
        assert len(log.records()) == 1
        assert log.verify().valid
