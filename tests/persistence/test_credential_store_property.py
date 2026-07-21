"""Property-based test for the encrypted credential store (task 9.2; Property 27).

This module implements exactly one design property:

    Property 27: Credential persistence round trip with no plaintext at rest --
    the on-disk blob contains no plaintext substring of the stored secret, a
    decrypt returns the original value, a fresh store instance over the same
    file (simulating a restart) still decrypts, and deletion leaves neither the
    plaintext nor the ciphertext behind.

**Validates: Requirements 14.1, 14.2, 14.5**

The universal guarantee is exercised across many generated secret values and
names via :func:`CredentialStore.from_passphrase` backed by a temp-file store.
Specific examples and error paths are covered separately by the unit tests in
``test_credential_store.py``.
"""

import json
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.persistence.credential_store import CredentialStore

# Realistic, high-entropy secret values. A minimum length of 16 keeps generated
# secrets well clear of the store's small structural tokens (``version``,
# ``salt``, ``entries``) and their quoted forms, so a match against the on-disk
# blob can only ever mean a genuine plaintext leak rather than a coincidental
# collision with the JSON scaffolding.
secret_values = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=0x10FFFF, blacklist_categories=("Cs",)),
    min_size=16,
    max_size=256,
)

# Credential names: non-empty snake_case-style identifiers (letters, digits,
# underscores). Names are legitimately stored in plaintext, so they are kept
# shorter than the minimum secret length and thus can never themselves be a
# secret substring.
credential_names = st.from_regex(r"[a-z][a-z0-9_]{0,20}", fullmatch=True)

# Access passphrases used to derive the encryption key. Any non-empty string is
# accepted by ``from_passphrase``.
passphrases = st.text(min_size=1, max_size=64)


# Feature: insightconnect-plugin-builder, Property 27: Credential persistence round trip with no plaintext at rest
@settings(max_examples=100, deadline=None)
@given(name=credential_names, secret=secret_values, passphrase=passphrases)
def test_credential_round_trip_no_plaintext_at_rest(name, secret, passphrase):
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "credentials.json"

        # Store the secret through a passphrase-derived key.
        store = CredentialStore.from_passphrase(path, passphrase)
        store.store(name, secret)

        # Req 14.1: nothing on disk contains the plaintext secret.
        blob = path.read_text(encoding="utf-8")
        assert secret not in blob

        # Req 14.1/14.2: decrypt returns the original value.
        assert store.retrieve(name) == secret

        # Req 14.2: a fresh instance over the same file (a "restart") re-derives
        # the key and decrypts the persisted credential without re-entry.
        reopened = CredentialStore.from_passphrase(path, passphrase)
        assert reopened.retrieve(name) == secret

        # Capture the ciphertext token so deletion can be checked against it.
        token = json.loads(path.read_text(encoding="utf-8"))["entries"][name]
        assert token in path.read_text(encoding="utf-8")

        # Req 14.5: deletion removes both the plaintext and the ciphertext.
        assert reopened.delete(name) is True
        after = path.read_text(encoding="utf-8")
        assert secret not in after
        assert token not in after
        assert not reopened.has(name)
