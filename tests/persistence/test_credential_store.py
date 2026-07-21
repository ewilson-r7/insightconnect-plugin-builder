"""Unit tests for the encrypted credential store (task 9.1; Req 14.1, 14.2, 14.5).

These cover specific examples and edge cases: encrypted round trip, no plaintext
at rest, reuse after "restart" (a fresh store instance over the same file),
deletion removing both plaintext and ciphertext, overwrite, and error paths for
missing names and wrong passphrases. The universal "no plaintext substring at
rest" property is covered separately by the property test (task 9.2, Property 27).
"""

import json

import pytest

from icplugin_builder.persistence.credential_store import (
    CredentialDecryptionError,
    CredentialNotFoundError,
    CredentialStore,
    CredentialStoreError,
)

PASSPHRASE = "correct horse battery staple"
API_KEY = "ic-tenant-api-key-abcdef123456"


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / "credentials.json"


class TestStoreRetrieve:
    def test_round_trip_returns_original_value(self, store_path):
        # Req 14.1/14.2: encrypt on store, decrypt back to the original value.
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        store.store("tenant_api_key", API_KEY)
        assert store.retrieve("tenant_api_key") == API_KEY

    def test_no_plaintext_at_rest(self, store_path):
        # Req 14.1: the on-disk blob must not contain the plaintext secret.
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        store.store("tenant_api_key", API_KEY)
        blob = store_path.read_text(encoding="utf-8")
        assert API_KEY not in blob

    def test_store_file_holds_only_salt_and_tokens(self, store_path):
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        store.store("tenant_api_key", API_KEY)
        data = json.loads(store_path.read_text(encoding="utf-8"))
        assert set(data) == {"version", "salt", "entries"}
        assert data["salt"]
        assert "tenant_api_key" in data["entries"]
        # The master secret is never written to disk.
        assert PASSPHRASE not in store_path.read_text(encoding="utf-8")

    def test_overwrite_replaces_value(self, store_path):
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        store.store("k", "first-value")
        store.store("k", "second-value")
        assert store.retrieve("k") == "second-value"

    def test_multiple_credentials_are_independent(self, store_path):
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        store.store("tenant_api_key", API_KEY)
        store.store("git_token", "ghp_secrettoken")
        assert store.retrieve("tenant_api_key") == API_KEY
        assert store.retrieve("git_token") == "ghp_secrettoken"
        assert store.names() == ["git_token", "tenant_api_key"]


class TestReuseAfterRestart:
    def test_new_instance_same_passphrase_decrypts(self, store_path):
        # Req 14.2: a fresh store (simulating a restart) reuses the persisted
        # credential without re-entry.
        CredentialStore.from_passphrase(store_path, PASSPHRASE).store("k", API_KEY)
        reopened = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        assert reopened.retrieve("k") == API_KEY

    def test_wrong_passphrase_cannot_decrypt(self, store_path):
        CredentialStore.from_passphrase(store_path, PASSPHRASE).store("k", API_KEY)
        wrong = CredentialStore.from_passphrase(store_path, "not the passphrase")
        with pytest.raises(CredentialDecryptionError):
            wrong.retrieve("k")


class TestDelete:
    def test_delete_removes_plaintext_and_ciphertext(self, store_path):
        # Req 14.5: after deletion neither plaintext nor ciphertext remains.
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        store.store("k", API_KEY)
        token = json.loads(store_path.read_text(encoding="utf-8"))["entries"]["k"]

        assert store.delete("k") is True

        blob = store_path.read_text(encoding="utf-8")
        assert API_KEY not in blob  # no plaintext
        assert token not in blob  # no ciphertext
        assert not store.has("k")
        with pytest.raises(CredentialNotFoundError):
            store.retrieve("k")

    def test_delete_missing_is_noop(self, store_path):
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        assert store.delete("absent") is False

    def test_delete_leaves_other_credentials_intact(self, store_path):
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        store.store("a", "value-a")
        store.store("b", "value-b")
        store.delete("a")
        assert not store.has("a")
        assert store.retrieve("b") == "value-b"


class TestErrorPaths:
    def test_retrieve_missing_raises(self, store_path):
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        with pytest.raises(CredentialNotFoundError):
            store.retrieve("nope")

    def test_empty_passphrase_rejected(self, store_path):
        with pytest.raises(CredentialStoreError):
            CredentialStore.from_passphrase(store_path, "")

    def test_empty_master_secret_rejected(self, store_path):
        with pytest.raises(CredentialStoreError):
            CredentialStore(store_path, "")

    def test_empty_name_rejected(self, store_path):
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        with pytest.raises(CredentialStoreError):
            store.store("", API_KEY)


class TestEncryptionFailureRejectsStore:
    """Task 9.3 / Req 14.6: a failed encryption must reject the store operation
    and leave nothing partially written to disk."""

    def test_encryption_failure_on_new_store_writes_nothing(self, store_path, monkeypatch):
        # Req 14.6: if encryption fails on a brand-new store, the operation is
        # rejected and no store file (and no partial temp file) is left behind.
        def boom(self, data):  # noqa: ANN001 - test double signature mirrors Fernet.encrypt
            raise RuntimeError("simulated encryption failure")

        monkeypatch.setattr("cryptography.fernet.Fernet.encrypt", boom)

        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        with pytest.raises(RuntimeError):
            store.store("tenant_api_key", API_KEY)

        # Nothing partially written: neither the store file nor its temp sibling exist.
        assert not store_path.exists()
        assert not store_path.with_name(store_path.name + ".tmp").exists()

    def test_encryption_failure_leaves_existing_store_unchanged(self, store_path, monkeypatch):
        # Req 14.6: a failed encryption while adding a second credential must not
        # corrupt or partially update the already-persisted store.
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        store.store("tenant_api_key", API_KEY)
        before = store_path.read_bytes()

        def boom(self, data):  # noqa: ANN001 - test double signature mirrors Fernet.encrypt
            raise RuntimeError("simulated encryption failure")

        monkeypatch.setattr("cryptography.fernet.Fernet.encrypt", boom)

        with pytest.raises(RuntimeError):
            store.store("git_token", "ghp_secrettoken")

        # The on-disk store is byte-for-byte unchanged and holds no partial entry.
        assert store_path.read_bytes() == before
        assert not store_path.with_name(store_path.name + ".tmp").exists()

        # A fresh instance still sees only the original credential.
        reopened = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        assert reopened.retrieve("tenant_api_key") == API_KEY
        assert not reopened.has("git_token")

    def test_atomic_write_failure_leaves_existing_store_unchanged(self, store_path, monkeypatch):
        # Req 14.6: if the durable replace step fails after encryption, the real
        # store file must remain the previous good version, not a partial write.
        store = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        store.store("tenant_api_key", API_KEY)
        before = store_path.read_bytes()

        def boom(src, dst):
            raise OSError("simulated atomic replace failure")

        monkeypatch.setattr("icplugin_builder.persistence.credential_store.os.replace", boom)

        with pytest.raises(OSError):
            store.store("git_token", "ghp_secrettoken")

        # The committed store file is untouched; the new credential was not persisted.
        assert store_path.read_bytes() == before
        reopened = CredentialStore.from_passphrase(store_path, PASSPHRASE)
        assert reopened.retrieve("tenant_api_key") == API_KEY
        assert not reopened.has("git_token")
