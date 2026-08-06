"""ADO Task Monitor MCP.

Scans one or more Azure DevOps scopes (project + area + iteration -- see
targets.json), flags work items that are overdue or stale, and emails each
assignee ONE combined list of everything assigned to them across all scopes --
sent as you via delegated Microsoft Graph.

Read-only tools:
  list_targets           show the configured scopes
  test_connection        verify PAT + config; open-item count per target
  list_area_paths        print a project's area-path tree (config discovery)
  current_iteration      show the sprint @current resolves to per target
  scan                   flag overdue/stale items, grouped by assignee (no email)
  get_work_item          one item in full: fields, links, comments, history
  blocked_items          blocked items + WHAT is blocking them
  list_wiki_pages        wiki page hierarchy
  get_wiki_page          one wiki page's markdown
  preview_notifications  build the per-assignee emails (dry run, no send)

Writing tools -- all refuse unless ENABLE_WRITE=true AND confirm=True:
  add_work_item_comment  post a discussion comment
  update_work_item       change state / assignee / dates / fields
  create_work_item       create a new item
  create_or_update_wiki_page   write a wiki page (read-before-write guarded)

Emailing tool -- refuses unless ENABLE_SEND=true AND confirm=True:
  send_notifications     actually send the per-assignee reminders
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

import ado
import mailer
from report import (DEFAULT_STALE_DAYS, TRACKED_PEOPLE, flag_items, is_blocked,
                    render_email)

mcp = FastMCP("ado")

# Master kill-switches. Both default OFF: the server cannot send mail or change
# anything in Azure DevOps until they are explicitly turned on in .env.
ENABLE_SEND = (os.environ.get("ENABLE_SEND", "false").strip().lower() == "true")
ENABLE_WRITE = (os.environ.get("ENABLE_WRITE", "false").strip().lower() == "true")

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


def _write_blocked(confirm: bool, what: str) -> dict | None:
    """Return a refusal dict when writing isn't allowed, else None."""
    if not ENABLE_WRITE:
        return {"written": False, "would_do": what,
                "reason": "ENABLE_WRITE is false -- writing disabled. Set "
                          "ENABLE_WRITE=true in .env to allow it."}
    if not confirm:
        return {"written": False, "would_do": what,
                "reason": "confirm=False -- nothing written. Re-run with "
                          "confirm=True."}
    return None


def _brief(item: dict) -> dict:
    """One-line summary of a work item, for blockers and link targets."""
    f = item.get("fields", {})
    a = f.get("System.AssignedTo") or {}
    project = item.get("_project") or f.get("System.TeamProject", "")
    return {
        "id": item.get("id"),
        "title": f.get("System.Title", ""),
        "type": f.get("System.WorkItemType", ""),
        "state": f.get("System.State", ""),
        "assignee": a.get("displayName") if isinstance(a, dict) else None,
        "url": ado.CONFIG.work_item_url(project, item.get("id")),
    }


