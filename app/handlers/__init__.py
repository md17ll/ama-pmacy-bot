"""Telegram update handlers."""

# Attach the extra shift-management handlers to the existing shifts router.
from . import shift_tools as _shift_tools  # noqa: F401,E402

# Attach the intelligent schedule workflow to the existing admin router.
from . import smart_schedules as _smart_schedules  # noqa: F401,E402
