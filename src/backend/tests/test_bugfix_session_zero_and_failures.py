import re
from app.models.session_zero_interview import (
    SessionZeroInterviewDraft,
    SessionZeroWorldDraft,
)
from app.services.session_zero_agent import SessionZeroInterviewService
from app.services.session_zero_interview import SessionZeroInterviewService as InterviewGuards


def test_boundaries_list_counts_as_present_and_auto_confirms():
    draft = SessionZeroInterviewDraft(
        world=SessionZeroWorldDraft(
            boundaries=["без пыток", "без вреда детям"],
            boundaries_confirmed=False,
            starting_location_name="Станция Полярная-7",
            world_summary="Научная станция в тундре.",
        )
    )
    assert InterviewGuards._topic_has_value(draft, "world.boundaries")
    normalized = SessionZeroInterviewService._normalize_world_draft_fields(draft)
    assert normalized.world.boundaries_confirmed is True
    assert normalized.world.setting_name == "Станция Полярная-7"


def test_failure_summary_prefers_db_error_over_json_column_name():
    def failure_summary(error: str) -> str:
        error = error.lower()
        if re.search(r"no such table|operationalerror|sqlite3\.|sqlalchemy|alembic", error):
            return "db"
        if re.search(
            r"(json\s*decode|jsondecode|invalid json|schema validation|structured output|structured response|validation error|failed to parse|parse error)",
            error,
        ):
            return "structured"
        return "other"

    sql = (
        "(sqlite3.OperationalError) no such table: truth_event_records\n"
        "[SQL: SELECT truth_event_records.payload_json FROM truth_event_records]"
    )
    assert failure_summary(sql) == "db"