# --------------------------------------------------------------------------
# Read: drill down into a single item
# --------------------------------------------------------------------------
@mcp.tool()
def get_work_item(id: int, project: str | None = None,
                  include_comments: bool = True,
                  include_history: bool = False,
                  include_description: bool = False) -> dict:
    """Full detail for ONE work item: fields, links, comments, and history.

    Use this after ``scan`` flags something, to see *why* it stalled --
    ``scan`` only returns a dozen summary fields per item.

    ``include_history`` adds a per-revision changelog (who changed which field
    when), which is what tells you whether an item is genuinely untouched or
    just being nudged without progress. ``include_description`` includes the
    (often long) description and acceptance-criteria HTML.
    """
    proj = project or _target(None).project
    item = ado.get_work_item(proj, id)
    f = item.get("fields", {})
    links = ado.relation_summary(item)

    # Resolve linked work items to titles/states in one batch.
    linked_ids = [l["id"] for l in links if l.get("id")]
    if linked_ids:
        by_id = {b["id"]: b for b in
                 (_brief(i) for i in ado.get_items_expanded(proj, linked_ids,
                                                            expand="none"))}
        for l in links:
            if l.get("id") in by_id:
                l.update({k: v for k, v in by_id[l["id"]].items() if k != "id"})

    assignee = f.get("System.AssignedTo") or {}
    created_by = f.get("System.CreatedBy") or {}
    out = {
        "id": item.get("id"),
        "project": proj,
        "title": f.get("System.Title", ""),
        "type": f.get("System.WorkItemType", ""),
        "state": f.get("System.State", ""),
        "reason": f.get("System.Reason"),
        "assignee": assignee.get("displayName") if isinstance(assignee, dict) else None,
        "assignee_email": (assignee.get("uniqueName")
                           if isinstance(assignee, dict) else None),
        "created_by": (created_by.get("displayName")
                       if isinstance(created_by, dict) else None),
        "created": f.get("System.CreatedDate"),
        "changed": f.get("System.ChangedDate"),
        "area_path": f.get("System.AreaPath"),
        "iteration_path": f.get("System.IterationPath"),
        "tags": f.get("System.Tags") or "",
        "priority": f.get("Microsoft.VSTS.Common.Priority"),
        "due_date": f.get("Microsoft.VSTS.Scheduling.DueDate"),
        "target_date": f.get("Microsoft.VSTS.Scheduling.TargetDate"),
        "start_date": f.get("Microsoft.VSTS.Scheduling.StartDate"),
        "remaining_work": f.get("Microsoft.VSTS.Scheduling.RemainingWork"),
        "original_estimate": f.get("Microsoft.VSTS.Scheduling.OriginalEstimate"),
        "completed_work": f.get("Microsoft.VSTS.Scheduling.CompletedWork"),
        "blocked": is_blocked(f),
        "rev": item.get("rev"),
        "url": ado.CONFIG.work_item_url(proj, item.get("id")),
        "links": links,
        "parent": next((l for l in links if l["link_type"] == "parent"), None),
        "children": [l for l in links if l["link_type"] == "child"],
    }
    if include_description:
        out["description"] = f.get("System.Description")
        out["acceptance_criteria"] = f.get(
            "Microsoft.VSTS.Common.AcceptanceCriteria")
    if include_comments:
        out["comments"] = [
            {"by": (c.get("createdBy") or {}).get("displayName"),
             "on": c.get("createdDate"), "text": c.get("text")}
            for c in ado.get_comments(proj, id)
        ]
    if include_history:
        hist = []
        for rev in ado.get_revisions(proj, id):
            changed = rev.get("fields") or {}
            if not changed:
                continue  # link-only or relation-only revision
            hist.append({
                "rev": rev.get("rev"),
                "by": (rev.get("revisedBy") or {}).get("displayName"),
                "on": (changed.get("System.ChangedDate") or {}).get("newValue")
                      or rev.get("revisedDate"),
                "changes": {k: {"from": v.get("oldValue"), "to": v.get("newValue")}
                            for k, v in changed.items()
                            if k not in ("System.Rev", "System.ChangedDate",
                                         "System.AuthorizedDate",
                                         "System.RevisedDate",
                                         "System.Watermark",
                                         "System.ChangedBy",
                                         "System.AuthorizedAs",
                                         "System.PersonId")},
            })
        out["history"] = hist
    return out


