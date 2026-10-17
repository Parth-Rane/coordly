import re
from typing import Any, Callable

import database
from agent import AnalysisError, analyze_reply
from database import FollowUpCooldownError as FollowUpCooldown
from llm_provider import ProviderError
from models import Analysis, ValidationError
from task_status import Task, TaskStatus, extract_blocker_reason, process_task_update


def is_clear_completion(reply: str) -> bool:
    text = re.sub(r"\s+", " ", reply.lower()).strip()
    if re.search(r"\b(not|never|haven't|havent|hasn't|hasnt|isn't|isnt|almost|nearly|still)\b.{0,24}\b(done|complete|completed|finished)\b", text):
        return False
    patterns = (
        r"\b(done|completed|finished|all done)\b",
        r"\b(task|work|it|this)\s+(is|was)\s+(done|complete|completed|finished)\b",
        r"\bi('m| am| have|’ve)\s+(done|finished|completed)\b",
        r"\bi('ve| have)\s+completed\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def has_unresolved_blocker_signal(reply: str) -> bool:
    text = re.sub(r"\s+", " ", reply.lower()).strip()
    if is_clear_blocker_resolution(text) or re.search(
        r"\b(not|never|no longer)\s+blocked\b|\bno blockers?\b", text
    ):
        return False
    return bool(
        re.search(
            r"\b(blocked|blocker|stuck|unable to|can't|cannot|waiting for)\b",
            text,
        )
    )


def handle_incoming_reply(
    db_path: str,
    *,
    update_id: str,
    telegram_message_id: str,
    sender_chat_id: str,
    text: str,
    reply_to_telegram_message_id: str | None = None,
    analyzer: Callable[..., Analysis] = analyze_reply,
) -> dict[str, Any]:
    message_id, is_new = database.insert_incoming_message(
        db_path, update_id, telegram_message_id, sender_chat_id, text,
        reply_to_telegram_message_id=reply_to_telegram_message_id,
    )
    if not is_new:
        return {"action": "duplicate", "reply_text": "This message was already received."}

    return _analyze_existing_message(
        db_path, message_id, sender_chat_id, text, analyzer
    )


def retry_failed_analysis(
    db_path: str,
    message_id: str,
    analyzer: Callable[..., Analysis] = analyze_reply,
) -> dict[str, Any]:
    message = database.get_message(db_path, message_id)
    if message is None:
        raise ValidationError("Message not found")
    if message["resulting_action"] != "analysis_failed":
        raise ValidationError("Only messages with failed analysis can be retried")
    return _analyze_existing_message(
        db_path, message_id, message["sender_chat_id"], message["body"], analyzer
    )


def _analyze_existing_message(
    db_path: str,
    message_id: str,
    sender_chat_id: str,
    text: str,
    analyzer: Callable[..., Analysis],
) -> dict[str, Any]:
    participant = database.get_participant_by_chat_id(db_path, sender_chat_id)
    if participant is None:
        response = "Your Telegram account is not registered with this project. Please contact the project manager."
        database.store_message_result(
            db_path, message_id, interpretation=None, confidence=None,
            action="unauthorized_sender",
        )
        return {"action": "unauthorized_sender", "reply_text": response}

    tasks = [
        task for task in database.list_tasks(db_path, owner_participant_id=participant["participant_id"])
        if task["status"] != "Complete"
    ]
    if not tasks:
        response = "You have no open tasks assigned right now. Contact the project manager if this is unexpected."
        database.store_message_result(
            db_path, message_id, interpretation=None, confidence=None, action="no_open_tasks"
        )
        return {"action": "no_open_tasks", "reply_text": response}

    try:
        analysis = analyzer(text, participant["name"], tasks)
    except (AnalysisError, ProviderError) as exc:
        database.store_message_result(
            db_path, message_id, interpretation=None, confidence=None,
            action="analysis_failed", error=str(exc),
        )
        return {
            "action": "analysis_failed",
            "reply_text": "I saved your reply, but could not analyze it. Please try again later or contact the project manager.",
            "message_id": message_id,
        }

    candidate_ids = {task["task_id"] for task in tasks}
    if analysis.task_id is not None and analysis.task_id not in candidate_ids:
        response = "I could not match that reply to one of your open tasks. Please reply with the task title."
        database.store_message_result(
            db_path, message_id, interpretation=analysis.as_dict(),
            confidence=analysis.confidence, action="mismatched_task_id",
        )
        return {"action": "mismatched_task_id", "reply_text": response}
    if analysis.task_id is None and len(tasks) == 1:
        selected = tasks[0]
    elif analysis.task_id is None:
        response = analysis.clarification_question or (
            "Which task are you updating? Please include its title so I can route your reply."
        )
        database.store_message_result(
            db_path, message_id, interpretation=analysis.as_dict(),
            confidence=analysis.confidence, action="needs_clarification",
        )
        return {"action": "needs_clarification", "reply_text": response}
    else:
        selected = next(task for task in tasks if task["task_id"] == analysis.task_id)

    current_status = {
        "Pending": TaskStatus.PENDING,
        "In Progress": TaskStatus.IN_PROGRESS,
        "Blocked": TaskStatus.BLOCKED,
        "Complete": TaskStatus.COMPLETED,
    }[selected["status"]]
    status_task = Task(
        id=selected["task_id"],
        title=selected["title"],
        assignee=participant["name"],
        status=current_status,
        blocker_reason=selected.get("blocker_reason") or None,
    )
    reply_blocker_reason = extract_blocker_reason(text)
    process_task_update(status_task, text)
    reply_has_blocker = (
        status_task.status is TaskStatus.BLOCKED and reply_blocker_reason is not None
    )

    if analysis.confidence < 0.55 or (
        analysis.status == "Needs Clarification" and not reply_has_blocker
    ):
        response = analysis.clarification_question or (
            f"Could you clarify the status of “{selected['title']}”?"
        )
        database.store_message_result(
            db_path, message_id, interpretation=analysis.as_dict(),
            confidence=analysis.confidence, action="needs_clarification",
        )
        return {"action": "needs_clarification", "reply_text": response}

    if reply_has_blocker:
        effective_status = "Blocked"
        blocker = status_task.blocker_reason or text.strip()
    else:
        effective_status = analysis.status

    if effective_status != "Blocked" and has_unresolved_blocker_signal(text):
        response = "I noticed a possible blocker. Please confirm what is blocking the task."
        database.store_message_result(
            db_path, message_id, interpretation=analysis.as_dict(),
            confidence=analysis.confidence, action="needs_clarification",
        )
        return {"action": "needs_clarification", "reply_text": response}

    if effective_status == "Complete":
        if not is_clear_completion(text) or analysis.blocker_reason:
            response = "Please confirm whether this task is fully finished and all blockers are resolved."
            database.store_message_result(
                db_path, message_id, interpretation=analysis.as_dict(),
                confidence=analysis.confidence, action="completion_not_confirmed",
            )
            return {"action": "completion_not_confirmed", "reply_text": response}
        blocker = ""
    elif effective_status == "Blocked":
        if reply_has_blocker and analysis.status == "Blocked" and analysis.blocker_reason:
            blocker = analysis.blocker_reason
        elif reply_has_blocker:
            blocker = status_task.blocker_reason or text.strip()
        elif not analysis.blocker_reason:
            response = "What is blocking this task? Please describe the blocker."
            database.store_message_result(
                db_path, message_id, interpretation=analysis.as_dict(),
                confidence=analysis.confidence, action="needs_clarification",
            )
            return {"action": "needs_clarification", "reply_text": response}
        else:
            blocker = analysis.blocker_reason
    else:
        blocker = analysis.blocker_reason

    if selected["status"] == "Blocked" and effective_status != "Blocked":
        if not is_clear_blocker_resolution(text):
            response = "Has the blocker been resolved? Please confirm before I change this task's status."
            database.store_message_result(
                db_path, message_id, interpretation=analysis.as_dict(),
                confidence=analysis.confidence, action="needs_clarification",
            )
            return {"action": "needs_clarification", "reply_text": response}

    if effective_status != "Blocked" and analysis.blocker_reason:
        response = "I detected a blocker but could not confidently classify its status. Please clarify the blocker or current progress."
        database.store_message_result(
            db_path, message_id, interpretation=analysis.as_dict(),
            confidence=analysis.confidence, action="needs_clarification",
        )
        return {"action": "needs_clarification", "reply_text": response}

    changes = {
        "status": effective_status,
        "blocker_reason": blocker,
        "next_action": analysis.next_action,
    }
    if effective_status != "Blocked" and selected["status"] == "Blocked":
        changes["blocker_reason"] = ""
    interpretation = analysis.as_dict()
    if reply_has_blocker:
        interpretation["status"] = "Blocked"
        interpretation["blocker_reason"] = blocker
    try:
        database.apply_reply_update(db_path, message_id, selected["task_id"], changes, interpretation)
    except ValidationError as exc:
        database.store_message_result(
            db_path, message_id, interpretation=interpretation,
            confidence=analysis.confidence, action="update_rejected", error=str(exc),
        )
        return {
            "action": "update_rejected",
            "reply_text": "I couldn't safely apply that update. Please contact the project manager.",
        }
    return {
        "action": "task_updated",
        "task_id": selected["task_id"],
        "status": effective_status,
        "reply_text": f"Thanks, I updated “{selected['title']}” to {effective_status}.",
    }


def is_clear_blocker_resolution(reply: str) -> bool:
    text = re.sub(r"\s+", " ", reply.lower()).strip()
    patterns = (
        r"\b(blocker|issue|problem)\s+(is|was)?\s*(resolved|fixed|cleared)\b",
        r"\b(resolved|fixed|cleared|unblocked)\b",
        r"\b(no longer blocked|not blocked anymore)\b",
        r"\b(access|permission|approval)\s+(is\s+)?(fixed|resolved|granted|working|available)\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def follow_up_text(task: dict[str, Any]) -> str:
    return (
        f"Project check-in: {task['title']}\n"
        f"Deadline: {task['deadline']}\n\n"
        "Please reply with your current progress and any blockers. "
        "If you have multiple assigned tasks, include the task title."
    )
