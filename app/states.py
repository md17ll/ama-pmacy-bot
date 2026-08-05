from aiogram.fsm.state import State, StatesGroup


class UserSearchState(StatesGroup):
    waiting_query = State()


class AdminImportState(StatesGroup):
    waiting_image = State()
    waiting_excel = State()
    waiting_pharmacies_excel = State()
    waiting_missing_pharmacies_excel = State()


class PharmacyCreateState(StatesGroup):
    waiting_name = State()
    waiting_address = State()
    waiting_aliases = State()


class PharmacySearchState(StatesGroup):
    waiting_query = State()


class PharmacyEditState(StatesGroup):
    waiting_value = State()


class ShiftCreateState(StatesGroup):
    waiting_pharmacy = State()
    waiting_date = State()
    waiting_start = State()
    waiting_end = State()


class ShiftSearchState(StatesGroup):
    waiting_query = State()


class DeletePeriodState(StatesGroup):
    waiting_start = State()
    waiting_end = State()


class AdminAddState(StatesGroup):
    waiting_id = State()
    waiting_role = State()
