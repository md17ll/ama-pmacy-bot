USER_HOME = "u:home"
USER_NOW = "u:now"
USER_TODAY = "u:today"
USER_TOMORROW = "u:tomorrow"
USER_SEARCH = "u:search"
USER_REFRESH = "u:refresh"

ADMIN_HOME = "a:home"
ADMIN_IMPORT = "a:import"
ADMIN_IMPORT_GEMINI = "a:import:gemini"
ADMIN_IMPORT_EXCEL = "a:import:excel"
ADMIN_IMPORT_MANUAL = "a:import:manual"
ADMIN_TEMPLATE_SHIFTS = "a:tpl:shifts"
ADMIN_DRAFTS = "a:drafts"
ADMIN_SHIFTS = "a:shifts"
ADMIN_PHARMACIES = "a:pharmacies"
ADMIN_ERRORS = "a:errors"
ADMIN_UNDO = "a:undo"
ADMIN_PREVIEW = "a:preview"
ADMIN_EXPORTS = "a:exports"
ADMIN_STATS = "a:stats"
ADMIN_ADMINS = "a:admins"
ADMIN_ENTRY_NOTIFICATIONS = "a:entry_notify"
ADMIN_BACK_USER = "a:back_user"

ADMIN_SHIFT_NOW = "a:s:now"
ADMIN_SHIFT_TODAY = "a:s:today"
ADMIN_SHIFT_TOMORROW = "a:s:tomorrow"
ADMIN_SHIFT_UPCOMING = "a:s:upcoming"
ADMIN_SHIFT_SEARCH = "a:s:search"
ADMIN_SHIFT_ADD = "a:s:add"
ADMIN_SHIFT_DELETE_PERIOD = "a:s:delperiod"
ADMIN_SHIFT_CHECK = "a:s:check"

ADMIN_PHARMACY_ADD = "a:p:add"
ADMIN_PHARMACY_SEARCH = "a:p:search"
ADMIN_PHARMACY_LIST = "a:p:list"
ADMIN_PHARMACY_INCOMPLETE = "a:p:incomplete"
ADMIN_PHARMACY_IMPORT = "a:p:import"
ADMIN_TEMPLATE_PHARMACIES = "a:tpl:pharmacies"

ADMIN_EXPORT_PHARMACIES = "a:e:pharmacies"
ADMIN_EXPORT_SHIFTS = "a:e:shifts"
ADMIN_EXPORT_JSON = "a:e:json"

ADMIN_ADMIN_ADD = "a:m:add"
ADMIN_ADMIN_LIST = "a:m:list"
ADMIN_ADMIN_TOGGLE_NOTIFY = "a:m:notify"

ALL_STATIC_CALLBACKS = {
    value
    for name, value in globals().items()
    if name.isupper() and isinstance(value, str) and (value.startswith("u:") or value.startswith("a:"))
}
