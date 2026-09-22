"""Flagging logic for the Azure DevOps MCP.

An open work item is *flagged* when it is either:
  * OVERDUE  - its Due Date or Target Date is strictly before today (UTC), or
  * STALE    - its last ChangedDate is older than STALE_DAYS days.

Flagged items are grouped by assignee (across ALL targets), so one person's
whole list arrives together rather than split per scope. Items with no assignee
are grouped under the sentinel ``None`` for the caller to handle.

The HTML e-mail renderer that used to live here was removed 2026-08-12 along
with the notification tools; this module is now pure flagging/grouping.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from ado import CONFIG

DEFAULT_STALE_DAYS = int(os.environ.get("STALE_DAYS", "7"))

# Optional allow-list of people to track (emails, CSV in .env). When non-empty,
# only items assigned to these people are flagged; everyone else (and
# unassigned items) is ignored. Empty = track everyone.
TRACKED_PEOPLE = {e.strip().lower()
                  for e in (os.environ.get("TRACK_PEOPLE") or "").split(",")
                  if e.strip()}


# A work item counts as blocked when its state says so, a process-specific
# Blocked field is set, or it carries a "blocked" tag. Checked against fields
# already fetched, so this costs no extra API call.
BLOCKED_STATES = {"blocked", "on hold", "waiting"}
BLOCKED_FIELDS = ("Microsoft.VSTS.CMMI.Blocked", "Microsoft.VSTS.Common.Blocked")


def is_blocked(fields: dict[str, Any]) -> bool:
    if (fields.get("System.State") or "").strip().lower() in BLOCKED_STATES:
        return True
    for key in BLOCKED_FIELDS:
        if str(fields.get(key) or "").strip().lower() in {"yes", "true", "1"}:
            return True
    return "blocked" in (fields.get("System.Tags") or "").lower()


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _assignee(item: dict[str, Any]) -> tuple[Optional[str], str]:
    a = item.get("fields", {}).get("System.AssignedTo")
    if isinstance(a, dict):
        email = a.get("uniqueName") or a.get("mail")
        return (email.lower() if email else None), a.get("displayName", "Unassigned")
    return None, "Unassigned"


def flag_items(items: list[dict[str, Any]], stale_days: int,
               now: Optional[datetime] = None,
               tracked: Optional[set[str]] = None,
               include_all: bool = False) -> dict[Optional[str], dict[str, Any]]:
    """Group items by assignee email across all projects/areas.

    If ``tracked`` (an allow-list of lowercased emails) is non-empty, only
    items assigned to those people are considered. Defaults to TRACKED_PEOPLE.

    By default only *flagged* items (overdue or stale) are returned. Set
    ``include_all=True`` to return every open item -- non-flagged items are
    included with an empty ``reasons`` list -- so callers can list an entire
    sprint, not just the items needing attention.
    """
    now = now or datetime.now(timezone.utc)
    today = now.date()
    track = TRACKED_PEOPLE if tracked is None else tracked
    groups: dict[Optional[str], dict[str, Any]] = {}

    for it in items:
        f = it.get("fields", {})

        # Apply the people allow-list before doing any date work.
        if track:
            assignee_email, _ = _assignee(it)
            if assignee_email is None or assignee_email not in track:
                continue

        reasons: list[str] = []

        due = _parse(f.get("Microsoft.VSTS.Scheduling.DueDate"))
        target = _parse(f.get("Microsoft.VSTS.Scheduling.TargetDate"))
        due_ref = due or target
        if due_ref and due_ref.date() < today:
            label = "Due Date" if due else "Target Date"
            reasons.append(f"Overdue ({label}: {due_ref.date().isoformat()})")

        changed = _parse(f.get("System.ChangedDate"))
        days_since = (now - changed).days if changed else None
        if days_since is not None and days_since >= stale_days:
            reasons.append(f"Not updated in {days_since} days")

        if not reasons and not include_all:
            continue

        project = it.get("_project") or f.get("System.TeamProject", "")
        area = f.get("System.AreaPath", "") or ""
        email, name = _assignee(it)
        grp = groups.setdefault(email, {"display_name": name, "items": []})
        grp["items"].append({
            "id": it.get("id"),
            "title": f.get("System.Title", ""),
            "type": f.get("System.WorkItemType", ""),
            "state": f.get("System.State", ""),
            "assignee": name,
            "assignee_email": email,
            "project": project,
            "area_path": area,
            "area": area.split("\\")[-1] if area else "",
            "iteration": f.get("System.IterationPath", ""),
            "sprint": (f.get("System.IterationPath", "") or "").split("\\")[-1],
            "url": CONFIG.work_item_url(project, it.get("id")),
            "due": due_ref.date().isoformat() if due_ref else None,
            "days_since_update": days_since,
            "tags": f.get("System.Tags") or "",
            # Informational only -- deliberately NOT a flag reason, so an item
            # being blocked doesn't by itself make it show up as needing action.
            "blocked": is_blocked(f),
            "reasons": reasons,
        })

    for grp in groups.values():
        grp["items"].sort(
            key=lambda r: (0 if any("Overdue" in x for x in r["reasons"]) else 1,
                           -(r["days_since_update"] or 0)))
    return groups