@mcp.tool()
def blocked_items(sprint: str = "@current", include_dependencies: bool = True,
                  max_items: int = 1200) -> dict:
    """List blocked items and, where knowable, WHAT is blocking them.

    Two kinds of blockage are reported:

    * **declared** -- the item's state is Blocked/On Hold, a process Blocked
      field is set, or it carries a "blocked" tag.
    * **dependency** -- the item has a *predecessor* link to an item that is
      not finished yet, so it cannot proceed regardless of its own state.

    Useful before sending reminders: an item that is stale because someone
    else's predecessor is open shouldn't be chased with its assignee.
    ``sprint`` accepts the same values as ``scan``.

    Candidates are capped at ``max_items`` per project; when the cap bites,
    ``truncated`` comes back true and the counts are floors, not totals.
    """
    scoped = []
    for t in ado.TARGETS:
        path, _ = ado.resolve_sprint(t.project, sprint)
        scoped.append(ado.Target({
            "name": t.name, "project": t.project, "area_path": t.area_path,
            "iteration_path": path, "work_item_types": t.work_item_types,
            "exclude_states": t.exclude_states,
        }))

    # Group ids by project, then batch-fetch WITH relations.
    ids_by_project: dict[str, list[int]] = {}
    for t in scoped:
        ids_by_project.setdefault(t.project, []).extend(ado.query_ids(t))

    rows, truncated = [], False
    for proj, ids in ids_by_project.items():
        uniq = list(dict.fromkeys(ids))
        if len(uniq) > max_items:
            uniq, truncated = uniq[:max_items], True
        items = ado.get_items_expanded(proj, uniq) if uniq else []

        # Resolve every predecessor in one batch per project.
        pred_ids: set[int] = set()
        if include_dependencies:
            for it in items:
                for l in ado.relation_summary(it):
                    if l["link_type"] == "predecessor" and l.get("id"):
                        pred_ids.add(l["id"])
        preds = {}
        if pred_ids:
            preds = {i.get("id"): i for i in
                     ado.get_items_expanded(proj, sorted(pred_ids),
                                            expand="none")}

        for it in items:
            f = it.get("fields", {})
            declared = is_blocked(f)
            open_preds = []
            if include_dependencies:
                for l in ado.relation_summary(it):
                    if l["link_type"] != "predecessor" or not l.get("id"):
                        continue
                    p = preds.get(l["id"])
                    if not p:
                        continue
                    pstate = (p.get("fields", {})
                               .get("System.State", "") or "").strip().lower()
                    if pstate not in ado.DONE_STATES:
                        open_preds.append(_brief(p))
            if not declared and not open_preds:
                continue
            row = _brief(it)
            row.update({
                "area_path": f.get("System.AreaPath"),
                "sprint": (f.get("System.IterationPath") or "").split("\\")[-1],
                "tags": f.get("System.Tags") or "",
                "changed": f.get("System.ChangedDate"),
                "blocked_declared": declared,
                "blocked_by": open_preds,
            })
            rows.append(row)

    rows.sort(key=lambda r: (not r["blocked_declared"], r["id"]))
    return {
        "sprint_requested": sprint,
        "blocked_count": len(rows),
        "declared_count": sum(1 for r in rows if r["blocked_declared"]),
        "dependency_count": sum(1 for r in rows if r["blocked_by"]),
        "truncated": truncated,
        "max_items": max_items,
        "items": rows,
    }


# --------------------------------------------------------------------------
# Wiki
# --------------------------------------------------------------------------
def _wiki_ctx(project: str | None, wiki: str | None) -> tuple[str, dict]:
    proj = project or _target(None).project
    return proj, ado.resolve_wiki(proj, wiki)


def _flatten_pages(node: dict, out: list, depth: int = 0) -> list:
    if node.get("path") is not None:
        out.append({"path": node.get("path"), "depth": depth,
                    "is_folder": bool(node.get("isParentPage")),
                    "url": node.get("remoteUrl")})
    for child in node.get("subPages") or []:
        _flatten_pages(child, out, depth + 1)
    return out


@mcp.tool()
def list_wiki_pages(project: str | None = None, wiki: str | None = None,
                    path: str = "/", depth: str = "full") -> dict:
    """List the wiki's page hierarchy (``depth``: none | oneLevel | full)."""
    proj, w = _wiki_ctx(project, wiki)
    tree = ado.wiki_page_tree(proj, w["id"], path, depth)
    if tree is None:
        return {"project": proj, "wiki": w.get("name"), "path": path,
                "error": f"No page found at '{path}'."}
    pages = _flatten_pages(tree, [])
    return {"project": proj, "wiki": w.get("name"), "wiki_id": w["id"],
            "root": path, "page_count": len(pages), "pages": pages}


