# mcp-ado

A FastMCP server that scans your team's **Azure DevOps** work items (by area
path), flags anything **overdue** or **stale** (not updated in N days), and
emails each **assignee** their own list — sent **as you** via delegated
Microsoft Graph.

## How it works

1. **ADO** — a WIQL query returns open (non-done) items `UNDER` your area path
   using a PAT (Work Items → Read). Fields are batch-fetched.
2. **Flagging** — an item is flagged if its Due/Target date is past, **or** its
   `ChangedDate` is older than `STALE_DAYS`. Results are grouped by assignee.
3. **Email** — each assignee gets an HTML reminder listing only their items,
   sent from your mailbox via Graph `/me/sendMail`. Auth is the Windows WAM
   broker (no secret, no pasted token) — same pattern as the ADF MCP.

## Setup

```powershell
git clone https://github.com/rajivdatta/mcp-ado.git
cd mcp-ado
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env             # then edit .env
Copy-Item targets.example.json targets.json   # optional, see below
```

Fill in `.env`:
- `ADO_ORG_URL`, `ADO_PROJECT`, `ADO_AREA_PATH`, `ADO_PAT`
- `STALE_DAYS` (default 7), optional `ADO_WORK_ITEM_TYPES` / `ADO_EXCLUDE_STATES`
- `ADO_ITERATION_PATH` — optional; `@current` auto-detects the sprint whose
  start/finish dates contain today
- `TRACK_PEOPLE` — optional CSV of assignee emails; when set, only those
  people's items are flagged. Blank = everyone.
- `ENABLE_SEND` — master kill-switch for email, **default off**
- `MAIL_CC` / `MAIL_FALLBACK_TO` (optional)

Not sure of your area path? After setting org/project/PAT, call
`list_area_paths` to print the tree.

### Scanning more than one team

Drop a `targets.json` next to `server.py` to scan several
project/area/iteration combinations in one pass (org + PAT stay global in
`.env`):

```json
[
  { "name": "Data", "project": "YOUR_PROJECT",
    "area_path": "YOUR_PROJECT\\Your Team", "iteration_path": "@current" }
]
```

Items are de-duplicated by id across targets, and a person with flagged items
in several targets gets **one** combined email. Without `targets.json`, the
`ADO_PROJECT` / `ADO_AREA_PATH` / `ADO_ITERATION_PATH` env values are used as a
single target.

## Tools

| Tool | Sends email? | Purpose |
|------|:---:|---------|
| `list_targets` | no | Show the resolved scan targets |
| `test_connection` | no | Verify PAT + config, show open-item count |
| `list_area_paths` | no | Print area-path tree for a project |
| `current_iteration` | no | Show configured vs resolved sprint (`@current`) |
| `scan` | no | Flag overdue/stale items grouped by assignee |
| `preview_notifications` | no | Render the exact per-assignee emails |
| `send_notifications` | **yes** | Send — **only when `ENABLE_SEND=true` and `confirm=True`** |

**Safety:** every tool but the last is read-only, and `send_notifications`
sends nothing unless `ENABLE_SEND=true` in `.env` **and** you pass
`confirm=True`. The first Graph call pops a WAM sign-in; after that it's silent.

### Suggested flow
`test_connection` → `scan` → `preview_notifications` → `send_notifications(confirm=True)`

## Register with Claude

Add to your MCP config (`claude_desktop_config.json` for Desktop, or via
`claude mcp add` for Claude Code):

```json
{
  "mcpServers": {
    "ado": {
      "command": "C:\\path\\to\\mcp-ado\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\mcp-ado\\server.py"]
    }
  }
}
```

## Notes

- **Delegated Mail.Send consent.** The broker signs in with the Azure CLI
  first-party client by default. If tenant policy blocks Mail.Send consent for
  it, register a public-client app with delegated `Mail.Send` + `User.Read`
  and set `GRAPH_CLIENT_ID` to its id.
- **Scheduling.** For a daily run, wrap `send_notifications(confirm=True)` in a
  small script and drive it from Windows Task Scheduler, or ask Claude to run
  the tool on a schedule.
- Dates are compared in UTC.
