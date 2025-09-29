# iCloud CalDAV MCP Connector

An HTTP **Model Context Protocol (MCP)** server exposing iCloud Calendar (CalDAV) tools so MCP-aware clients (e.g., ChatGPT custom connectors, IDEs) can list calendars, read events, and create/update/delete events using an iCloud **app-specific password**.

> Unofficial. Calendar only. Keep this service private; it forwards your iCloud app-specific password to Apple’s CalDAV endpoint.

---

## Features

- HTTP MCP server (`/mcp`) + `GET /health`
- Tools:
  - `list_calendars()`
  - `list_events(calendar_name_or_url, start, end, expand_recurring=True)`
  - `create_event(calendar_name_or_url, summary, start, end, tzid?, description?)`
  - `update_event(calendar_name_or_url, uid, summary?, start?, end?, tzid?, description?)`
  - `delete_event(calendar_name_or_url, uid)`
- ISO datetime input (`YYYY-MM-DDTHH:MM:SS`, with optional `Z` or timezone offset)
- Minimal ICS generation (summary/description escaping), UID matching across a ±3-year window

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
````

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

## Tool Reference (functional details)

### `list_calendars() -> List[Calendar]`

Returns:

* `name: str | null`
* `url: str` (preferred identifier for other calls)
* `id: str | null`

### `list_events(calendar_name_or_url, start, end, expand_recurring=True) -> List[Event]`

**Args**

* `calendar_name_or_url: str` — display name or full CalDAV URL
* `start, end: str` — ISO datetimes; search is [**start**, **end**)
* `expand_recurring: bool` — include concrete instances of recurring series

**Returns** each event with:

* `uid: str`
* `summary: str`
* `start: str` (ISO)
* `end: str | null` (ISO)
* `raw: str` (original ICS text)

### `create_event(calendar_name_or_url, summary, start, end, tzid?, description?) -> str`

Creates a minimal **VEVENT**.

* `tzid` defaults to `TZID` env if omitted.
* Returns the generated `uid` (random hex + `@chatgpt-mcp`).

### `update_event(calendar_name_or_url, uid, summary?, start?, end?, tzid?, description?) -> bool`

Updates the **whole** event identified by `uid` (for recurring events this updates the series VEVENT, not a single instance).

* Preserves any omitted fields from the original component.
* Returns `True` on success, `False` if `uid` not found in ±3-year window.

### `delete_event(calendar_name_or_url, uid) -> bool`

Deletes the first matching `uid` in a ±3-year window.

* Returns `True` if deleted, `False` if not found.

**Date/Time Notes**

* Accepts naive or `Z`/offset datetimes (`YYYY-MM-DDTHH:MM:SS`, optionally `Z` or `-04:00` etc.)
* New/edited events emit `DTSTART;TZID=...` and `DTEND;TZID=...` using provided `tzid` or `TZID` env
* Updates attempt to reuse the original TZID when present

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
        cals = unwrap(await c.call_tool("list_calendars"))
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
            "tzid":"America/New_York"
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
| Update/Delete no-ops | UID not in ±3-year scan window or different calendar than you’re querying.        |
| Timezone drift       | Pass `tzid` explicitly (e.g., `America/New_York`) or use UTC `...Z`.              |

---

## Security

* Use **app-specific passwords** and rotate as needed
* Keep this server private (tunnel ACLs, IP allowlists, auth proxy)
* This project rewrites minimal VEVENTs; advanced fields (attendees, alarms, recurrence exceptions) are not preserved on update

---

## License

MIT License.

---

## Why did I build this?

Happy scheduling, I hope this helps! I built this to use in ChatGPT Custom Connector, so I can change my iCloud Calendar compared to changing it manually. Came up with this idea on a Friday night before a TOP Pset was due, and this turned out to be a fun 1-day project.