@mcp.tool()
def get_wiki_page(path: str, project: str | None = None,
                  wiki: str | None = None) -> dict:
    """Read one wiki page's markdown.

    Path spellings are forgiving -- ``/My Page`` and ``/My-Page`` both resolve.
    The returned ``etag`` is what an update needs to avoid clobbering someone
    else's concurrent edit.
    """
    proj, w = _wiki_ctx(project, wiki)
    page = ado.get_wiki_page(proj, w["id"], path)
    if page is None:
        return {"project": proj, "wiki": w.get("name"), "path": path,
                "exists": False}
    content = page.get("content") or ""
    return {"project": proj, "wiki": w.get("name"), "wiki_id": w["id"],
            "path": page.get("path"), "resolved_path": page.get("resolved_path"),
            "exists": True, "etag": page.get("etag"),
            "char_count": len(content), "url": page.get("remoteUrl"),
            "content": content}


@mcp.tool()
def create_or_update_wiki_page(path: str, content: str,
                               project: str | None = None,
                               wiki: str | None = None,
                               confirm: bool = False,
                               allow_overwrite: bool = False) -> dict:
    """Create or overwrite a wiki page. Requires ENABLE_WRITE=true AND confirm=True.

    This always reads the page first and reports what would change. Overwriting
    an EXISTING page additionally requires ``allow_overwrite=True``, because a
    wiki page written by hand can hold work that isn't reproducible from
    anywhere else -- the whole page content is replaced, not merged.
    """
    proj, w = _wiki_ctx(project, wiki)
    existing = ado.get_wiki_page(proj, w["id"], path)
    exists = existing is not None
    old = (existing or {}).get("content") or ""
    target_path = (existing or {}).get("resolved_path") or (
        "/" + path.strip().lstrip("/"))

    plan = {
        "project": proj, "wiki": w.get("name"), "path": target_path,
        "action": "update" if exists else "create",
        "existing_chars": len(old) if exists else 0,
        "new_chars": len(content),
        "unchanged": exists and old == content,
    }
    if plan["unchanged"]:
        return {**plan, "written": False,
                "reason": "Content is byte-identical to the live page -- "
                          "nothing to do."}
    if exists and not allow_overwrite:
        return {**plan, "written": False,
                "reason": f"Page exists with {len(old)} chars and would be "
                          "REPLACED. Re-run with allow_overwrite=True if that "
                          "is intended.",
                "existing_content": old}

    refusal = _write_blocked(confirm, f"{plan['action']} wiki page {target_path}")
    if refusal:
        return {**plan, **refusal}

    result = ado.upsert_wiki_page(proj, w["id"], target_path, content,
                                  etag=(existing or {}).get("etag"))
    return {**plan, "written": True, "created": result["created"],
            "url": (result.get("page") or {}).get("remoteUrl"),
            "replaced_content": old if exists else None}


# --------------------------------------------------------------------------
# Work-item writes
# --------------------------------------------------------------------------
@mcp.tool()
def add_work_item_comment(id: int, text: str, project: str | None = None,
                          confirm: bool = False) -> dict:
    """Post a discussion comment on a work item (changes no field).

    Requires ENABLE_WRITE=true AND confirm=True.
    """
    proj = project or _target(None).project
    refusal = _write_blocked(confirm, f"comment on #{id}: {text[:80]}")
    if refusal:
        return {**refusal, "id": id, "project": proj}
    c = ado.add_comment(proj, id, text)
    return {"written": True, "id": id, "project": proj,
            "comment_id": c.get("id"),
            "url": ado.CONFIG.work_item_url(proj, id)}


