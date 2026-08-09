"""Telegram update handlers."""

# Attach the extra shift-management handlers to the existing shifts router.
from . import shift_tools as _shift_tools  # noqa: F401,E402

# Merge the photographed Friday history into the smart scheduler before the
# admin handlers import the scheduling integration functions.
from app.services import smart_schedule_history_patch as _smart_schedule_history_patch  # noqa: F401,E402

# Replace random smart-schedule selection with deterministic fairness rules.
from app.services import smart_schedule_fair_patch as _smart_schedule_fair_patch  # noqa: F401,E402

# Guard batch-bearing smart callbacks before the workflow handlers are registered.
from . import smart_schedule_guard as _smart_schedule_guard  # noqa: F401,E402

# Intercept smart day editing, confirmed saves, rerolls and Word export before
# the legacy workflow handlers so all outputs use the current smart draft.
from . import smart_schedule_template_editor as _smart_schedule_template_editor  # noqa: F401,E402

# Attach the intelligent schedule workflow to the existing admin router.
from . import smart_schedules as _smart_schedules  # noqa: F401,E402

# Simplify only the smart-schedule admin UI without changing scheduling logic.
from . import smart_schedule_ui as _smart_schedule_ui  # noqa: F401,E402

# Rename the simplified edit/Word buttons to match the confirmed workflow.
from . import smart_schedule_ui_labels_patch as _smart_schedule_ui_labels_patch  # noqa: F401,E402

# Add the cycle-aware photographed Friday ledger editor.
from . import smart_schedule_friday_ui as _smart_schedule_friday_ui  # noqa: F401,E402
