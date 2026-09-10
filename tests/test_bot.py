import csv
import io

from bot import (
    LEAD_STATUS_NOTIFIED,
    LEAD_STATUS_REPLIED,
    Database,
    format_summary,
)


def test_summary_escapes_html() -> None:
    escaped = format_summary(
        {"name": "<X>", "task": "a & b"}
    )
    assert "<X>" not in escaped
    assert "a & b" not in escaped
    assert "&lt;X&gt;" in escaped
    assert "a &amp; b" in escaped


def test_database_lifecycle(tmp_path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()

    lead_id = database.add_lead(
        telegram_user_id=42,
        telegram_username="developer",
        name="John",
        task="Build a Telegram bot",
    )

    assert lead_id == 1
    assert database.statistics() == (1, 1, 1, 0)

    database.update_status(lead_id, LEAD_STATUS_NOTIFIED)
    assert database.statistics() == (1, 1, 0, 0)

    database.update_status(lead_id, LEAD_STATUS_REPLIED)
    assert database.statistics() == (1, 1, 0, 1)

    lead = database.get_lead(lead_id)
    assert lead is not None
    assert lead["status"] == LEAD_STATUS_REPLIED

    decoded = database.export_csv().decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(decoded), delimiter=";"))
    assert rows[0][0] == "id"
    assert rows[1][2:4] == ["John", "Build a Telegram bot"]
    assert rows[1][-1] == LEAD_STATUS_REPLIED


def test_get_lead_for_user_returns_latest(tmp_path) -> None:
    database = Database(tmp_path / "test.db")
    database.initialize()

    first_id = database.add_lead(
        telegram_user_id=7, telegram_username=None,
        name="Anna", task="First lead",
    )
    second_id = database.add_lead(
        telegram_user_id=7, telegram_username="anna",
        name="Anna", task="Second lead",
    )

    latest = database.get_lead_for_user(7)
    assert latest is not None
    assert latest["id"] == second_id
    assert latest["task"] == "Second lead"

    other = database.get_lead_for_user(999)
    assert other is None

    assert first_id != second_id