@mcp.tool()
def update_work_item(id: int, project: str | None = None,
                     state: str | None = None,
                     assigned_to: str | None = None,
                     title: str | None = None,
                     due_date: str | None = None,
                     iteration_path: str | None = None,
                     area_path: str | None = None,
                     tags: str | None = None,
                     comment: str | None = None,
                     fields: dict | None = None,
                     confirm: bool = False,
                     validate_only: bool = False) -> dict:
    """Change fields on an existing work item.

    Only the arguments you pass are touched. ``assigned_to`` takes an email;
    ``due_date`` an ISO date (YYYY-MM-DD). ``fields`` sets any other field by
    reference name, e.g. ``{"Microsoft.VSTS.Common.Priority": 2}``. ``comment``
    adds a note to the discussion in the same revision.

    ``validate_only=True`` asks Azure DevOps to validate the change and save
    NOTHING -- a safe dry run. Otherwise requires ENABLE_WRITE=true AND
    confirm=True.
    """
    proj = project or _target(None).project
    named = {
        "System.State": state,
        "System.AssignedTo": assigned_to,
        "System.Title": title,
        "Microsoft.VSTS.Scheduling.DueDate": due_date,
        "System.IterationPath": iteration_path,
        "System.AreaPath": area_path,
        "System.Tags": tags,
        "System.History": comment,
    }
    patch = {k: v for k, v in named.items() if v is not None}
    patch.update(fields or {})
    if not patch:
        return {"written": False, "id": id,
                "reason": "Nothing to change -- pass at least one field."}

    ops = [{"op": "add", "path": f"/fields/{k}", "value": v}
           for k, v in patch.items()]
    plan = {"id": id, "project": proj, "changes": patch,
            "url": ado.CONFIG.work_item_url(proj, id)}

    if validate_only:
        ado.patch_work_item(proj, id, ops, validate_only=True)
        return {**plan, "written": False, "validated": True,
                "reason": "validate_only=True -- Azure DevOps accepted the "
                          "change but saved nothing."}
    refusal = _write_blocked(confirm, f"update #{id}: {patch}")
    if refusal:
        return {**plan, **refusal}

    item = ado.patch_work_item(proj, id, ops)
    return {**plan, "written": True, "rev": item.get("rev"),
            "state": item.get("fields", {}).get("System.State")}


@mcp.tool()
def create_work_item(title: str, type: str = "Task",
                     project: str | None = None,
                     assigned_to: str | None = None,
                     description: str | None = None,
                     area_path: str | None = None,
                     iteration_path: str | None = None,
                     due_date: str | None = None,
                     tags: str | None = None,
                     parent_id: int | None = None,
                     fields: dict | None = None,
                     confirm: bool = False,
                     validate_only: bool = False) -> dict:
    """Create a work item.

    Defaults ``area_path`` to the first target's area path so new items land in
    the scope this server monitors. ``parent_id`` links the new item as a child
    of an existing one.

    ``validate_only=True`` validates and creates NOTHING -- a safe dry run.
    Otherwise requires ENABLE_WRITE=true AND confirm=True.
    """
    t = _target(None)
    proj = project or t.project
    named = {
        "System.Title": title,
        "System.AssignedTo": assigned_to,
        "System.Description": description,
        "System.AreaPath": area_path or t.area_path or None,
        "System.IterationPath": iteration_path,
        "Microsoft.VSTS.Scheduling.DueDate": due_date,
        "System.Tags": tags,
    }
    patch = {k: v for k, v in named.items() if v is not None}
    patch.update(fields or {})
    ops = [{"op": "add", "path": f"/fields/{k}", "value": v}
           for k, v in patch.items()]
    if parent_id:
        ops.append({"op": "add", "path": "/relations/-", "value": {
            "rel": "System.LinkTypes.Hierarchy-Reverse",
            "url": f"{ado.CONFIG.org_url}/_apis/wit/workItems/{int(parent_id)}",
        }})

    plan = {"project": proj, "type": type, "fields": patch,
            "parent_id": parent_id}
    if validate_only:
        ado.create_work_item(proj, type, ops, validate_only=True)
        return {**plan, "written": False, "validated": True,
                "reason": "validate_only=True -- Azure DevOps accepted the "
                          "item but created nothing."}
    refusal = _write_blocked(confirm, f"create {type} '{title}' in {proj}")
    if refusal:
        return {**plan, **refusal}

    item = ado.create_work_item(proj, type, ops)
    return {**plan, "written": True, "id": item.get("id"),
            "url": ado.CONFIG.work_item_url(proj, item.get("id"))}


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
