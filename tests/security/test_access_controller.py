"""Unit tests for the Access_Controller (task 21.3; Req 17.1, 17.2, 18.1, 18.5).

These cover the passphrase hash/verify helpers, enabled-protection
authentication (grant on match, deny on mismatch or missing passphrase), the
:meth:`AccessController.guard` choke point that runs a protected function only
on a granted session, audit recording of both outcomes, and misconfiguration
handling.

The universal "wrong passphrase denies access and runs nothing" property
(Property 32) is covered by the property test (task 21.4), and the
disabled-protection grant is covered by its own unit test (task 21.5).
"""

import pytest

from icplugin_builder.api.config import AccessConfig, NetworkConfig
from icplugin_builder.persistence.audit_log import AuditEvent, AuditLog
from icplugin_builder.security.access_controller import (
    DEFAULT_USER_IDENTITY,
    AccessConfigurationError,
    AccessController,
    AccessDenied,
    hash_passphrase,
    verify_passphrase,
)

_PASSPHRASE = "correct horse battery staple"


class TestPassphraseHashing:
    def test_hash_then_verify_round_trip(self):
        stored = hash_passphrase(_PASSPHRASE)
        assert verify_passphrase(_PASSPHRASE, stored) is True

    def test_wrong_passphrase_does_not_verify(self):
        stored = hash_passphrase(_PASSPHRASE)
        assert verify_passphrase("wrong", stored) is False

    def test_hash_is_self_describing_scrypt(self):
        stored = hash_passphrase(_PASSPHRASE)
        assert stored.startswith("scrypt$")
        # scheme, n, r, p, salt, derived
        assert len(stored.split("$")) == 6

    def test_hash_uses_a_fresh_salt_each_call(self):
        first = hash_passphrase(_PASSPHRASE)
        second = hash_passphrase(_PASSPHRASE)
        assert first != second
        # ...but both still verify the same passphrase.
        assert verify_passphrase(_PASSPHRASE, first)
        assert verify_passphrase(_PASSPHRASE, second)

    def test_empty_passphrase_rejected_at_hash_time(self):
        with pytest.raises(AccessConfigurationError):
            hash_passphrase("")

    def test_malformed_hash_returns_false(self):
        assert verify_passphrase(_PASSPHRASE, "not-a-valid-hash") is False
        assert verify_passphrase(_PASSPHRASE, "") is False

    def test_empty_candidate_returns_false(self):
        stored = hash_passphrase(_PASSPHRASE)
        assert verify_passphrase("", stored) is False

    def test_unknown_scheme_returns_false(self):
        # A well-formed 6-part hash with an unrecognized scheme is rejected.
        stored = hash_passphrase(_PASSPHRASE)
        tampered = "bcrypt$" + stored.split("$", 1)[1]
        assert verify_passphrase(_PASSPHRASE, tampered) is False


def _enabled_controller(audit_log=None):
    """An AccessController with protection enabled and a known passphrase."""
    config = AccessConfig(protection_enabled=True, passphrase_hash=hash_passphrase(_PASSPHRASE))
    return AccessController(config, audit_log=audit_log)


class TestAuthenticateEnabled:
    def test_correct_passphrase_grants(self):
        session = _enabled_controller().authenticate(_PASSPHRASE)
        assert session.granted is True
        assert session.protection_enabled is True
        assert session.reason is None

    def test_wrong_passphrase_denies(self):
        session = _enabled_controller().authenticate("nope")
        assert session.granted is False
        assert session.reason == "incorrect passphrase"

    def test_missing_passphrase_denies(self):
        session = _enabled_controller().authenticate(None)
        assert session.granted is False
        assert session.reason == "no passphrase provided"


