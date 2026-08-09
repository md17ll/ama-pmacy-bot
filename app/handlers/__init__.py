"""Telegram update handlers."""

# Attach the extra shift-management handlers to the existing shifts router.
from . import shift_tools as _shift_tools  # noqa: F401,E402

# Merge the photographed Friday history into the smart scheduler before the
# admin handlers import the scheduling integration functions.
from app.services import smart_schedule_history_patch as _smart_schedule_history_patch  # noqa: F401,E402

# Replace random smart-schedule selection with deterministic fairness rules.
from app.services import smart_schedule_fair_patch as _smart_schedule_fair_patch  # noqa: F401,E402

# Preserve original smart choices so manual pharmacy edits can be reverted.
from app.services import smart_schedule_edit_patch as _smart_schedule_edit_patch  # noqa: F401,E402

# Guard batch-bearing smart callbacks before the workflow handlers are registered.
from . import smart_schedule_guard as _smart_schedule_guard  # noqa: F401,E402

# Attach the intelligent schedule workflow to the existing admin router.
from . import smart_schedules as _smart_schedules  # noqa: F401,E402

# Simplify only the smart-schedule admin UI without changing scheduling logic.
from . import smart_schedule_ui as _smart_schedule_ui  # noqa: F401,E402

# Add the cycle-aware photographed Friday ledger editor.
from . import smart_schedule_friday_ui as _smart_schedule_friday_ui  # noqa: F401,E402

# Guard the new smart-revert callback before registering its writer handler.
from . import smart_schedule_template_guard as _smart_schedule_template_guard  # noqa: F401,E402

# Use the approved Word template and transparent pharmacy-edit controls.
from . import smart_schedule_template_ui as _smart_schedule_template_ui  # noqa: F401,E402
