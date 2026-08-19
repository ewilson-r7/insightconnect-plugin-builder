"""Reading facts out of the installed plugin toolchain, for tests that must not guess.

One helper, two callers: the export-gate exploration tests and the completeness
cross-check. It exists because "skip when ``insight_plugin`` is not importable" --
the obvious way to write such a guard -- skips in exactly the environment where the
drift it guards against would go unnoticed.

On the reproduction host this repository's own virtualenv has no ``insight_plugin``
at all, while the interpreter the tool *resolves* for the plugin toolchain does. So
the probe tries this interpreter first and then that one, through a subprocess, and
only skips when neither can answer.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, Optional

from icplugin_builder.integrations.build_prep import resolve_target_python

__all__ = ["toolchain_credential_types", "CREDENTIAL_PROBE"]

#: Printed as JSON so the answer crosses the subprocess boundary as data.
CREDENTIAL_PROBE = (
    "import json;"
    "from insight_plugin.features.common.schema_util import SchemaUtil;"
    "print(json.dumps({name: definition for name, definition in SchemaUtil.BASE_TYPES.items()"
    " if name.startswith('credential')}))"
)


def toolchain_credential_types() -> Optional[Dict[str, Any]]:
    """The credential types the installed ``insight-plugin`` defines, or ``None``.

    ``None`` means no interpreter available to the test could answer, which is a
    reason to skip rather than to assert a hardcoded expectation -- a hardcoded set
    is the very thing this cross-check exists to catch.
    """
    try:
        from insight_plugin.features.common.schema_util import SchemaUtil  # noqa: PLC0415 - probe, not a dependency

        return {name: definition for name, definition in SchemaUtil.BASE_TYPES.items() if name.startswith("credential")}
    except ImportError:
        pass

    interpreter = resolve_target_python().executable
    if interpreter is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [interpreter, "-c", CREDENTIAL_PROBE],
            capture_output=True,
            timeout=120.0,
            check=False,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    # The toolchain's own imports emit urllib3 warnings on some hosts, so the JSON
    # is the last parseable line rather than the whole of stdout.
    for line in reversed(completed.stdout.decode("utf-8", errors="replace").strip().splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None
