"""Property test for wrong-passphrase denial (task 21.4).

# Feature: insightconnect-plugin-builder, Property 32: Wrong passphrase denies access and runs nothing

The unit tests in ``test_access_controller.py`` pin specific examples; this
module covers the universal property across generated inputs: *for any*
passphrase that does not match the configured one while access protection is
enabled, :class:`~icplugin_builder.security.access_controller.AccessController`
denies access (``guard`` raises :class:`AccessDenied`) and never invokes the
protected function, while the correct passphrase both grants access and runs it.

A ``guard`` call is the single choke point that is supposed to guarantee a
protected function only runs on a granted session, so the property is asserted
against ``guard``: the protected callable records every invocation into a list,
and the test checks that the list stays empty on a wrong passphrase and receives
exactly one call on the correct passphrase.

Because access protection is genuinely enabled, the configured passphrase is
stored only as a salted scrypt hash and every attempt re-derives the hash; the
scrypt KDF is intentionally memory-hard, so the per-example work makes a fixed
per-test deadline unreliable and ``deadline=None`` is used rather than lowering
the security parameters the code ships with.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from icplugin_builder.api.config import AccessConfig
from icplugin_builder.security.access_controller import (
    AccessController,
    AccessDenied,
    hash_passphrase,
)

#: Non-empty passphrases drawn from arbitrary printable characters.
_passphrases = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=64,
)


@st.composite
def _correct_and_wrong(draw: st.DrawFn):
    """Generate ``(correct, wrong)`` where ``wrong`` never matches ``correct``.

    ``correct`` is always a non-empty passphrase. ``wrong`` is either ``None``
    (no passphrase supplied) or any string that differs from ``correct`` --
    including the empty string -- so the full space of "does not match the
    configured one" is exercised.
    """
    correct = draw(_passphrases)
    wrong = draw(
        st.one_of(
            st.none(),
            st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), max_size=64).filter(
                lambda candidate: candidate != correct
            ),
        )
    )
    return correct, wrong


def _enabled_controller(correct_passphrase: str) -> AccessController:
    """An AccessController with protection enabled for ``correct_passphrase``."""
    config = AccessConfig(protection_enabled=True, passphrase_hash=hash_passphrase(correct_passphrase))
    return AccessController(config)


@settings(max_examples=100, deadline=None)
@given(data=_correct_and_wrong())
def test_wrong_passphrase_denies_and_runs_nothing(data):
    """Property 32: with protection enabled, a wrong passphrase denies access
    and the protected function is never invoked, while the correct passphrase
    grants access and runs it exactly once.

    **Validates: Requirements 17.1, 17.2**
    """
    correct, wrong = data
    controller = _enabled_controller(correct)

    calls = []

    def protected(marker):
        calls.append(marker)
        return marker

    # A mismatching passphrase denies access: guard raises and never touches the
    # protected callable (Req 17.2).
    try:
        controller.guard(wrong, protected, "denied")
        raised = False
    except AccessDenied:
        raised = True

    assert raised is True
    assert calls == []  # the protected function was never invoked

    # The correct passphrase grants access and runs the protected function
    # exactly once, returning its result (Req 17.1).
    result = controller.guard(correct, protected, "granted")
    assert result == "granted"
    assert calls == ["granted"]
