import json
from datetime import date
from typing import Any

import database


def _day_delta(deadline: str) -> int | None:
    try:
        return (date.fromisoformat(deadline) - date.today()).days
    except (TypeError, ValueError):
        return None


def _message_task_id(message: dict[str, Any]) -> str | None:
    if message.get("task_id"):
        return message["task_id"]
    interpretation = message.get("ai_interpretation")
    if isinstance(interpretation, str):
        try:
            interpretation = json.loads(interpretation)
        except json.JSONDecodeError:
            return None
    if isinstance(interpretation, dict):
        task_id = interpretation.get("task_id")
        return task_id if isinstance(task_id, str) else None
    return None


def build_project_snapshot(db_path: str) -> dict[str, Any]:
    tasks = database.list_tasks(db_path)
    participants = database.list_participants(db_path)
    messages = database.get_recent_messages(db_path, 200)
    dependencies = database.list_task_dependencies(db_path)
    stats = database.get_task_statistics(db_path)
    activities = database.get_recent_task_activity(
        db_path, [task["task_id"] for task in tasks], per_task_limit=5
    )
    task_by_id = {task["task_id"]: task for task in tasks}
    workload: list[dict[str, Any]] = []
    for participant in participants:
        owned_tasks = [
            task for task in tasks
            if task["owner_participant_id"] == participant["participant_id"]
        ]
        workload.append({
            "participant_id": participant["participant_id"],
            "name": participant["name"],
            "open": sum(task["status"] != "Complete" for task in owned_tasks),
            "complete": sum(task["status"] == "Complete" for task in owned_tasks),
            "blocked": sum(task["status"] == "Blocked" for task in owned_tasks),
        })
    unassigned_tasks = [
        task for task in tasks
        if not task.get("owner_participant_id") or task.get("owner_name") == "Unassigned"
    ]
    dependencies_by_prerequisite: dict[str, list[dict[str, Any]]] = {}
    dependencies_by_dependent: dict[str, list[dict[str, Any]]] = {}
    for dependency in dependencies:
        dependencies_by_prerequisite.setdefault(
            dependency["prerequisite_task_id"], []
        ).append(dependency)
        dependencies_by_dependent.setdefault(
            dependency["dependent_task_id"], []
        ).append(dependency)

    for message in messages:
        message["related_task_id"] = _message_task_id(message)
    messages_by_task: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        if message["related_task_id"]:
            messages_by_task.setdefault(message["related_task_id"], []).append(message)

    open_tasks = [task for task in tasks if task["status"] != "Complete"]
    critical_tasks = sorted(
        (task for task in open_tasks if task["priority"] == "High"),
        key=lambda task: (
            _day_delta(task.get("deadline")) is None,
            _day_delta(task.get("deadline")) or 0,
            task.get("title", "").casefold(),
        ),
    )
    overdue_tasks = [
        task for task in open_tasks
        if (_day_delta(task.get("deadline")) is not None)
        and _day_delta(task.get("deadline")) < 0
    ]
    blocked_tasks = [
        task for task in open_tasks
        if task["status"] == "Blocked" or bool(task.get("blocker_reason"))
    ]
    upcoming_tasks = [
        task for task in open_tasks
        if (_day_delta(task.get("deadline")) is not None)
        and 0 <= _day_delta(task.get("deadline")) <= 7
    ]
    dependency_impacts: dict[str, list[dict[str, Any]]] = {}
    for blocked in blocked_tasks:
        downstream = dependencies_by_prerequisite.get(blocked["task_id"], [])
        if downstream:
            dependency_impacts[blocked["task_id"]] = downstream

    if not tasks:
        health = "AWAITING DATA"
        health_reason = "Create or import project tasks to calculate project health."
    elif overdue_tasks or any(
        task["priority"] == "High"
        and (_day_delta(task.get("deadline")) is not None)
        and _day_delta(task.get("deadline")) <= 2
        for task in blocked_tasks
    ):
        health = "CRITICAL"
        reasons = []
        if overdue_tasks:
            reasons.append(f"{len(overdue_tasks)} open task(s) are overdue")
        urgent_blockers = [
            task for task in blocked_tasks
            if task["priority"] == "High"
            and (_day_delta(task.get("deadline")) is not None)
            and _day_delta(task.get("deadline")) <= 2
        ]
        if urgent_blockers:
            reasons.append(
                f"{len(urgent_blockers)} high-priority blocker(s) are due within two days"
            )
        health_reason = "; ".join(reasons) + "."
    elif blocked_tasks or upcoming_tasks:
        health = "AT RISK"
        if blocked_tasks:
            health_reason = f"{len(blocked_tasks)} task(s) are blocked and need attention."
        else:
            health_reason = f"{len(upcoming_tasks)} open task(s) are due within seven days."
    else:
        health = "ON TRACK"
        health_reason = "No overdue tasks, active blockers, or deadlines within the next seven days."

    priority_rank = {"High": 0, "Medium": 1, "Low": 2}

    def blocker_order(task: dict[str, Any]) -> tuple[int, int, str]:
        delta = _day_delta(task.get("deadline"))
        return (
            priority_rank.get(task["priority"], 3),
            delta if delta is not None else 999999,
            task.get("updated_at") or "",
        )

    blocker = min(blocked_tasks, key=blocker_order) if blocked_tasks else None
    actions: list[dict[str, Any]] = []
    for task in overdue_tasks:
        actions.append({
            "priority": 0,
            "title": f"Review overdue: {task['title']}",
            "reason": f"Deadline {task['deadline']} has passed; status is {task['status']}.",
            "task_id": task["task_id"],
        })
    for task in blocked_tasks:
        actions.append({
            "priority": 1 if task["priority"] == "High" else 2,
            "title": f"Unblock: {task['title']}",
            "reason": task.get("blocker_reason") or "Task is marked Blocked; blocker details are missing.",
            "task_id": task["task_id"],
        })
    for task in upcoming_tasks:
        actions.append({
            "priority": 3,
            "title": f"Check in: {task['title']}",
            "reason": f"Due {task['deadline']}; status is {task['status']}.",
            "task_id": task["task_id"],
        })
    actions.sort(key=lambda action: (action["priority"], action["title"]))

    return {
        "tasks": tasks,
        "participants": participants,
        "workload": workload,
        "unassigned_tasks": unassigned_tasks,
        "task_by_id": task_by_id,
        "messages": messages,
        "messages_by_task": messages_by_task,
        "dependencies": dependencies,
        "dependencies_by_prerequisite": dependencies_by_prerequisite,
        "dependencies_by_dependent": dependencies_by_dependent,
        "dependency_impacts": dependency_impacts,
        "activities": activities[:50],
        "stats": stats,
        "open_tasks": open_tasks,
        "critical_tasks": critical_tasks,
        "overdue_tasks": overdue_tasks,
        "blocked_tasks": blocked_tasks,
        "upcoming_tasks": upcoming_tasks,
        "health": health,
        "health_reason": health_reason,
        "primary_blocker": blocker,
        "next_actions": actions[:8],
        "completion_percent": (
            round(stats["Complete"] / stats["Total"] * 100)
            if stats["Total"] else 0
        ),
        "synced_at": date.today().isoformat(),
    }


