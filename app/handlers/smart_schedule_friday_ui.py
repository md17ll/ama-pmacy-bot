from __future__ import annotations

from datetime import date
from math import ceil

from aiogram import F
from aiogram.enums import ButtonStyle
from aiogram.types import CallbackQuery

from app import keyboards, texts
from app.config import Settings
from app.db import Database
from app.handlers import smart_schedule_ui as simple_ui
from app.handlers import smart_schedules as smart_handlers
from app.handlers.admin import router
from app.handlers.common import require_admin, require_writer
from app.services import smart_schedule as smart_service
from app.services.friday_history import friday_cycle_for
from app.services.friday_overrides import (
    build_friday_states,
    clear_friday_override,
    current_reference_date,
    set_friday_override,
    state_source_label,
)


FRIDAY_PER_PAGE = 8
_ORIGINAL_HOME_UI = simple_ui._home_ui


def _home_ui_with_friday(text, markup):
    rendered_text, rendered_markup = _ORIGINAL_HOME_UI(text, markup)
    rows = [list(row) for row in rendered_markup.inline_keyboard]
    insert_at = max(0, len(rows) - 2)
    rows.insert(
        insert_at,
        [keyboards.button("🕌 سجل الجمعة", "a:smart:friday:0", ButtonStyle.PRIMARY)],
    )
    return rendered_text, keyboards.keyboard(rows)


simple_ui._home_ui = _home_ui_with_friday


