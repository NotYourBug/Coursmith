"""Compatibility names delegate to issued-rights domains, never old SQL."""
from contextlib import closing
from pathlib import Path
import secrets

from pydantic import ValidationError

from .database import open_readonly
from .domain import Actor, BusinessError, Clock, utc_now
from .delivery.entitlements import AuthorizedSession, EntitlementService, RedemptionReceipt
from .delivery.progress import ProgressService
from .operations.codes import BatchInput, CodeService


class InvalidAccessCode(Exception):
    """Generic compatibility redemption denial."""


class InvalidSession(Exception):
    """Generic compatibility authorization denial."""


class AccessService:
    def __init__(self, database_path: Path, session_ttl_hours: int = 72, *, clock: Clock = utc_now):
        self.database_path = Path(database_path)
        self.entitlements = EntitlementService(self.database_path, clock=clock, session_ttl_hours=session_ttl_hours)
        self.progress = ProgressService(self.database_path, clock=clock, entitlement_service=self.entitlements)
        self.codes = CodeService(self.database_path, clock=clock)

    def create_access_code(self, course_id: str, *, actor: Actor, activation_days: int = 30) -> str:
        with closing(open_readonly(self.database_path)) as connection:
            row = connection.execute("SELECT id FROM products WHERE course_id=?", (course_id,)).fetchone()
        if not row:
            raise BusinessError("product_missing", "Course product is unavailable.", 404)
        try:
            data = BatchInput(product_id=row[0], purpose="sale", activation_days=activation_days)
        except ValidationError:
            raise BusinessError("invalid_activation_days", "Activation days must be an integer from 1 to 365.", 400) from None
        return self.codes.issue_batch(actor, data, secrets.token_hex(32)).codes[0].raw_code

    def redeem_access_code(self, raw_code: str, course_id: str) -> RedemptionReceipt:
        try:
            return self.entitlements.redeem(raw_code, expected_course_id=course_id, request_id=secrets.token_hex(16))
        except BusinessError:
            raise InvalidAccessCode("Unable to redeem this code.") from None

    def get_session(self, raw_session_id: str) -> AuthorizedSession | None:
        if not isinstance(raw_session_id, str) or not raw_session_id or len(raw_session_id) > 200:
            return None
        from .delivery.entitlements import _hash
        with closing(open_readonly(self.database_path)) as connection:
            row = connection.execute("SELECT course_id FROM sessions WHERE session_hash=?", (_hash(raw_session_id),)).fetchone()
        if not row:
            return None
        try:
            return self.require_session(raw_session_id, row[0])
        except InvalidSession:
            return None

    def require_session(self, raw_session_id: str, course_id: str) -> AuthorizedSession:
        try:
            return self.entitlements.require_session(raw_session_id, course_id)
        except BusinessError:
            raise InvalidSession("Learning session is unavailable.") from None

    def get_progress(self, session_id: str, course_id: str) -> dict[int, bool]:
        return self.progress.get_progress(session_id, course_id)

    def record_progress(self, session_id: str, course_id: str, chapter_number: int, completed: bool) -> None:
        self.progress.set_completed(session_id, course_id, chapter_number, completed)
