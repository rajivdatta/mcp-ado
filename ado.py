"""Azure DevOps (ADO) REST client for the ADO task-monitor MCP.

Auth: Personal Access Token (PAT) via HTTP Basic (``:<pat>`` base64-encoded),
loaded from ``.env`` (never inlined). Only Work Items *Read* scope is needed.

Scope is a LIST of targets (see ``targets.json``). Each target names a project
plus an optional area path, iteration path, work-item types and excluded
states. The org URL and PAT are global (one org, one token). If no
``targets.json`` exists, a single target is built from the ADO_* env vars for
backward compatibility.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
from datetime import date, datetime, timezone
from typing import Any, Optional

import requests
from dotenv import load_dotenv

_HERE = pathlib.Path(__file__).parent
load_dotenv(_HERE / ".env")

API_VERSION = "7.1"

FIELDS = [
    "System.Id",
    "System.Title",
    "System.WorkItemType",
    "System.State",
    "System.AssignedTo",
    "System.ChangedDate",
    "System.CreatedDate",
    "System.TeamProject",
    "System.AreaPath",
    "System.IterationPath",
    "Microsoft.VSTS.Scheduling.DueDate",
    "Microsoft.VSTS.Scheduling.TargetDate",
]

DEFAULT_EXCLUDE_STATES = "Closed,Done,Removed,Resolved,Completed"


def _csv(val: str | None) -> list[str]:
    return [x.strip() for x in (val or "").split(",") if x.strip()]


class GlobalConfig:
    """Org-wide settings shared by every target."""

    def __init__(self) -> None:
        self.org_url = (os.environ.get("ADO_ORG_URL") or "").rstrip("/")
        self.pat = os.environ.get("ADO_PAT") or ""
        self.default_types = _csv(os.environ.get("ADO_WORK_ITEM_TYPES"))
        self.default_exclude = _csv(
            os.environ.get("ADO_EXCLUDE_STATES") or DEFAULT_EXCLUDE_STATES)

    @property
    def org(self) -> str:
        return self.org_url.rsplit("/", 1)[-1] if self.org_url else ""

    def work_item_url(self, project: str, wid: int) -> str:
        proj = requests.utils.quote(project)
        return f"{self.org_url}/{proj}/_workitems/edit/{wid}"


CONFIG = GlobalConfig()


class Target:
    """A single project/area/iteration scope to scan."""

    def __init__(self, d: dict[str, Any]) -> None:
        self.project = d["project"]
        self.name = d.get("name") or self.project
        self.area_path = d.get("area_path", "") or ""
        self.iteration_path = d.get("iteration_path", "") or ""
        self.work_item_types = d.get("work_item_types") or CONFIG.default_types
        self.exclude_states = d.get("exclude_states") or CONFIG.default_exclude

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "project": self.project,
            "area_path": self.area_path or "(entire project)",
            "iteration_path": self.iteration_path or "(all iterations)",
            "work_item_types": self.work_item_types or "(all)",
            "exclude_states": self.exclude_states,
        }


def _load_targets() -> list[Target]:
    tj = _HERE / "targets.json"
    if tj.exists():
        data = json.loads(tj.read_text(encoding="utf-8"))
        return [Target(d) for d in data]
    # Backward-compatible single target from env.
    proj = os.environ.get("ADO_PROJECT")
    if proj:
        return [Target({
            "name": proj, "project": proj,
            "area_path": os.environ.get("ADO_AREA_PATH", ""),
            "iteration_path": os.environ.get("ADO_ITERATION_PATH", ""),
        })]
    return []


TARGETS = _load_targets()


def missing_global() -> list[str]:
    need = {"ADO_ORG_URL": CONFIG.org_url, "ADO_PAT": CONFIG.pat}
    out = [k for k, v in need.items() if not v]
    if not TARGETS:
        out.append("targets (targets.json or ADO_PROJECT)")
    return out


def _auth_header() -> dict[str, str]:
    token = base64.b64encode(f":{CONFIG.pat}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _wit_base(project: str) -> str:
    return f"{CONFIG.org_url}/{requests.utils.quote(project)}/_apis/wit"


# --------------------------------------------------------------------------
# WIQL
# --------------------------------------------------------------------------
def _build_wiql(target: Target) -> str:
    clauses = [f"[System.TeamProject] = '{target.project}'"]
    if target.area_path:
        clauses.append(f"[System.AreaPath] UNDER '{target.area_path}'")
    iteration = effective_iteration_path(target)
    if iteration:
        clauses.append(f"[System.IterationPath] UNDER '{iteration}'")
    if target.work_item_types:
        joined = ", ".join(f"'{t}'" for t in target.work_item_types)
        clauses.append(f"[System.WorkItemType] IN ({joined})")
    if target.exclude_states:
        joined = ", ".join(f"'{s}'" for s in target.exclude_states)
        clauses.append(f"[System.State] NOT IN ({joined})")
    where = "\n  AND ".join(clauses)
    return ("SELECT [System.Id] FROM WorkItems\n"
            f"WHERE {where}\n"
            "ORDER BY [System.ChangedDate] ASC")


def query_ids(target: Target) -> list[int]:
    url = f"{_wit_base(target.project)}/wiql?api-version={API_VERSION}"
    resp = requests.post(
        url, timeout=60,
        headers={**_auth_header(), "Content-Type": "application/json"},
        json={"query": _build_wiql(target)})
    resp.raise_for_status()
    return [wi["id"] for wi in resp.json().get("workItems", [])]


def get_items(project: str, ids: list[int]) -> list[dict[str, Any]]:
    """Batch-fetch fields (200/req). Items are stamped with their project."""
    out: list[dict[str, Any]] = []
    url = f"{_wit_base(project)}/workitemsbatch?api-version={API_VERSION}"
    for i in range(0, len(ids), 200):
        resp = requests.post(
            url, timeout=60,
            headers={**_auth_header(), "Content-Type": "application/json"},
            json={"ids": ids[i:i + 200], "fields": FIELDS})
        resp.raise_for_status()
        for it in resp.json().get("value", []):
            it["_project"] = project
            out.append(it)
    return out


def collect_items(targets: Optional[list[Target]] = None) -> list[dict[str, Any]]:
    """Run every target and return the de-duplicated union of open items."""
    seen: dict[int, dict[str, Any]] = {}
    for t in (targets or TARGETS):
        for it in get_items(t.project, query_ids(t)):
            seen.setdefault(it["id"], it)  # first target wins on overlap
    return list(seen.values())


# --------------------------------------------------------------------------
# Iteration resolution (auto-detect the current sprint from today's date)
# --------------------------------------------------------------------------
_CURRENT_SENTINELS = {"@current", "current", "@currentiteration"}


def _iterations_tree(project: str, depth: int = 10) -> dict[str, Any]:
    url = (f"{_wit_base(project)}/classificationnodes/iterations"
           f"?$depth={depth}&api-version={API_VERSION}")
    resp = requests.get(url, timeout=60, headers=_auth_header())
    resp.raise_for_status()
    return resp.json()


def _walk(node: dict[str, Any]):
    yield node
    for child in node.get("children", []) or []:
        yield from _walk(child)


def _to_wiql_path(api_path: str) -> str:
    parts = [p for p in api_path.split("\\") if p]
    if len(parts) >= 2 and parts[1] == "Iteration":
        parts = [parts[0]] + parts[2:]
    return "\\".join(parts)


def resolve_current_iteration(project: str,
                              today: Optional[date] = None) -> Optional[str]:
    """WIQL path of the most-specific iteration whose dates contain today."""
    today = today or datetime.now(timezone.utc).date()
    candidates = []
    for node in _walk(_iterations_tree(project)):
        attrs = node.get("attributes") or {}
        start, finish = attrs.get("startDate"), attrs.get("finishDate")
        if not start or not finish:
            continue
        sd = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
        fd = datetime.fromisoformat(finish.replace("Z", "+00:00")).date()
        if sd <= today <= fd:
            is_leaf = not node.get("children")
            candidates.append((0 if is_leaf else 1, (fd - sd).days, node))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return _to_wiql_path(candidates[0][2].get("path", ""))


def effective_iteration_path(target: Target) -> str:
    ip = (target.iteration_path or "").strip()
    if ip.lower() in _CURRENT_SENTINELS:
        return resolve_current_iteration(target.project) or ""
    return ip


# --------------------------------------------------------------------------
# Arbitrary-sprint resolution (lets callers scan any sprint, not just @current)
# --------------------------------------------------------------------------
_NEXT_SENTINELS = {"@next", "next", "@nextiteration"}
_ALL_SENTINELS = {"@all", "all", "*", "any", "everything"}


def _sprint_leaves(project: str, max_days: int = 45) -> list[dict[str, Any]]:
    """Leaf iterations that look like sprints (short), sorted by start date.

    Filters out the year-long 'Major Deliverables / Epics' buckets so
    'current' and 'next' track the real sprint cadence.
    """
    out: list[dict[str, Any]] = []
    for node in _walk(_iterations_tree(project)):
        attrs = node.get("attributes") or {}
        s, f = attrs.get("startDate"), attrs.get("finishDate")
        if not s or not f or node.get("children"):
            continue
        sd = datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        fd = datetime.fromisoformat(f.replace("Z", "+00:00")).date()
        if (fd - sd).days <= max_days:
            out.append({"sd": sd, "fd": fd, "name": node.get("name"),
                        "wiql": _to_wiql_path(node.get("path", ""))})
    out.sort(key=lambda l: l["sd"])
    return out


def resolve_next_iteration(project: str,
                           today: Optional[date] = None) -> Optional[str]:
    """WIQL path of the first sprint that starts after the current one."""
    today = today or datetime.now(timezone.utc).date()
    sprints = _sprint_leaves(project)
    cur = resolve_current_iteration(project, today)
    cur_leaf = next((l for l in sprints if l["wiql"] == cur), None)
    ref = cur_leaf["sd"] if cur_leaf else today
    nxt = next((l for l in sprints if l["sd"] > ref), None)
    return nxt["wiql"] if nxt else None


def find_iteration_by_name(project: str, name: str) -> Optional[str]:
    """WIQL path of an iteration matched by name (exact first, then substring)."""
    key = (name or "").strip().lower()
    if not key:
        return None
    nodes = list(_walk(_iterations_tree(project)))
    for node in nodes:
        if (node.get("name") or "").lower() == key:
            return _to_wiql_path(node.get("path", ""))
    for node in nodes:
        if key in (node.get("name") or "").lower():
            return _to_wiql_path(node.get("path", ""))
    return None


def resolve_sprint(project: str, sprint: Optional[str]) -> tuple[str, str]:
    """Resolve a caller's ``sprint`` argument to an (iteration_path, label).

    Accepts ``@current`` (default), ``@next``, ``@all`` (no iteration filter),
    or a sprint name/path. An empty returned path means 'do not filter by
    iteration' (scan every sprint).
    """
    key = (sprint or "@current").strip().lower()
    if key in _ALL_SENTINELS:
        return "", "(all iterations)"
    if key in _CURRENT_SENTINELS:
        p = resolve_current_iteration(project)
        return (p or ""), (p.split("\\")[-1] if p else "(no current sprint)")
    if key in _NEXT_SENTINELS:
        p = resolve_next_iteration(project)
        return (p or ""), (p.split("\\")[-1] if p else "(no next sprint)")
    # Treat anything else as a sprint name (or an explicit path).
    p = find_iteration_by_name(project, sprint) or sprint
    return p, p.split("\\")[-1]


def list_area_paths(project: str, depth: int = 10) -> dict[str, Any]:
    url = (f"{_wit_base(project)}/classificationnodes/areas"
           f"?$depth={depth}&api-version={API_VERSION}")
    resp = requests.get(url, timeout=60, headers=_auth_header())
    resp.raise_for_status()
    return resp.json()
