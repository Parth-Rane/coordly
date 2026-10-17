from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
from typing import Any


TASK_STATUSES = ("Pending", "In Progress", "Blocked", "Complete")
ANALYSIS_STATUSES = (*TASK_STATUSES, "Needs Clarification")
PRIORITIES = ("Low", "Medium", "High")
STATUS_TRANSITIONS = {
    "Pending": set(TASK_STATUSES),
    "In Progress": set(TASK_STATUSES),
    "Blocked": set(TASK_STATUSES),
    "Complete": {"Complete", "Pending", "In Progress"},
}


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Analysis:
    task_id: str | None
    status: str
    progress_summary: str
    blocker_reason: str
    next_action: str
    confidence: float
    clarification_question: str

    @classmethod
    def from_dict(cls, value: Any) -> "Analysis":
        required = {
            "task_id",
            "status",
            "progress_summary",
            "blocker_reason",
            "next_action",
            "confidence",
            "clarification_question",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ValidationError("AI result must contain exactly the required fields")
        if value["task_id"] is not None and (
            not isinstance(value["task_id"], str) or not value["task_id"].strip()
        ):
            raise ValidationError("task_id must be a non-empty string or null")
        if value["status"] not in ANALYSIS_STATUSES:
            raise ValidationError("AI result contains an unsupported status")
        for key in (
            "progress_summary",
            "blocker_reason",
            "next_action",
            "clarification_question",
        ):
            if not isinstance(value[key], str):
                raise ValidationError(f"{key} must be a string")
        confidence = value["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValidationError("confidence must be a number between 0 and 1")
        if not 0 <= confidence <= 1:
            raise ValidationError("confidence must be a number between 0 and 1")
        return cls(
            task_id=value["task_id"],
            status=value["status"],
            progress_summary=value["progress_summary"].strip(),
            blocker_reason=value["blocker_reason"].strip(),
            next_action=value["next_action"].strip(),
            confidence=float(confidence),
            clarification_question=value["clarification_question"].strip(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "progress_summary": self.progress_summary,
            "blocker_reason": self.blocker_reason,
            "next_action": self.next_action,
            "confidence": self.confidence,
            "clarification_question": self.clarification_question,
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_task_fields(fields: dict[str, Any], *, partial: bool = False) -> None:
    required = ("title", "owner_participant_id", "deadline", "status", "priority")
    if not partial:
        missing = [key for key in required if not fields.get(key)]
        if missing:
            raise ValidationError(f"Required task fields missing: {', '.join(missing)}")
    if "title" in fields and (not isinstance(fields["title"], str) or not fields["title"].strip()):
        raise ValidationError("Task title is required")
    if "deadline" in fields:
        try:
            date.fromisoformat(str(fields["deadline"]))
        except ValueError as exc:
            raise ValidationError("Deadline must be a valid YYYY-MM-DD date") from exc
    if "status" in fields and fields["status"] not in TASK_STATUSES:
        raise ValidationError("Unsupported task status")
    if "priority" in fields and fields["priority"] not in PRIORITIES:
        raise ValidationError("Priority must be Low, Medium, or High")
    if "owner_participant_id" in fields and not str(fields["owner_participant_id"]).strip():
        raise ValidationError("An assigned participant is required")
    for key in ("description", "blocker_reason", "next_action"):
        if key in fields and not isinstance(fields[key], str):
            raise ValidationError(f"{key} must be text")


def validate_telegram_chat_id(chat_id: str) -> str:
    value = str(chat_id).strip()
    if not re.fullmatch(r"-?\d{1,20}", value) or value == "0":
        raise ValidationError("Telegram chat ID must be a numeric ID (negative IDs are allowed)")
    return value
