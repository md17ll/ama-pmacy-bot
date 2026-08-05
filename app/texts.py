from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from app.models import ImportBatch, ImportRow, Pharmacy, Shift
from app.utils import (
    as_local,
    format_date_ar,
    format_duration,
    format_time_ar,
    html,
)


def _period_label(start: datetime) -> str:
    return "☀️ مناوبة نهارية" if start.hour < 18 else "🌙 مناوبة مسائية"


def user_home_text(now: datetime, timezone, last_update: datetime | None = None) -> str:
    local = as_local(now, timezone)
    update_line = (
        f"\n✅ آخر تحديث للجدول: {format_date_ar(last_update, timezone)}، {format_time_ar(last_update, timezone)}"
        if last_update
        else "\n⚠️ لم يتم نشر جدول مناوبات حتى الآن."
    )
    return (
        "💊 <b>صيدليات عامودا المناوبة</b>\n\n"
        f"📅 {format_date_ar(local)}\n"
        f"🕐 الوقت الآن: {format_time_ar(local)}"
        f"{update_line}\n\n"
        "اختر الخدمة المطلوبة من الأزرار:"
    )


def shifts_text(title: str, shifts: Iterable[Shift], now: datetime, timezone) -> str:
    shifts = list(shifts)
    local_now = as_local(now, timezone)
    lines = [
        f"{title}\n",
        f"📅 {format_date_ar(local_now)}",
        f"🕐 الوقت الآن: {format_time_ar(local_now)}",
    ]
    if not shifts:
        lines.extend(["", "⚠️ لا توجد مناوبات مسجلة لهذه الفترة."])
        return "\n".join(lines)

    for shift in shifts:
        start = as_local(shift.start_at, timezone)
        end = as_local(shift.end_at, timezone)
        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━",
                f"💊 <b>{html(shift.pharmacy.name)}</b>",
                f"{_period_label(start)}",
                f"🕐 {format_time_ar(start)} – {format_time_ar(end)}",
                f"📍 {html(shift.pharmacy.address)}",
            ]
        )
        if shift.start_at <= now < shift.end_at:
            lines.append(f"🟢 مناوبة الآن — تنتهي بعد {format_duration(shift.end_at - now)}")
        elif shift.start_at > now:
            lines.append(f"🟡 تبدأ بعد {format_duration(shift.start_at - now)}")
    return "\n".join(lines)


def pharmacy_result_text(pharmacy: Pharmacy, next_shift: Shift | None, now: datetime, timezone) -> str:
    lines = [
        "🔍 <b>نتيجة البحث</b>",
        "",
        f"💊 <b>{html(pharmacy.name)}</b>",
        f"📍 {html(pharmacy.address)}",
    ]
    if next_shift:
        start = as_local(next_shift.start_at, timezone)
        end = as_local(next_shift.end_at, timezone)
        if next_shift.start_at <= now < next_shift.end_at:
            lines.extend(
                [
                    "",
                    "🟢 <b>مناوبة الآن</b>",
                    f"{_period_label(start)}",
                    f"📅 {format_date_ar(start)}",
                    f"🕐 {format_time_ar(start)} – {format_time_ar(end)}",
                    f"⏳ تنتهي بعد {format_duration(next_shift.end_at - now)}",
                ]
            )
        else:
            lines.extend(
                [
                    "",
                    "📅 <b>المناوبة القادمة</b>",
                    f"{_period_label(start)}",
                    format_date_ar(start),
                    f"🕐 {format_time_ar(start)} – {format_time_ar(end)}",
                ]
            )
    else:
        lines.extend(["", "لا توجد مناوبة قادمة مسجلة لهذه الصيدلية حالياً."])
    return "\n".join(lines)


def admin_section_text(
    breadcrumb: str,
    description: str,
    *,
    stats: Iterable[str] = (),
    warning: str | None = None,
) -> str:
    lines = [f"⚙️ <b>لوحة الإدارة › {html(breadcrumb)}</b>", "", description]
    stats = list(stats)
    if stats:
        lines.extend(["", *stats])
    if warning:
        lines.extend(["", f"⚠️ {warning}"])
    lines.extend(["", "اختر العملية المطلوبة:"])
    return "\n".join(lines)