def _fmt(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _dates_text(values: tuple[date, ...]) -> str:
    return "، ".join(_fmt(value) for value in values) if values else "لا يوجد"


async def _load_states(db: Database, settings: Settings, reference_date: date):
    async with db.session_factory() as session:
        pharmacies, shifts = await smart_service._active_pharmacies_and_shifts(session)
        states = await build_friday_states(
            session,
            pharmacies,
            shifts,
            reference_date=reference_date,
            timezone=settings.timezone,
            before_date=None,
        )
    return sorted(states.values(), key=lambda item: (item.effective_count, item.name))


async def _render_friday_home(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    *,
    reference_date: date,
    page: int,
) -> None:
    states = await _load_states(db, settings, reference_date)
    cycle = friday_cycle_for(reference_date)
    pages = max(1, ceil(len(states) / FRIDAY_PER_PAGE))
    page = min(max(0, page), pages - 1)
    chosen = states[page * FRIDAY_PER_PAGE : (page + 1) * FRIDAY_PER_PAGE]

    zero = sum(1 for item in states if item.effective_count == 0)
    one = sum(1 for item in states if item.effective_count == 1)
    two = sum(1 for item in states if item.effective_count == 2)
    over = sum(1 for item in states if item.effective_count > 2)
    overrides = sum(1 for item in states if item.is_overridden)

    stats = [
        f"📅 دورة الجمعة: {_fmt(cycle.start)} → {_fmt(cycle.end)}",
        f"⚪ 0/2: {zero} | 🟡 1/2: {one} | 🟢 2/2: {two}",
        f"✏️ تعديلات يدوية: {overrides}",
        "📷 مرجع الصور محفوظ ولا يتغير؛ التعديل اليدوي يُحفظ فوقه ويمكن إلغاؤه.",
    ]
    if over:
        stats.append(f"⚠️ يوجد {over} سجل فوق حد 2/2 ويحتاج مراجعة.")

    rows = [
        [
            keyboards.button(
                f"{'✏️ ' if item.is_overridden else ''}{item.name} • 🕌 {item.effective_count}/2",
                f"a:smart:friday:p:{cycle.start.isoformat()}:{item.pharmacy_id}",
                ButtonStyle.PRIMARY,
            )
        ]
        for item in chosen
    ]
    nav = []
    if page:
        nav.append(keyboards.button("◀️ السابق", f"a:smart:friday:{page - 1}"))
    if page + 1 < pages:
        nav.append(keyboards.button("التالي ▶️", f"a:smart:friday:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([keyboards.button("⬅️ رجوع", smart_handlers.SMART_HOME)])

    await simple_ui._ORIGINAL_SAFE_EDIT(
        callback,
        texts.admin_section_text(
            "سجل الجمعة",
            f"اضغط على أي صيدلية لمراجعة رصيدها أو تعديله. صفحة {page + 1}/{pages}.",
            stats=stats,
        ),
        keyboards.keyboard(rows),
    )
    await simple_ui._ORIGINAL_ANSWER_CALLBACK(callback)


async def _render_pharmacy(
    callback: CallbackQuery,
    db: Database,
    settings: Settings,
    *,
    cycle_start: date,
    pharmacy_id: int,
) -> None:
    states = await _load_states(db, settings, cycle_start)
    state = next((item for item in states if item.pharmacy_id == pharmacy_id), None)
    if state is None:
        await simple_ui._ORIGINAL_ANSWER_CALLBACK(callback, "الصيدلية غير موجودة أو غير فعالة.", True)
        return

    stats = [
        f"📅 الدورة: {_fmt(state.cycle.start)} → {_fmt(state.cycle.end)}",
        f"🕌 الرصيد المستخدم بالمولّد: {state.effective_count}/2",
        f"📌 المصدر الحالي: {state_source_label(state)}",
        f"📷 تواريخ الصورة: {_dates_text(state.image_dates)}",
        f"📋 تواريخ الجدول المنشور: {_dates_text(state.database_dates)}",
    ]
    if state.override_count is not None:
        stats.append(f"✏️ التعديل اليدوي: {state.override_count}/2")
        stats.append("↩️ يمكنك إلغاء التعديل للرجوع تلقائياً لمرجع الصورة والجدول المنشور.")
    else:
        stats.append("✅ لا يوجد تعديل يدوي على هذا السجل.")

    rows = [
        [
            keyboards.button("0/2", f"a:smart:friday:set:{state.cycle.start.isoformat()}:{pharmacy_id}:0"),
            keyboards.button("1/2", f"a:smart:friday:set:{state.cycle.start.isoformat()}:{pharmacy_id}:1"),
            keyboards.button("2/2", f"a:smart:friday:set:{state.cycle.start.isoformat()}:{pharmacy_id}:2"),
        ]
    ]
    if state.override_count is not None:
        rows.append(
            [
                keyboards.button(
                    "↩️ إلغاء التعديل اليدوي",
                    f"a:smart:friday:clear:{state.cycle.start.isoformat()}:{pharmacy_id}",
                    ButtonStyle.PRIMARY,
                )
            ]
        )
    rows.append([keyboards.button("⬅️ رجوع للقائمة", "a:smart:friday:0")])

    await simple_ui._ORIGINAL_SAFE_EDIT(
        callback,
        texts.admin_section_text(
            f"سجل الجمعة › {state.name}",
            "يمكنك تصحيح الرصيد هنا بدون تعديل الصور الأصلية.",
            stats=stats,
            warning="التعديل يؤثر على توزيع الجمعات القادمة فقط، لذلك استخدمه لتصحيح السجل المؤكد.",
        ),
        keyboards.keyboard(rows),
    )
    await simple_ui._ORIGINAL_ANSWER_CALLBACK(callback)


@router.callback_query(F.data.startswith("a:smart:friday:set:"))
async def smart_friday_set(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, _, start_raw, pharmacy_raw, count_raw = (callback.data or "").split(":", 6)
        cycle_start = date.fromisoformat(start_raw)
        pharmacy_id = int(pharmacy_raw)
        count = int(count_raw)
        if count not in {0, 1, 2}:
            raise ValueError
    except (ValueError, AttributeError):
        await simple_ui._ORIGINAL_ANSWER_CALLBACK(callback, "قيمة التعديل غير صالحة.", True)
        return

    async with db.session_factory() as session:
        try:
            await set_friday_override(
                session,
                reference_date=cycle_start,
                pharmacy_id=pharmacy_id,
                count=count,
                admin_id=callback.from_user.id,
            )
        except ValueError as exc:
            await simple_ui._ORIGINAL_ANSWER_CALLBACK(callback, str(exc), True)
            return
    await _render_pharmacy(callback, db, settings, cycle_start=cycle_start, pharmacy_id=pharmacy_id)


@router.callback_query(F.data.startswith("a:smart:friday:clear:"))
async def smart_friday_clear(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_writer(callback, db) is None:
        return
    try:
        _, _, _, _, start_raw, pharmacy_raw = (callback.data or "").split(":", 5)
        cycle_start = date.fromisoformat(start_raw)
        pharmacy_id = int(pharmacy_raw)
    except (ValueError, AttributeError):
        await simple_ui._ORIGINAL_ANSWER_CALLBACK(callback, "قيمة التعديل غير صالحة.", True)
        return
    async with db.session_factory() as session:
        try:
            await clear_friday_override(
                session,
                reference_date=cycle_start,
                pharmacy_id=pharmacy_id,
                admin_id=callback.from_user.id,
            )
        except ValueError as exc:
            await simple_ui._ORIGINAL_ANSWER_CALLBACK(callback, str(exc), True)
            return
    await _render_pharmacy(callback, db, settings, cycle_start=cycle_start, pharmacy_id=pharmacy_id)


@router.callback_query(F.data.startswith("a:smart:friday:p:"))
async def smart_friday_pharmacy(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    try:
        _, _, _, _, start_raw, pharmacy_raw = (callback.data or "").split(":", 5)
        cycle_start = date.fromisoformat(start_raw)
        pharmacy_id = int(pharmacy_raw)
    except (ValueError, AttributeError):
        await simple_ui._ORIGINAL_ANSWER_CALLBACK(callback, "السجل غير صالح.", True)
        return
    await _render_pharmacy(callback, db, settings, cycle_start=cycle_start, pharmacy_id=pharmacy_id)


@router.callback_query(F.data.startswith("a:smart:friday:"))
async def smart_friday_home(callback: CallbackQuery, db: Database, settings: Settings) -> None:
    if await require_admin(callback, db) is None:
        return
    try:
        page = max(0, int((callback.data or "").rsplit(":", 1)[1]))
    except (ValueError, AttributeError):
        await simple_ui._ORIGINAL_ANSWER_CALLBACK(callback, "الصفحة غير صالحة.", True)
        return
    await _render_friday_home(
        callback,
        db,
        settings,
        reference_date=current_reference_date(settings.timezone),
        page=page,
    )
