"""Keep Word preview day/evening placement identical to the smart draft."""

from app.handlers import smart_schedules as smart_ui
from app.services import smart_schedule_edit_patch as edit_patch


smart_ui.draft_shift_views = edit_patch.draft_shift_views
