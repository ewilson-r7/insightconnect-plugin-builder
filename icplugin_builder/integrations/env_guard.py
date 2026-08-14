"""Default-deny environment construction for delegated CLI subprocesses.

Any subprocess this tool spawns inherits the parent environment unless one is
supplied explicitly. That is a real exposure here: the Plugin_Builder decrypts
InsightConnect tenant API keys and private-repository git credentials into its
own process (design "Credential_Store"), and it shells out to a third-party LLM
CLI. Handing that CLI the full ambient environment gives it every secret the
operator happens to have exported -- ``AWS_SECRET_ACCESS_KEY``,
``GITHUB_TOKEN``, ``ANTHROPIC_API_KEY``, and anything else -- none of which it
needs.

This module builds a **default-deny** environment instead: start from nothing,
then re-admit only

* a fixed set of base variable names every CLI needs to function at all
  (:data:`BASE_NAMES`),
* a fixed set of benign prefixes (:data:`BASE_PREFIXES` -- locale, XDG, TLS
  certificate paths), and
* the caller-named authentication prefixes the specific tool legitimately needs
  (e.g. :data:`KIRO_ALLOW_PREFIXES` for the Kiro CLI).

Everything else is dropped. This is a Python port of the ``agent-env-guard``
skill's ``env-guard.sh`` from rapid7/ai-vault (asset version 1.0.0), which does
the same thing via ``env -i`` for shell callers; the allowlists here are kept
deliberately identical to that script so the two cannot drift in what they
consider safe.

The environment is *built*, not applied globally: :func:`guarded_env` returns a
new mapping to pass as the ``env=`` argument of
:func:`asyncio.create_subprocess_exec`. Nothing here mutates
:data:`os.environ`, so concurrent work in the parent process is unaffected.
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, Mapping, Optional, Tuple

__all__ = [
    "BASE_NAMES",
    "BASE_PREFIXES",
    "KIRO_ALLOW_PREFIXES",
    "guarded_env",
    "redacted_names",
]

#: Exact variable names admitted for any delegated CLI. These are what a process
#: needs to locate its own binary, home, temp space, and terminal -- not secrets.
#: Kept identical to ``BASE_NAMES`` in the vault's ``env-guard.sh``.
BASE_NAMES: Tuple[str, ...] = (
    "PATH",
    "HOME",
    "SHELL",
    "USER",
    "LOGNAME",
    "PWD",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "COLUMNS",
    "LINES",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
)

#: Name prefixes admitted for any delegated CLI: locale, X desktop config, and
#: TLS trust-store paths. Kept identical to ``PREFIXES`` in ``env-guard.sh``
#: (before the caller's tool-specific additions).
BASE_PREFIXES: Tuple[str, ...] = ("LC_", "XDG_", "SSL_")

#: The authentication prefixes the Kiro CLI needs. Per the vault's per-tool
#: table: Kiro authenticates through AWS/Amazon identity, so those prefixes are
#: required for it to reach the service at all.
KIRO_ALLOW_PREFIXES: Tuple[str, ...] = ("KIRO_", "AWS_", "AMAZON_", "CODEWHISPERER_")


def guarded_env(
    allow_prefixes: Iterable[str] = (),
    *,
    source: Optional[Mapping[str, str]] = None,
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Build a default-deny environment for a delegated CLI subprocess.

    Args:
        allow_prefixes: the tool-specific authentication name prefixes to admit
            in addition to :data:`BASE_PREFIXES` (e.g.
            :data:`KIRO_ALLOW_PREFIXES`). An empty iterable admits only the base
            names and base prefixes.
        source: the environment to filter; defaults to :data:`os.environ`.
            Injectable so the filtering can be tested without touching the real
            process environment.
        extra: variables to set unconditionally *after* filtering. Use this for
            values this tool chooses itself (for example a ``KIRO_HOME``
            override), not for passing secrets through.

    Returns:
        A new ``dict`` suitable for the ``env=`` argument of
        :func:`asyncio.create_subprocess_exec`. Never the same object as
        ``source``, and never a view onto it.

    Note:
        ``extra`` is applied last and is not filtered, so a caller can always
        set what it explicitly intends to set. It is the one bypass, and it
        exists so the bypass is visible at the call site rather than hidden in
        the allowlists.
    """
    env: Mapping[str, str] = os.environ if source is None else source
    prefixes: Tuple[str, ...] = (*BASE_PREFIXES, *(str(prefix) for prefix in allow_prefixes))

    guarded: Dict[str, str] = {}
    for name, value in env.items():
        if name in BASE_NAMES or name.startswith(prefixes):
            guarded[name] = value

    if extra:
        guarded.update({str(key): str(value) for key, value in extra.items()})

    return guarded


def redacted_names(
    allow_prefixes: Iterable[str] = (),
    *,
    source: Optional[Mapping[str, str]] = None,
) -> Tuple[str, ...]:
    """Return the sorted names that :func:`guarded_env` would drop.

    Intended for logging and diagnostics: it reports which variables were
    withheld from a delegated CLI **by name only**. It never returns a value, so
    logging its output cannot leak a secret (Req 14.4).

    Args:
        allow_prefixes: the same prefixes passed to :func:`guarded_env`.
        source: the environment to inspect; defaults to :data:`os.environ`.

    Returns:
        The dropped variable names, sorted for stable output.
    """
    env: Mapping[str, str] = os.environ if source is None else source
    kept = guarded_env(allow_prefixes, source=env)
    return tuple(sorted(name for name in env if name not in kept))
