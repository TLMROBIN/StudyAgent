from __future__ import annotations

CURRENT_USER_NOTICE_VERSION = "2026-11-v1"


def needs_user_notice_acceptance(accepted_version: str | None) -> bool:
    """Return True when the user has not accepted the current user notice version."""
    return accepted_version != CURRENT_USER_NOTICE_VERSION
