"""Security layer.

Optional local access guard (passphrase-based) for the single local operator.
"""

from icplugin_builder.security.access_controller import (
    AccessConfigurationError,
    AccessController,
    AccessDenied,
    AccessError,
    Session,
    hash_passphrase,
    verify_passphrase,
)

__all__ = [
    "AccessConfigurationError",
    "AccessController",
    "AccessDenied",
    "AccessError",
    "Session",
    "hash_passphrase",
    "verify_passphrase",
]
