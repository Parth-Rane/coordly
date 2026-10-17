import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TaskStatus(Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"


@dataclass
class Task:
    id: str
    title: str
    assignee: str
    status: TaskStatus
    blocker_reason: Optional[str] = None


_BLOCKER_PATTERNS = (
    re.compile(
        r"\b(?P<cue>waiting\s+(?:for|on))\s+(?P<reason>[^,.!?;\n]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<cue>blocked\s+by)\s+(?P<reason>[^,.!?;\n]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<cue>depends\s+on)\s+(?P<reason>[^,.!?;\n]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?P<cue>pending)(?:\s+(?:(?:on|for)\s+))?(?P<reason>[^,.!?;\n]+)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:waiting\s+(?:for|on)|blocked\s+by|depends\s+on)\b", re.IGNORECASE),
    re.compile(r"\bblocked\b", re.IGNORECASE),
)

_NEGATED_COMPLETION = re.compile(
    r"\b(?:not|never|haven't|havent|hasn't|hasnt|isn't|isnt|almost|nearly|still)\b"
    r".{0,24}\b(?:done|complete|completed|finished)\b",
    re.IGNORECASE,
)
_BLOCKER_RESOLUTION_PATTERNS = (
    re.compile(r"\b(?:not|no longer|no-longer)\s+blocked\b", re.IGNORECASE),
    re.compile(r"\b(?:blocker|issue|problem)\s+(?:is|was)?\s*(?:resolved|fixed|cleared|gone)\b", re.IGNORECASE),
    re.compile(r"\b(?:resolved|fixed|cleared|unblocked)\b", re.IGNORECASE),
    re.compile(r"\b(?:access|permission|approval)\s+(?:is\s+)?(?:fixed|resolved|granted|working|available)\b", re.IGNORECASE),
)
_COMPLETION_PATTERNS = (
    re.compile(r"\b(?:done|completed|finished|all done)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:task|work|it|this)\s+(?:is|was)\s+"
        r"(?:done|complete|completed|finished)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bi(?:'m| am| have|’ve)\s+(?:done|finished|completed)\b", re.IGNORECASE),
    re.compile(r"\bi(?:'ve| have)\s+completed\b", re.IGNORECASE),
)


def _implies_blocker_resolution(user_reply: str) -> bool:
    normalized = re.sub(r"\s+", " ", user_reply).strip()
    return any(pattern.search(normalized) for pattern in _BLOCKER_RESOLUTION_PATTERNS)


def extract_blocker_reason(user_reply: str) -> Optional[str]:
    if _implies_blocker_resolution(user_reply):
        return None
    for pattern in _BLOCKER_PATTERNS:
        match = pattern.search(user_reply)
        if match is not None:
            reason = match.groupdict().get("reason")
            if reason:
                cleaned_reason = reason.strip(" ,")
                if cleaned_reason:
                    cue = match.groupdict().get("cue")
                    if cue:
                        return f"{cue.strip()} {cleaned_reason}"
                    return cleaned_reason
            return user_reply.strip()
    return None


def _implies_completion(user_reply: str) -> bool:
    if _NEGATED_COMPLETION.search(user_reply):
        return False
    return any(pattern.search(user_reply) for pattern in _COMPLETION_PATTERNS)


def process_task_update(task: Task, user_reply: str) -> Task:
    """Apply the status implied by a reply, prioritizing blockers over completion."""
    blocker_reason = extract_blocker_reason(user_reply)
    if blocker_reason is not None:
        task.status = TaskStatus.BLOCKED
        task.blocker_reason = blocker_reason
    elif _implies_completion(user_reply):
        task.status = TaskStatus.COMPLETED
        task.blocker_reason = None
    elif _implies_blocker_resolution(user_reply):
        task.status = TaskStatus.IN_PROGRESS
        task.blocker_reason = None
    elif re.search(
        r"\b(?:not started|haven't started|havent started|yet to start|pending)\b",
        user_reply,
        re.IGNORECASE,
    ):
        task.status = TaskStatus.PENDING
        task.blocker_reason = None
    elif re.search(
        r"\b(?:in progress|working on it|currently working|started working|resumed work)\b",
        user_reply,
        re.IGNORECASE,
    ):
        task.status = TaskStatus.IN_PROGRESS
        task.blocker_reason = None
    return task
