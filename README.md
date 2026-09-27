# iCloud CalDAV MCP Connector

A **Model Context Protocol (MCP)** server (HTTP or stdio) exposing iCloud Calendar (CalDAV) tools so MCP-aware clients (e.g., ChatGPT custom connectors, Claude Code, Claude Desktop) can list calendars, read events, and create/update/delete events using an iCloud **app-specific password**.

> Unofficial. Calendar only. Keep this service private; it forwards your iCloud app-specific password to Apple's CalDAV endpoint.

---

## Why did I build this?

I built this to use in ChatGPT Custom Connector, so I can change my iCloud Calendar compared to changing it manually. Came up with this idea on a Friday night before a TOP Pset was due, and this turned out to be a fun 1-day project.

---

## Features

- HTTP MCP server (`/mcp`) + `GET /health`, or stdio for local clients (`--transport stdio`)
- Tools (default write-capable profile):
  - `list_calendars()`
  - `list_calendars_with_events(start, end, expand_recurring=True)`
  - `list_events(start, end, calendar_name_or_url?, expand_recurring=True, query?, include_raw=False)`
  - `create_event(calendar_name_or_url, summary, start, end, tzid?, description?, location?, recurrence?)`
  - `update_event(calendar_name_or_url, uid, summary?, start?, end?, tzid?, description?, location?, recurrence?, clear_recurrence=False)`
  - `delete_event(calendar_name_or_url, uid, occurrence_start?)`
- Tools (Deep Research read-only profile):
  - `search(query)` -> basic text search over SUMMARY/DESCRIPTION in a time window
  - `fetch(ids)` -> fetch raw `text/calendar` ICS blobs for search results
- ISO datetime input (`YYYY-MM-DDTHH:MM:SS`, with optional `Z` or timezone offset); bare dates for all-day events
- Updates edit the stored event in place, so alarms, attendees and recurrence exceptions survive
- Finds events by their `<uid>.ics` URL, then by UID query, then by a +/-3-year scan (iCloud rejects UID queries)

---

## Requirements

- Python **3.11+**
- Apple ID (**email** identity, not phone number)
- iCloud **app-specific password** (revocable)
- Network access to `https://caldav.icloud.com`

---

## Environment

Create a `.env` **next to** `server.py` (auto-loaded):

```env
APPLE_ID=you@example.com                 # Use your Apple ID email
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx  # App-specific password
CALDAV_URL=https://caldav.icloud.com     # optional, default shown
HOST=127.0.0.1                           # optional
PORT=8000                                # optional
TZID=America/New_York                    # default TZ for new/edited events

# Deep Research: read-only profile (optional)
DR_PROFILE=0                             # Set to 1 to enable DR mode (default 0)
SCAN_DAYS=1095                           # Time window (days) scanned by DR search/fetch (default ~3 years)
```

Required: `APPLE_ID`, `ICLOUD_APP_PASSWORD`.

---

## Quick Start (local)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Ensure .env exists (see above), then:
python server.py
# -> Listening on http://127.0.0.1:8000
curl http://127.0.0.1:8000/health   # OK
```

**MCP endpoint:** `http://127.0.0.1:8000/mcp`

---

## Use with Claude Code / Claude Desktop (stdio)

Local clients can launch the server on demand over stdio, so nothing has to stay running. The `.env` next to `server.py` is still used for credentials.

Claude Code:

```bash
claude mcp add -s user icloud-calendar -- /path/to/icloud-mcp/.venv/bin/python /path/to/icloud-mcp/server.py --transport stdio
```

Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`), then restart Claude:

```json
{
  "mcpServers": {
    "icloud-calendar": {
      "command": "/path/to/icloud-mcp/.venv/bin/python",
      "args": ["/path/to/icloud-mcp/server.py", "--transport", "stdio"]
    }
  }
}
```

`MCP_TRANSPORT=stdio` in the environment does the same as `--transport stdio`.

---

## Tool Reference (functional details)

### `list_calendars() -> List[Calendar]`

Returns:

- `name: str | null`
- `url: str` (preferred identifier for other calls)
- `id: str | null`

### `list_calendars_with_events(start, end, expand_recurring=True) -> List[Calendar]`

Returns only the calendars that contain **at least one event** in the
given time window.

**Args**

- `start, end: str`: ISO datetimes; search is [**start**, **end**)
- `expand_recurring: bool`: treat recurring series as concrete instances

Each returned calendar has the same shape as `list_calendars()`.

### `list_events(start, end, calendar_name_or_url?, expand_recurring=True, query?, include_raw=False) -> List[Event]`

**Args**

- `start, end: str`: ISO datetimes; search is [**start**, **end**) (naive times are in `TZID`)
- `calendar_name_or_url: str | null`: display name or full CalDAV URL; omit to search **all** calendars
- `expand_recurring: bool`: include concrete instances of recurring series
- `query: str | null`: case-insensitive filter on summary, location and description
- `include_raw: bool`: also return the ICS text (large; off by default)

**Returns** events sorted by start, each with:

- `uid: str` (shared by every occurrence of a recurring series)
- `summary: str`
- `start: str`, `end: str | null`: ISO, expressed in `TZID`; date-only for all-day events (end exclusive)
- `all_day: bool`
- `location: str | null`
- `description: str | null` (first 500 characters)
- `calendar: str` (display name)
- `recurring: bool`
- `raw: str` (only with `include_raw=true`)

### `create_event(calendar_name_or_url, summary, start, end, tzid?, description?, location?, recurrence?) -> str`

Creates a **VEVENT**.

- `start`/`end` as dates (`YYYY-MM-DD`) create an all-day event; `end` is exclusive (a single day is `D` to `D+1`; `end == start` is treated as one day).
- `tzid` defaults to `TZID` env if omitted; naive datetimes are assumed in that zone (stored as `DTSTART;TZID=...`).
- An unrecognized `recurrence` is rejected rather than silently ignored.
- `description` is optional; omit or pass `null` to skip it.
- `location` is optional; omit or pass `null` to skip it.
- `recurrence` (optional) describes how the event should repeat, for example:

    ```jsonc
    {
        "frequency": "weekly",              // daily | weekly | monthly | yearly | custom
        "interval": 1,                       // optional, default 1
        "by_weekday": ["MO", "WE"],         // optional; for weekly/custom
        "by_monthday": [1, 15],             // optional; for monthly/custom
        "end": {                            // optional end condition
            "type": "on_date",              // or "after_occurrences"
            "date": "2025-12-31"            // when type == "on_date"
            // or: "count": 10               // when type == "after_occurrences"
        }
        // for custom frequency you can pass a raw RRULE:
        // "frequency": "custom",
        // "rrule": "FREQ=MONTHLY;BYDAY=MO,TU;BYSETPOS=1"
    }
    ```

- Returns the generated `uid` (random hex + `@icloud-mcp`).

### `update_event(calendar_name_or_url, uid, summary?, start?, end?, tzid?, description?, location?, recurrence?, clear_recurrence=False) -> bool`

Updates the **whole** event identified by `uid` (for recurring events this updates the series VEVENT, not a single instance).

- Edits the stored event in place: anything not passed (alarms, attendees, URL, EXDATEs, moved occurrences, ...) is kept.
- `start`/`end`:
  - Only `start` given: the event keeps its duration.
  - Dates (`YYYY-MM-DD`) make it all-day; datetimes make it timed.
  - Moving a recurring series' start shifts its EXDATEs and moved occurrences by the same amount so they stay attached.
- `description`: omit to keep, `""` to clear.
- `location`:
  - If omitted (`null` / not provided), keeps the existing location.
  - If provided as a non-empty string, updates the event's location.
  - If provided as an empty string, clears the event's location.
- `recurrence`:
  - If provided, replaces any existing RRULE using the same shape as in `create_event`.
- `clear_recurrence`:
  - If `True`, removes any RRULE/RDATE/EXDATE and moved occurrences, converting the event back to a single non-recurring instance.
  - If `True` and `recurrence` is also provided, `clear_recurrence` wins (no recurrence).
- Returns `True` on success, `False` if `uid` is not in that calendar.

### `delete_event(calendar_name_or_url, uid, occurrence_start?) -> bool`

Deletes the event with `uid`.

- Without `occurrence_start`: deletes the whole event (the entire series if recurring).
- With `occurrence_start` (the `start` that `list_events` returned for that occurrence, or just its date): deletes only that occurrence by adding an EXDATE (and dropping its override if it had been moved). The rest of the series stays.
- Returns `True` if deleted, `False` if the event or occurrence is not found.

**Date/Time Notes**

- Accepts naive or `Z`/offset datetimes (`YYYY-MM-DDTHH:MM:SS`, optionally `Z` or `-04:00` etc.)
- `YYYY-MM-DD` means an all-day event (create/update) or a whole day (`occurrence_start`)
- New/rescheduled events emit `DTSTART;TZID=...` and `DTEND;TZID=...` using provided `tzid` or `TZID` env
- Updates leave `DTSTART`/`DTEND` (and their TZID) untouched unless `start`/`end` are passed
- `LOCATION` is emitted when `location` is provided and non-empty; passing an empty string when updating an event removes the existing location.

---

## Deep Research read-only mode

Set DR_PROFILE=1 to run a read-only tool set for Deep Research. This exposes only:

- search(query) -> [{ id, title, snippet }]
- fetch(ids) -> [{ id, mimeType: 'text/calendar', content }]

Example:

```bash
DR_PROFILE=1 HOST=127.0.0.1 PORT=8000 python server.py
```

Notes:

- Write tools (list_events/create_event/update_event/delete_event) are disabled in this mode.
- SCAN_DAYS controls the search window around "now" (default: 1095 days ~ 3 years).
- Keep this service private or add auth

---

## Example (programmatic client)

```python
import asyncio, json
from fastmcp import Client