def admin_home_text(stats: dict, now: datetime, timezone) -> str:
    latest = stats.get("latest_shift_end")
    if latest:
        latest_line = f"📅 آخر مناوبة مسجلة: {format_date_ar(latest, timezone)}"
        remaining = latest - now
        remaining_line = (
            f"⏳ المتبقي من الجدول: {format_duration(remaining)}"
            if remaining.total_seconds() > 0
            else "⚠️ انتهى جدول المناوبات المسجل"
        )
    else:
        latest_line = "📅 لا يوجد جدول منشور"
        remaining_line = "⚠️ ارفع جدول المناوبات الأول"
    return admin_section_text(
        "الرئيسية",
        "من هنا يمكنك إدارة الصيدليات والمناوبات والاستيراد والنسخ الاحتياطية. كل عملية حذف أو نشر تحتاج إلى تأكيد.",
        stats=[
            latest_line,
            remaining_line,
            f"🏥 عدد الصيدليات: {stats['pharmacies']}",
            f"👥 عدد المستخدمين: {stats['users']}",
            f"⚠️ الأخطاء المفتوحة: {stats['errors']}",
        ],
    )


def batch_summary_text(batch: ImportBatch) -> str:
    summary = batch.summary or {}
    source = (
        "GPT-5.4 Mini عبر OpenRouter"
        if batch.source_type == "gemini"
        else "Word الرسمي"
        if batch.source_type == "word"
        else "Excel"
        if batch.source_type == "excel"
        else "يدوي"
    )
    lines = [
        f"📝 <b>مسودة رقم {batch.id}</b>",
        "",
        f"📥 المصدر: {source}",
        f"📊 عدد السطور: {summary.get('total', len(batch.rows))}",
        f"✅ جاهزة: {summary.get('ready', 0)}",
        f"⚠️ تحتاج مراجعة: {summary.get('needs_review', 0)}",
        f"❌ غير مطابقة: {summary.get('unmatched', 0)}",
    ]
    if batch.period_start and batch.period_end:
        lines.append(f"📅 الفترة: {format_date_ar(batch.period_start)} — {format_date_ar(batch.period_end)}")
    lines.extend(["", "راجع الأخطاء والمعاينة قبل النشر."])
    return "\n".join(lines)


def batch_rows_preview(batch: ImportBatch, timezone, only_errors: bool = False, limit: int = 20) -> str:
    rows = [row for row in batch.rows if row.errors] if only_errors else list(batch.rows)
    title = "⚠️ <b>أخطاء المسودة</b>" if only_errors else "👁 <b>معاينة المسودة</b>"
    lines = [title, ""]
    if not rows:
        return title + "\n\n✅ لا توجد أخطاء."
    for row in rows[:limit]:
        pharmacy = row.matched_pharmacy.name if row.matched_pharmacy else row.raw_pharmacy_name
        lines.append(f"• السطر {row.row_number}: <b>{html(pharmacy)}</b>")
        if row.start_at and row.end_at:
            start = as_local(row.start_at, timezone)
            lines.append(f"  {_period_label(start)}")
            lines.append(
                f"  {format_date_ar(row.start_at, timezone)} | {format_time_ar(row.start_at, timezone)} – {format_time_ar(row.end_at, timezone)}"
            )
        for error in row.errors:
            lines.append(f"  ⚠️ {html(error)}")
        lines.append("")
    if len(rows) > limit:
        lines.append(f"… ويوجد {len(rows) - limit} سطر إضافي.")
    return "\n".join(lines)


def pharmacy_admin_text(pharmacy: Pharmacy) -> str:
    aliases = "، ".join(alias.alias for alias in pharmacy.aliases) or "لا يوجد"
    return (
        f"💊 <b>{html(pharmacy.name)}</b>\n\n"
        f"📍 العنوان: {html(pharmacy.address)}\n"
        f"🔤 الأسماء البديلة: {html(aliases)}\n"
        f"📌 الحالة: {status_ar(pharmacy.status)}\n"
        f"📝 الملاحظات: {html(pharmacy.notes or 'لا يوجد')}"
    )


def status_ar(status: str) -> str:
    return {
        "active": "✅ فعالة",
        "temporarily_closed": "⏸ مغلقة مؤقتاً",
        "inactive": "🚫 متوقفة",
        "deleted": "🗑 محذوفة",
    }.get(status, html(status))
