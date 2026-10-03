"""Online progress belongs to persistent rights, shared across buyer sessions."""

from pathlib import Path
import sqlite3

from ..database import to_db_time, transaction
from ..domain import BusinessError, Clock, utc_now
from .entitlements import EntitlementService


class ProgressService:
    def __init__(self, db_path: Path, *, clock: Clock = utc_now,
                 entitlement_service: EntitlementService | None = None):
        self.db_path = Path(db_path)
        self.clock = clock
        self.entitlements = entitlement_service or EntitlementService(self.db_path, clock=clock)

    def _authorize(self, connection, raw_token, course_id):
        authorized = self.entitlements.require_session_in_tx(connection, raw_token, course_id)
        if authorized.issued_policy is not None and not authorized.issued_policy.access.online:
            raise BusinessError("online_unavailable", "Online learning is unavailable for this entitlement.", 403)
        return authorized

    def set_completed(self, raw_token: str, course_id: str, chapter_number: int, completed: bool) -> None:
        try:
            with transaction(self.db_path, immediate=True) as connection:
                authorized = self._authorize(connection, raw_token, course_id)
                if type(chapter_number) is not int or not 1 <= chapter_number <= 2**63 - 1 or type(completed) is not bool:
                    raise BusinessError("invalid_progress", "Provide a chapter number and a boolean completion value.", 400)
                if not connection.execute("SELECT 1 FROM chapters WHERE course_id=? AND chapter_number=?", (course_id, chapter_number)).fetchone():
                    raise BusinessError("invalid_progress", "Chapter is unavailable.", 400)
                connection.execute("""INSERT INTO entitlement_progress
                    (entitlement_id, course_id, chapter_number, completed, updated_at) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(entitlement_id, course_id, chapter_number) DO UPDATE SET
                    completed=excluded.completed, updated_at=excluded.updated_at""",
                    (authorized.entitlement_id, course_id, chapter_number, int(completed), to_db_time(self.clock())))
        except sqlite3.DatabaseError:
            raise BusinessError("progress_unavailable", "Unable to save learning progress.", 409) from None

    def get_progress(self, raw_token: str, course_id: str) -> dict[int, bool]:
        with transaction(self.db_path, immediate=True) as connection:
            authorized = self._authorize(connection, raw_token, course_id)
            return {row["chapter_number"]: bool(row["completed"]) for row in connection.execute(
                "SELECT chapter_number, completed FROM entitlement_progress WHERE entitlement_id=? AND course_id=? ORDER BY chapter_number",
                (authorized.entitlement_id, course_id))}