MCP_URL = "http://127.0.0.1:8000/mcp"
CAL_URL = "<paste one of your calendar URLs>"

def unwrap(res):
    sc = getattr(res, "structured_content", None)
    if isinstance(sc, dict) and "result" in sc:
        return sc["result"]
    return json.loads(res.content[0].text)

async def main():
    async with Client(MCP_URL) as c:
        cals = unwrap(await c.call_tool("list_calendars", {"confirm": True}))
        print("Calendars:", cals[:2])

        evs = unwrap(await c.call_tool("list_events", {
            "calendar_name_or_url": CAL_URL,
            "start": "2025-09-01T00:00:00",
            "end":   "2025-10-01T00:00:00",
            "expand_recurring": True
        }))
        print("Events:", len(evs))

        uid = unwrap(await c.call_tool("create_event", {
            "calendar_name_or_url": CAL_URL,
            "summary":"Demo",
            "start":"2025-09-29T15:00:00",
            "end":"2025-09-29T15:30:00",
            "tzid":"America/New_York",
            "location": "Bobst Library"
        }))
        print("Created:", uid)

asyncio.run(main())
```

---

## Deployment / Public HTTPS

To use this with ChatGPT Custom Connectors you need a public HTTPS endpoint that forwards to your local server.

See [DEPLOY.md](./DEPLOY.md) for:

- Cloudflare Tunnel (stable hostname, free)
- ngrok (quick test)
- VPS + Caddy/Nginx (permanent)

Security: add auth (Cloudflare Access, Basic Auth proxy, IP allowlist). Do **NOT** expose this unauthenticated; it holds live calendar write access.
You need a public HTTPS URL that forwards to your local `http://127.0.0.1:8000`.

---

## Troubleshooting

| Symptom              | Likely Cause / Fix                                                                |
| -------------------- | --------------------------------------------------------------------------------- |
| `401 Unauthorized`   | Wrong Apple ID or app-specific password; ensure `.env` uses **email**, not phone. |
| Empty event results  | Wrong calendar URL or time window; remember `end` is exclusive.                   |
| Update/Delete no-ops | UID belongs to a different calendar than the one you passed.                      |
| Timezone drift       | Pass `tzid` explicitly (e.g., `America/New_York`) or use UTC `...Z`.              |

---

## Security

- Use **app-specific passwords** and rotate as needed
- Keep this server private (tunnel ACLs, IP allowlists, auth proxy)
- Updates edit the stored event in place; the server only changes the fields you pass (plus DTSTAMP/LAST-MODIFIED)

---

## License

MIT License.

---

Happy scheduling, I hope this helps!
