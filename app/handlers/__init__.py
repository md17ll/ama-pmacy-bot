"""Telegram update handlers."""

# Install the admin statistics UI overrides before handlers render keyboards.
from app.stats_ui_patch import install as _install_stats_ui  # noqa: E402

_install_stats_ui()

# Attach the extra shift-management handlers to the existing shifts router.
from . import shift_tools as _shift_tools  # noqa: F401,E402
