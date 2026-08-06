# mcp-ado

A FastMCP server that scans your team's **Azure DevOps** work items (by area
path), flags anything **overdue** or **stale** (not updated in N days), and
emails each **assignee** their own list — sent **as you** via delegated
Microsoft Graph.

It also drills into a stalled item (comments, link graph, revision history),
reports what is **blocking** it, reads and writes **wiki** pages, and can
comment on / update / create work items — with writing off by default and
gated twice.

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

### Getting the ADO personal access token

A PAT is the credential this server uses to talk to Azure DevOps. Grant the
**narrowest scope for the tools you actually intend to use** — there is no
reason to hand it Full access:

| You want | Scopes to grant |
|----------|-----------------|
| Monitoring + reminder emails (the default) | `Work Items → Read` |
| `get_work_item`, `blocked_items` | `Work Items → Read` |
| `list_wiki_pages`, `get_wiki_page` | `Wiki → Read` |
| `add_work_item_comment`, `update_work_item`, `create_work_item` | `Work Items → Read & Write` |
| `create_or_update_wiki_page` | `Wiki → Read & Write` |

Read-only is the right default: with `ENABLE_WRITE=false` (the shipped value)
the write tools refuse before making any request, so a read-only PAT loses you
nothing.

1. Sign in to your Azure DevOps organization: `https://dev.azure.com/YOUR_ORG`
2. Open **User settings** (the icon beside your avatar, top right) →
   **Personal access tokens**. Direct link:
   `https://dev.azure.com/YOUR_ORG/_usersSettings/tokens`
3. Click **+ New Token**.
4. Fill in the token:
   - **Name** — anything memorable, e.g. `mcp-ado`
   - **Organization** — the org holding the work items. If you belong to
     several, a token scoped to one org will **not** work against another.
   - **Expiration** — pick the shortest window you'll tolerate re-issuing.
5. Under **Scopes**, choose **Custom defined**, then expand **Work Items** and
   tick **Read**. Leave everything else unchecked.
6. Click **Create**, then **copy the token immediately** — Azure DevOps shows
   it exactly once and you cannot retrieve it afterwards.
7. Paste it into `.env` as `ADO_PAT=...`, then verify with the
   `test_connection` tool, which reports the open-item count on success.

`.env` is listed in `.gitignore`, so the token stays out of git. Keep it that
way — a PAT is a bearer credential, so anyone holding it has your read access
until it expires or you revoke it.

**When it stops working.** PATs expire, and an expired or revoked token fails
every call. If `test_connection` starts returning an authentication error — or
an HTML sign-in page instead of JSON — re-issue the token at the same
**Personal access tokens** page and update `.env`. Two other causes worth
ruling out: the token was scoped to a different organization than
`ADO_ORG_URL`, or it lacks the `Work Items → Read` scope. Some organizations
also restrict who may create PATs at all; if **+ New Token** is unavailable,
your Azure DevOps administrator has to permit it.

Note that the email side uses **no** PAT — it signs in through the Windows WAM
broker instead, so `ADO_PAT` only ever governs reading work items.

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

### Read-only

| Tool | Purpose |
|------|---------|
| `list_targets` | Show the resolved scan targets |
| `test_connection` | Verify PAT + config, show open-item count |
| `list_area_paths` | Print area-path tree for a project |
| `current_iteration` | Show configured vs resolved sprint (`@current`) |
| `scan` | Flag overdue/stale items grouped by assignee |
| `get_work_item` | One item in full — fields, links, comments, revision history |
| `blocked_items` | Blocked items **and what is blocking them** |
| `list_wiki_pages` | Wiki page hierarchy |
| `get_wiki_page` | One wiki page's markdown (+ etag) |
| `preview_notifications` | Render the exact per-assignee emails |

### Changes something — each gated twice

| Tool | Needs | Purpose |
|------|-------|---------|
| `add_work_item_comment` | `ENABLE_WRITE` + `confirm` | Post a discussion comment |
| `update_work_item` | `ENABLE_WRITE` + `confirm` | Change state / assignee / dates / any field |
| `create_work_item` | `ENABLE_WRITE` + `confirm` | Create an item, optionally under a parent |
| `create_or_update_wiki_page` | `ENABLE_WRITE` + `confirm` (+ `allow_overwrite`) | Write a wiki page |
| `send_notifications` | `ENABLE_SEND` + `confirm` | Send the reminder emails |

**Safety model.** Nothing outside your machine changes unless you opt in twice:
an `.env` master switch (`ENABLE_WRITE` / `ENABLE_SEND`, both default **off**)
*and* an explicit `confirm=True` on the call. Extra guards:

- `update_work_item` and `create_work_item` accept `validate_only=True`, which
  asks Azure DevOps to validate the change and **save nothing** — a real dry
  run against the server, usable even with `ENABLE_WRITE=false`.
- `create_or_update_wiki_page` always reads the page first and reports what
  would change. Overwriting an **existing** page also needs
  `allow_overwrite=True`, because the whole page is replaced, not merged — and
  the response echoes the previous content so an unwanted overwrite is
  recoverable.
- The first Graph call pops a WAM sign-in; after that it's silent.

### Drilling into a stalled item

`scan` returns a dozen summary fields per item — enough to flag it, not enough
to explain it. `get_work_item` adds the discussion, the parent/child chain, and
a per-revision changelog, so you can tell a genuinely untouched item from one
being nudged without progress:

```
scan(only_flagged=True) → get_work_item(id=33740, include_history=True)
```

`blocked_items` answers the related question — *is this stalled because of
someone else?* It reports items whose state/tags declare them blocked, plus
items with an unfinished **predecessor** link, naming the blocker. Chasing an
assignee whose work is gated on another open item is how reminder emails lose
their credibility.

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
