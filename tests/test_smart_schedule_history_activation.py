from __future__ import annotations


def test_bot_handler_import_activates_friday_history_integration() -> None:
    import app.handlers  # noqa: F401
    from app.services import smart_schedule
    from app.services.smart_schedule_history_patch import (
        analyze_batch,
        generate_import_rows,
        pharmacy_year_statistics,
    )

    assert smart.generate_import_rows is generate_import_rows
    assert smart.analyze_batch is analyze_batch
    assert smart.pharmacy_year_statistics is pharmacy_year_statistics