def task_schedule_impact(
    snapshot: dict[str, Any], prerequisite_task_id: str
) -> dict[str, Any]:
    pending: list[tuple[str, int]] = [(prerequisite_task_id, 0)]
    seen: set[str] = set()
    affected: list[dict[str, Any]] = []
    while pending:
        current_id, depth = pending.pop(0)
        if current_id in seen:
            continue
        seen.add(current_id)
        for dependency in snapshot["dependencies_by_prerequisite"].get(current_id, []):
            downstream_id = dependency["dependent_task_id"]
            task = snapshot["task_by_id"].get(downstream_id)
            if task and downstream_id not in seen:
                affected.append({**task, "dependency_depth": depth + 1})
                pending.append((downstream_id, depth + 1))
    return {
        "affected_tasks": affected,
        "estimated_delay": None,
        "impact_label": (
            f"{len(affected)} downstream task(s) are recorded; schedule delay is unestimated."
            if affected else "No downstream tasks are recorded for this task."
        ),
    }


def build_standup(snapshot: dict[str, Any]) -> str:
    stats = snapshot["stats"]
    lines = [
        f"Project stand-up — {date.today().isoformat()}",
        f"Status: {snapshot['health']} — {snapshot['health_reason']}",
        (
            f"Tasks: {stats['Complete']} complete, {stats['In Progress']} in progress, "
            f"{stats['Pending']} pending, {stats['Blocked']} blocked, "
            f"{stats['Overdue']} overdue."
        ),
    ]
    if not snapshot["tasks"]:
        lines.append("No task records are available for this stand-up.")
    if snapshot["blocked_tasks"]:
        lines.append("Blockers:")
        for task in snapshot["blocked_tasks"]:
            lines.append(
                f"- {task['title']} ({task['owner_name']}): "
                f"{task.get('blocker_reason') or 'Blocker details not recorded'}"
            )
    if snapshot["upcoming_tasks"]:
        lines.append("Upcoming:")
        for task in snapshot["upcoming_tasks"]:
            lines.append(
                f"- {task['title']} — {task['deadline']} ({task['owner_name']}, {task['status']})"
            )
    if not snapshot["blocked_tasks"] and not snapshot["upcoming_tasks"]:
        lines.append("No blockers or deadlines within the next seven days are recorded.")
    lines.append("Suggested priorities:")
    if snapshot["next_actions"]:
        lines.extend(f"- {action['title']}: {action['reason']}" for action in snapshot["next_actions"])
    else:
        lines.append("- No urgent actions identified from current task records.")
    return "\n".join(lines)