class TestAuthenticateDisabled:
    """Disabled access protection grants without prompting (task 21.5; Req 17.3)."""

    def _disabled_controller(self, audit_log=None):
        """An AccessController with protection disabled (no passphrase hash needed)."""
        config = AccessConfig(protection_enabled=False)
        return AccessController(config, audit_log=audit_log)

    def test_grants_without_a_passphrase(self):
        session = self._disabled_controller().authenticate()

        assert session.granted is True
        assert session.protection_enabled is False
        assert session.reason is None
        assert session.user_identity == DEFAULT_USER_IDENTITY

    def test_ignores_any_supplied_passphrase(self):
        # A passphrase is never required nor checked when protection is disabled,
        # so even a bogus one still grants access without prompting.
        session = self._disabled_controller().authenticate("irrelevant")

        assert session.granted is True
        assert session.protection_enabled is False
        assert session.reason is None

    def test_guard_runs_protected_function_without_a_prompt(self):
        controller = self._disabled_controller()
        calls = []

        result = controller.guard(None, lambda x: calls.append(x) or "ran", 42)

        assert result == "ran"
        assert calls == [42]

    def test_no_audit_record_written_when_disabled(self, tmp_path):
        # No authentication occurs when protection is disabled, so nothing is
        # recorded to the audit log.
        log = AuditLog(tmp_path / "audit.log")
        self._disabled_controller(audit_log=log).authenticate()

        assert log.records() == []

    def test_protection_enabled_property_is_false(self):
        assert self._disabled_controller().protection_enabled is False


class TestGuard:
    def test_runs_protected_function_when_granted(self):
        controller = _enabled_controller()
        calls = []

        result = controller.guard(_PASSPHRASE, lambda x: calls.append(x) or "ran", 7)

        assert result == "ran"
        assert calls == [7]

    def test_denies_and_never_runs_function_on_wrong_passphrase(self):
        controller = _enabled_controller()
        calls = []

        def protected():
            calls.append("executed")
            return "should not happen"

        with pytest.raises(AccessDenied):
            controller.guard("wrong", protected)

        assert calls == []  # protected function was never invoked (Req 17.2)

    def test_forwards_args_and_kwargs(self):
        controller = _enabled_controller()
        result = controller.guard(_PASSPHRASE, lambda a, b, c=0: a + b + c, 1, 2, c=3)
        assert result == 6


class TestAuditRecording:
    def test_records_success_on_grant(self, tmp_path):
        log = AuditLog(tmp_path / "audit.log")
        _enabled_controller(audit_log=log).authenticate(_PASSPHRASE)

        records = log.records()
        assert len(records) == 1
        assert records[0].event == AuditEvent.AUTH_SUCCESS
        assert records[0].user_identity == DEFAULT_USER_IDENTITY

    def test_records_failure_on_deny(self, tmp_path):
        log = AuditLog(tmp_path / "audit.log")
        _enabled_controller(audit_log=log).authenticate("wrong")

        records = log.records()
        assert len(records) == 1
        assert records[0].event == AuditEvent.AUTH_FAILURE
        assert records[0].reason == "incorrect passphrase"

    def test_audit_chain_stays_valid(self, tmp_path):
        log = AuditLog(tmp_path / "audit.log")
        controller = _enabled_controller(audit_log=log)
        controller.authenticate(_PASSPHRASE)
        controller.authenticate("wrong")
        assert log.verify().valid is True


class TestConfiguration:
    def test_enabled_without_hash_is_rejected(self):
        config = AccessConfig(protection_enabled=True, passphrase_hash=None)
        with pytest.raises(AccessConfigurationError):
            AccessController(config)

    def test_bind_address_defaults_to_loopback(self):
        controller = _enabled_controller()
        assert controller.bind_address == "127.0.0.1"

    def test_bind_address_reflects_network_config(self):
        config = AccessConfig(protection_enabled=True, passphrase_hash=hash_passphrase(_PASSPHRASE))
        controller = AccessController(config, network_config=NetworkConfig(bind_address="0.0.0.0"))
        assert controller.bind_address == "0.0.0.0"

    def test_protection_enabled_property(self):
        assert _enabled_controller().protection_enabled is True
