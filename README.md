# iCloud CalDAV MCP Connector

Minimal [Model Context Protocol](https://github.com/modelcontextprotocol) (MCP) server that exposes a subset of iCloud Calendar (CalDAV) operations as MCP tools. This lets an MCP‑aware client (e.g. an AI assistant / IDE integration) list calendars, fetch events, and create / update / delete events in your iCloud calendar using an app‑specific password.

**Calendar only. Not an official Apple product. Uses an iCloud app‑specific password for access.**

---

## Features

- `list_calendars`
- `list_events`
- `create_event`
- `update_event`
- `delete_event`
- And many more to be added...
  
## Features

- HTTP MCP server (FastMCP) with a simple `/health` route.
- Tools exposed:
  - `list_calendars`
  - `list_events`
  - `create_event`
  - `update_event`
  - `delete_event`
- Accepts ISO datetimes (`YYYY-MM-DDTHH:MM:SS` with optional trailing `Z`).
- Automatic 3‑year look‑back/forward window for UID matching when updating/deleting.
- Lightweight ICS generation with minimal escaping.

## iCloud CalDAV MCP Connector

Minimal MCP server exposing a small set of iCloud Calendar (CalDAV) operations: list calendars, list events, create, update, delete events. Intended for use by MCP‑aware AI assistants or tooling.

> Scope: **Calendar only. Not an official Apple product. Uses an iCloud app‑specific password for access.**

---

## Features

- HTTP MCP server (FastMCP) + `/health` endpoint
- Tools:
  - `list_calendars`
  - `list_events`
  - `create_event`
  - `update_event`
  - `delete_event`
- ISO datetime input (with or without trailing `Z` / offset)
- 3‑year lookback/lookahead window when searching by UID for updates/deletes
- Minimal, readable ICS generation

## High‑Level Architecture

Each tool call instantiates a CalDAV `DAVClient` using credentials from environment variables. No persistent cache or DB; operations hit iCloud directly. `fastmcp.FastMCP` wraps Starlette to expose tools under `/mcp`.

## Requirements

- Python 3.11+
- Apple ID
- iCloud app‑specific password (create at appleid.apple.com > Sign‑In & Security > App‑Specific Passwords)
- Network access to `caldav.icloud.com` (or regional shard URL if different)

## Environment Variables (.env)

Create a `.env` adjacent to `server.py` (auto‑loaded):

```.env
APPLE_ID=you@example.com                 # Only Email works (field does not expect phone number)
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx  # App-specific password
CALDAV_URL=https://caldav.icloud.com     # optional (default shown)
HOST=0.0.0.0                             # optional (default 127.0.0.1)
PORT=8000                                # optional
TZID=America/New_York                    # default timezone for new events
```

Required: `APPLE_ID`, `ICLOUD_APP_PASSWORD`.

## Quick Start (Local)

1. Create `.env` (see above).
2. Install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Run the server:

```bash
python server.py
```

4. MCP base URL: `http://127.0.0.1:8000/mcp`
5. Health check:

```bash
curl http://127.0.0.1:8000/health
```

## Quick Start (Docker)

```bash
# Run with env vars
docker build -t icloud-mcp .

# Optional: mount a local `.env` instead of passing vars.
docker run -it --rm \
  -e APPLE_ID=you@example.com \
  -e ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx \
  -e TZID=America/New_York \
  icloud-mcp
```

## Tool Reference

### list_calendars() -> List[Calendar]

Returns objects with: `name`, `url`, `id` (if present). Prefer using the `url` in other calls.

### list_events(calendar_name_or_url, start, end, expand_recurring=True) -> List[Event]

Inputs:

- `calendar_name_or_url`: Display name or full CalDAV URL
- `start`, `end`: ISO datetimes; CalDAV search is $[\text{start}, \text{end})$
- `expand_recurring`: If true, expands recurring events

Returns each event as: `uid`, `summary`, `start`, `end`, `raw` (ICS text).

### create_event(calendar_name_or_url, summary, start, end, tzid=None, description=None) -> UID

Creates a single VEVENT. `tzid` defaults to `TZID` env (or fallback). UID is random hex + `@chatgpt-mcp` suffix.

### update_event(calendar_name_or_url, uid, summary?, start?, end?, tzid?, description?) -> bool

Searches $\pm 3$ years for the matching UID. Rewrites a minimal VEVENT (advanced properties like attendees/alarms/recurrence rules are not preserved). Returns False if not found.

### delete_event(calendar_name_or_url, uid) -> bool

Deletes first UID match in $\pm 3$ year window. Returns True if deleted.

## Date / Time Handling

- Accepts naive or `Z`/offset datetimes (`YYYY-MM-DDTHH:MM:SS`, optionally `Z` or `-04:00` etc.)
- New events emit `DTSTART;TZID=...` and `DTEND;TZID=...` using provided or default TZID
- Updates try to reuse the original TZID when present

## Security Notes

- Use an app‑specific password (revocable) instead of primary password
- Secrets only live in environment variables / process memory
- No rate limiting or auth layer beyond iCloud; keep deployment private or behind a gateway

## Troubleshooting

| Symptom               | Likely Cause / Fix                                                             |
| --------------------- | ------------------------------------------------------------------------------ |
| 401 Unauthorized      | Wrong Apple ID or app password; remember to update `.env`.                     |
| Empty calendar list   | Region mismatch or no calendars; inspect principal URL via CalDAV debug tools. |
| Cannot update/delete  | UID outside $\pm 3$ year window or wrong calendar URL.                         |
| Time shifts / offsets | Specify `tzid` explicitly or confirm `TZID` matches intended zone.             |

## Extending Ideas

- Preserve recurrence rules / attendees on update
- Add direct `get_event` by UID (without large window scan)
- Add caching layer for performance
- Support reminders / alarms

## Disclaimer

Unofficial. Not affiliated with Apple. Use at your own risk; API behaviors may change.

## License

MIT License. Add your own if you plan to distribute.

---
Happy scheduling, I hope this helps! Came up with this idea on a Friday night before a pset was due, and this turned out to be a fun $1$-day project.
