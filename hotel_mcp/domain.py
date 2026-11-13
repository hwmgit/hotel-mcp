"""Business types and validation; no platform or transport dependencies."""
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone


CATEGORIES = ("air_conditioning", "plumbing_electrical", "network", "door_lock", "facilities", "other")
STATUSES = ("pending", "accepted", "completed", "rejected", "cancelled", "closed_unresolved",
            "quoted", "confirmed", "processing", "failed")
REQUEST_TYPES = ("general", "repair", "stay_extension", "meal", "gym_booking", "meeting_booking")
ACTIONS = ("accept", "add_note", "complete", "reject", "close_unresolved")


class BusinessError(ValueError):
    def __init__(self, code, message, retryable=False):
        self.code, self.message, self.retryable = code, message, retryable
        # SDK may wrap errors raised while resolving a tool/resource, before our handler.
        super().__init__(json.dumps(self.payload(), ensure_ascii=False))

    def payload(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


def fail(code, message):
    raise BusinessError(code, message)


def text(value, field, maximum, optional=False):
    if not isinstance(value, str) or not (0 if optional else 1) <= len(value.strip()) <= maximum:
        fail("VALIDATION_ERROR", f"{field} 长度必须为 {0 if optional else 1}～{maximum} 字符")
    return value.strip()


def version(value):
    if type(value) is not int or value < 1:
        fail("VALIDATION_ERROR", "expected_version 必须为正整数")
    return value


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp(value, field):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, AttributeError, TypeError):
        fail("VALIDATION_ERROR", f"{field} 必须为带时区的 ISO 时间")


def checkout_date(value):
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except (ValueError, TypeError):
        fail("VALIDATION_ERROR", "expected_checkout_date 必须为 YYYY-MM-DD")
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Principal:
    hotel: str
    role: str
    room: str = ""
    stay_id: str = ""
    principal_id: str = ""
    allowed_hotels: tuple[str, ...] = ()
    account_id: str = ""
    auth_version: int = 0
    history_readonly: bool = False
    account_role: str = ""
    agent_type: str = ""
    request_types: tuple[str, ...] = ()

    def __post_init__(self):
        if self.request_types and (self.role != "frontdesk" or not isinstance(self.request_types, tuple)
                                   or any(kind not in REQUEST_TYPES for kind in self.request_types)):
            raise ValueError("request type scope requires a staff agent and known business types")
        if self.role == "manager":
            if (self.hotel or self.room or self.stay_id or not isinstance(self.allowed_hotels, tuple)
                    or not 1 <= len(self.allowed_hotels) <= 100
                    or any(not isinstance(h, str) or not h.strip() or h != h.strip() or len(h) > 80
                           for h in self.allowed_hotels)
                    or len(set(self.allowed_hotels)) != len(self.allowed_hotels)):
                raise ValueError("manager requires 1-100 explicit unique allowed_hotels and no hotel/room/stay")
            return
        if self.allowed_hotels:
            raise ValueError("allowed_hotels is only valid for manager")
        if not self.hotel or self.role not in ("room", "frontdesk") or (self.role == "room" and not self.room):
            raise ValueError("invalid principal")
        if self.role == "frontdesk" and (self.room or self.stay_id):
            raise ValueError("frontdesk principal must not specify room or stay")

    @property
    def actor_id(self):
        # Local store callers may omit an endpoint ID; HTTP callers always supply it.
        return self.principal_id or f"{self.role}:{self.room}:{self.stay_id}"
