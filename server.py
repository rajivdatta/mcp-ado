"""ADO Task Monitor MCP.

Scans one or more Azure DevOps scopes (project + area + iteration -- see
targets.json), flags work items that are overdue or stale, and emails each
assignee ONE combined list of everything assigned to them across all scopes --
sent as you via delegated Microsoft Graph.

Tools:
  list_targets           show the configured scopes
  test_connection        verify PAT + config; open-item count per target
  list_area_paths        print a project's area-path tree (config discovery)
  current_iteration      show the sprint @current resolves to per target
  scan                   flag overdue/stale items, grouped by assignee (no email)
  preview_notifications  build the per-assignee emails (dry run, no send)
  send_notifications     actually send (requires ENABLE_SEND=true AND confirm=True)
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

import ado
import mailer
from report import DEFAULT_STALE_DAYS, TRACKED_PEOPLE, flag_items, render_email

mcp = FastMCP("ado")

# Master kill-switch. Sending is DISABLED unless this is explicitly "true".
ENABLE_SEND = (os.environ.get("ENABLE_SEND", "false").strip().lower() == "true")

SUBJECT_PREFIX = os.environ.get("MAIL_SUBJECT_PREFIX", "[ADO Reminder]")
FALLBACK_TO = os.environ.get("MAIL_FALLBACK_TO", "").strip()
CC = [c.strip() for c in (os.environ.get("MAIL_CC") or "").split(",") if c.strip()]


def _flatten(nodes):
    out = [nodes.get("path") or nodes.get("name")]
    for child in nodes.get("children", []) or []:
        out.extend(_flatten(child))
    return out


def _target(name: str | None) -> ado.Target:
    """Resolve a target by name, defaulting to the first configured one."""
    if name:
        for t in ado.TARGETS:
            if t.name.lower() == name.lower():
                return t
        raise ValueError(f"Unknown target '{name}'. Known: "
                         f"{[t.name for t in ado.TARGETS]}")
    if not ado.TARGETS:
        raise ValueError("No targets configured (targets.json / ADO_PROJECT).")
    return ado.TARGETS[0]


@mcp.tool()
def list_targets() -> dict:
    """List the configured scopes (project / area / iteration) being monitored."""
    return {"org": ado.CONFIG.org,
            "targets": [t.as_dict() for t in ado.TARGETS]}


@mcp.tool()
def test_connection() -> dict:
    """Verify config + PAT and return the open-item count for each target."""
    missing = ado.missing_global()
    if missing:
        return {"ok": False, "error": f"Missing config: {', '.join(missing)}"}
    results = []
    for t in ado.TARGETS:
        d = t.as_dict()
        d["iteration_resolved"] = ado.effective_iteration_path(t) or "(all iterations)"
        d["open_item_count"] = len(ado.query_ids(t))
        results.append(d)
    return {"ok": True, "org": ado.CONFIG.org, "targets": results}


@mcp.tool()
def list_area_paths(project: str | None = None) -> dict:
    """List a project's area paths (defaults to the first target's project)."""
    proj = project or _target(None).project
    return {"project": proj, "area_paths": _flatten(ado.list_area_paths(proj))}


@mcp.tool()
def current_iteration(target: str | None = None) -> dict:
    """Show the sprint @current resolves to for each target (based on today)."""
    out = []
    for t in ([_target(target)] if target else ado.TARGETS):
        out.append({
            "target": t.name, "project": t.project,
            "config": t.iteration_path or "(none)",
            "current_sprint": ado.resolve_current_iteration(t.project)
                              or "(no iteration covers today)",
        })
    return {"targets": out}


@mcp.tool()
def scan(days_stale: int | None = None, only_flagged: bool = False,
         sprint: str = "@current") -> dict:
    """List open work items across ALL targets, grouped by assignee. No email.

    By default returns EVERY open item in scope (each still annotated with
    whether it is overdue/stale in its ``reasons``). Set ``only_flagged=True``
    to return just the overdue/stale items (the old reminder behaviour).

    ``sprint`` overrides each target's configured iteration so you are no
    longer limited to the current sprint. Accepts:
      * ``@current`` (default) - the sprint covering today
      * ``@next``              - the sprint after the current one
      * ``@all``               - every sprint (no iteration filter)
      * a sprint name          - e.g. ``Q2FY27 Sprint 1`` (exact or partial)
    """
    stale = days_stale if days_stale is not None else DEFAULT_STALE_DAYS

    # Build per-target copies with the requested iteration substituted in.
    scoped, resolved = [], []
    for t in ado.TARGETS:
        path, label = ado.resolve_sprint(t.project, sprint)
        scoped.append(ado.Target({
            "name": t.name, "project": t.project,
            "area_path": t.area_path,
            "iteration_path": path,  # concrete path, or "" for all iterations
            "work_item_types": t.work_item_types,
            "exclude_states": t.exclude_states,
        }))
        resolved.append({"target": t.name, "sprint": label,
                         "iteration_path": path or "(all iterations)"})

    items = ado.collect_items(scoped)
    groups = flag_items(items, stale, include_all=not only_flagged)
    total = sum(len(g["items"]) for g in groups.values())
    flagged = sum(1 for g in groups.values()
                  for r in g["items"] if r["reasons"])
    return {
        "stale_days": stale,
        "only_flagged": only_flagged,
        "sprint_requested": sprint,
        "sprint_resolved": resolved,
        "targets": [t.name for t in ado.TARGETS],
        "tracked_people": sorted(TRACKED_PEOPLE) or "(everyone)",
        "open_items_scanned": len(items),
        "returned_total": total,
        "flagged_total": flagged,
        "assignees": [
            {"email": email or "(unassigned)",
             "display_name": g["display_name"],
             "item_count": len(g["items"]),
             "flagged_count": sum(1 for r in g["items"] if r["reasons"]),
             "items": g["items"]}
            for email, g in sorted(groups.items(), key=lambda kv: str(kv[0]))
        ],
    }


def _subject(email, g):
    if email is None:
        return f"{SUBJECT_PREFIX} {len(g['items'])} UNASSIGNED work item(s)"
    return f"{SUBJECT_PREFIX} {len(g['items'])} work item(s) need your attention"


@mcp.tool()
def preview_notifications(days_stale: int | None = None) -> dict:
    """Build the per-assignee reminder emails WITHOUT sending."""
    stale = days_stale if days_stale is not None else DEFAULT_STALE_DAYS
    groups = flag_items(ado.collect_items(), stale)
    previews = []
    for email, g in groups.items():
        previews.append({
            "to": (email or FALLBACK_TO) or "(no recipient - set MAIL_FALLBACK_TO)",
            "assignee": g["display_name"],
            "unassigned": email is None,
            "item_count": len(g["items"]),
            "subject": _subject(email, g),
            "cc": CC,
            "html": render_email(g["display_name"], g["items"], stale),
        })
    return {"stale_days": stale, "email_count": len(previews), "emails": previews}


@mcp.tool()
def send_notifications(days_stale: int | None = None,
                       confirm: bool = False,
                       skip_unassigned: bool = True) -> dict:
    """Send reminder emails to each assignee (as you, via Graph).

    Sends ONLY when ENABLE_SEND=true in .env AND confirm=True. Otherwise
    returns what would be sent.
    """
    stale = days_stale if days_stale is not None else DEFAULT_STALE_DAYS
    groups = flag_items(ado.collect_items(), stale)

    planned = []
    for email, g in groups.items():
        if email is None and skip_unassigned:
            continue
        to = email or FALLBACK_TO
        if not to:
            continue
        planned.append((to, _subject(email, g), g))

    if not ENABLE_SEND:
        return {"sent": False,
                "reason": "ENABLE_SEND is false -- sending disabled. Set "
                          "ENABLE_SEND=true in .env to allow sending.",
                "would_send_count": len(planned),
                "recipients": [p[0] for p in planned]}
    if not confirm:
        return {"sent": False,
                "reason": "confirm=False -- nothing sent. Re-run with confirm=True.",
                "would_send_count": len(planned),
                "recipients": [p[0] for p in planned]}

    results = []
    for to, subj, g in planned:
        try:
            mailer.send_mail(to=[to], subject=subj,
                             html=render_email(g["display_name"], g["items"], stale),
                             cc=CC)
            results.append({"to": to, "status": "sent", "items": len(g["items"])})
        except Exception as e:  # noqa: BLE001
            results.append({"to": to, "status": "failed", "error": str(e)})
    return {"sent": True, "stale_days": stale, "results": results}


if __name__ == "__main__":
    mcp.run()
