# server.py
# iCloud CalDAV - MCP connector

from __future__ import annotations

import os
import logging
import datetime as dt
from pathlib import Path
from typing import List, Dict, Optional, Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from caldav.davclient import DAVClient
from caldav.lib import error as dav_error

# Configuration / Env

# Load .env that lives next to this file, regardless of CWD.
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)

def _require_env(name: str, default: Optional[str] = None) -> str:
    v = os.environ.get(name, default)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v.strip()

APPLE_ID: str    = _require_env("APPLE_ID")
APP_PW: str      = _require_env("ICLOUD_APP_PASSWORD")
CALDAV_URL: str  = _require_env("CALDAV_URL", "https://caldav.icloud.com")
DEFAULT_TZID: str = os.environ.get("TZID", "America/New_York").strip()

LOOKBACK_YEARS = 3  # for UID searches
SERVER_HOST = os.environ.get("HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("PORT", "8000"))

# Add DR profile + scan window
DR_ONLY = os.environ.get("DR_PROFILE", "0") == "1"
SCAN_DAYS = int(os.environ.get("SCAN_DAYS", str(LOOKBACK_YEARS * 365)))

# Optional: simple logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("icloud-caldav")

# MCP app

mcp = FastMCP("icloud-caldav")

@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")

# CalDAV helpers

def _client() -> DAVClient:
    """Stateless DAV client factory."""
    return DAVClient(url=CALDAV_URL, username=APPLE_ID, password=APP_PW)

def _principal():
    """Authenticated principal (raises on bad credentials)."""
    return _client().principal()

def _all_calendars():
    """List all calendars for the authenticated principal."""
    return _principal().calendars()

def _resolve_calendar(name_or_url: str):
    """Return a caldav.Calendar object from a display name or absolute URL."""
    pr = _principal()
    for c in pr.calendars():
        if c.name == name_or_url or str(c.url) == name_or_url:
            return c
    # Fallback: instantiate by URL directly
    return _client().calendar(url=name_or_url)

def _parse_iso(s: str) -> dt.datetime:
    """
    Accept 'YYYY-MM-DDTHH:MM:SS' (naive/local) or '...Z' (UTC) or with offset.
    """
    if s.endswith("Z"):
        return dt.datetime.fromisoformat(s[:-1]).replace(tzinfo=dt.timezone.utc)
    return dt.datetime.fromisoformat(s)

def _fmt(ts: dt.datetime) -> str:
    """Format as 'YYYYMMDDTHHMMSS' for ICS."""
    return ts.strftime("%Y%m%dT%H%M%S")

def _ics_escape(text: str) -> str:
    """Minimal ICS escaping for SUMMARY/DESCRIPTION."""
    return (
        text.replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;")
    )

def _to_iso(o) -> Optional[str]:
    """Best-effort ISO formatter for date/time values."""
    if o is None:
        return None
    if isinstance(o, dt.datetime):
        return o.isoformat()
    try:
        return o.isoformat()
    except Exception:
        return str(o)

# DR profile: read-only search/fetch
if DR_ONLY:

    @mcp.tool(name="search")
    def search(query: str) -> List[Dict[str, Any]]:
        """
        Read-only search across SUMMARY and DESCRIPTION within a time window.
        Returns [{ id, title, snippet }]
        - id: "{calendar_url}|{uid}"
        - title: SUMMARY
        - snippet: ISO start + calendar name
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        now = dt.datetime.now(dt.timezone.utc)
        start = now - dt.timedelta(days=SCAN_DAYS)
        end = now + dt.timedelta(days=SCAN_DAYS)

        rows: List[Dict[str, Any]] = []
        for cal in _all_calendars():
            calname = getattr(cal, "name", None) or str(cal.url)
            # expand=True to surface recurring instances as separate hits
            for ev in cal.search(event=True, start=start, end=end, expand=True):
                comp = ev.component
                summary = str(comp.get("summary", "") or "")
                descr = str(comp.get("description", "") or "")
                haystack = (summary + "\n" + descr).lower()
                if q in haystack:
                    uid = str(comp.get("uid", "") or "").strip()
                    dtstart = comp.decoded("dtstart")
                    when = _to_iso(dtstart) or ""
                    rows.append({
                        "id": f"{str(cal.url)}|{uid}",
                        "title": summary[:200],
                        "snippet": f"{when} — {calname}",
                    })
        return rows[:200]

    @mcp.tool(name="fetch")
    def fetch(ids: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch raw ICS for ids returned by search().
        Returns [{ id, mimeType: 'text/calendar', content }]
        """
        ids = ids or []
        calendars = {str(c.url): c for c in _all_calendars()}
        now = dt.datetime.now(dt.timezone.utc)
        start = now - dt.timedelta(days=SCAN_DAYS)
        end = now + dt.timedelta(days=SCAN_DAYS)

        out: List[Dict[str, Any]] = []
        for ident in ids:
            try:
                cal_url, uid = ident.split("|", 1)
            except ValueError:
                continue
            cal = calendars.get(cal_url)
            if not cal:
                continue
            found_raw = None
            # expand=False to get the series VEVENT ICS blob
            for ev in cal.search(event=True, start=start, end=end, expand=False):
                comp = ev.component
                if str(comp.get("uid", "") or "").strip() == uid:
                    found_raw = ev.data
                    break
            if found_raw:
                out.append({
                    "id": ident,
                    "mimeType": "text/calendar",
                    "content": found_raw,
                })
        return out

# Write-capable tools (default mode)
if not DR_ONLY:

    @mcp.tool()
    def list_calendars() -> List[Dict[str, Any]]:
        """
        Return available calendar containers with their name and URL.
        """
        pr = _principal()
        out: List[Dict[str, Any]] = []
        for cal in pr.calendars():
            out.append({
                "name": getattr(cal, "name", None),
                "url": str(cal.url),
                "id": getattr(cal, "id", None),
            })
        return out

    @mcp.tool()
    def list_events(
        calendar_name_or_url: str,
        start: str,
        end: str,
        expand_recurring: bool = True
    ) -> List[Dict[str, Any]]:
        """
        List events between ISO datetimes [start, end).
        calendar_name_or_url: either display name or absolute CalDAV URL.
        """
        s = _parse_iso(start)
        e = _parse_iso(end)
        cal = _resolve_calendar(calendar_name_or_url)

        events = cal.search(event=True, start=s, end=e, expand=expand_recurring)
        out: List[Dict[str, Any]] = []
        for ev in events:
            comp = ev.component  # icalendar.Event
            summary = str(comp.get("summary", "")) if comp.get("summary") is not None else ""
            dtstart = comp.decoded("dtstart")
            dtend   = comp.decoded("dtend", default=None)
            uid     = str(comp.get("uid", "")) if comp.get("uid") is not None else ""

            out.append({
                "uid": uid,
                "summary": summary,
                "start": dtstart.isoformat() if hasattr(dtstart, "isoformat") else str(dtstart),
                "end":   dtend.isoformat() if (dtend and hasattr(dtend, "isoformat")) else (str(dtend) if dtend else None),
                "raw": ev.data,  # original ICS text
            })
        return out

    @mcp.tool()
    def create_event(
        calendar_name_or_url: str,
        summary: str,
        start: str,
        end: str,
        tzid: Optional[str] = None,
        description: Optional[str] = None
    ) -> str:
        """
        Create an event in the given calendar.
        start/end: ISO datetimes, local or '...Z' for UTC.
        tzid: IANA TZ (e.g., 'America/New_York'). If omitted, uses DEFAULT_TZID.
        """
        s = _parse_iso(start)
        e = _parse_iso(end)
        tzid = tzid or DEFAULT_TZID

        cal = _resolve_calendar(calendar_name_or_url)

        uid = os.urandom(16).hex() + "@chatgpt-mcp"
        ics = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//ChatGPT MCP iCloud CalDAV//EN",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DTSTART;TZID={tzid}:{_fmt(s)}",
            f"DTEND;TZID={tzid}:{_fmt(e)}",
        ]
        if description:
            ics.append(f"DESCRIPTION:{_ics_escape(description)}")
        ics += ["END:VEVENT", "END:VCALENDAR"]

        cal.save_event("\n".join(ics))
        return uid

    @mcp.tool()
    def update_event(
        calendar_name_or_url: str,
        uid: str,
        summary: Optional[str] = None,
        start: Optional[str] = None,   # ISO datetime, local or '...Z'
        end: Optional[str] = None,     # ISO datetime, local or '...Z'
        tzid: Optional[str] = None,    # IANA TZ; defaults to DEFAULT_TZID if not derivable
        description: Optional[str] = None
    ) -> bool:
        """
        Update a single VEVENT identified by UID in the given calendar.
        Any omitted field is preserved from the existing event.
        Returns True if an event was updated, else False.

        Notes:
        - Updates the whole event (series, if recurring), not a single instance.
        - Rich properties (alarms, attendees, recurrences) are preserved only
          to the extent they exist in the original component; we rewrite a minimal event.
        """
        cal = _resolve_calendar(calendar_name_or_url)

        # Find target event by UID across a generous window
        now = dt.datetime.now(dt.timezone.utc)
        s_window = now - dt.timedelta(days=365 * LOOKBACK_YEARS)
        e_window = now + dt.timedelta(days=365 * LOOKBACK_YEARS)

        target = None
        for ev in cal.search(event=True, start=s_window, end=e_window, expand=False):
            comp = ev.component
            if str(comp.get("uid", "")) == uid:
                target = ev
                break
        if target is None:
            return False

        comp = target.component
        old_summary = str(comp.get("summary", "")) if comp.get("summary") is not None else ""
        old_desc    = str(comp.get("description", "")) if comp.get("description") is not None else ""
        old_dtstart = comp.decoded("dtstart")
        old_dtend   = comp.decoded("dtend", default=None)

        def _to_dt(s: Optional[str], fallback: dt.datetime) -> dt.datetime:
            if s is None:
                return fallback
            if s.endswith("Z"):
                return dt.datetime.fromisoformat(s[:-1]).replace(tzinfo=dt.timezone.utc)
            return dt.datetime.fromisoformat(s)

        new_summary = summary if summary is not None else old_summary
        new_desc    = description if description is not None else old_desc
        new_start   = _to_dt(start, old_dtstart)
        new_end     = _to_dt(end,   old_dtend if old_dtend is not None else (new_start + dt.timedelta(hours=1)))

        # Keep original TZID if present; else requested; else default.
        try:
            orig_tzid = comp["dtstart"].params.get("TZID") if "dtstart" in comp and hasattr(comp["dtstart"], "params") else None
        except Exception:
            orig_tzid = None
        use_tzid = tzid or orig_tzid or DEFAULT_TZID

        new_ics = "\n".join([
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//ChatGPT MCP iCloud CalDAV//EN",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"SUMMARY:{_ics_escape(new_summary)}",
            f"DTSTART;TZID={use_tzid}:{_fmt(new_start)}",
            f"DTEND;TZID={use_tzid}:{_fmt(new_end)}",
            *( [f"DESCRIPTION:{_ics_escape(new_desc)}"] if new_desc else [] ),
            "END:VEVENT",
            "END:VCALENDAR",
        ])

        target.data = new_ics
        target.save()
        return True

    @mcp.tool()
    def delete_event(calendar_name_or_url: str, uid: str) -> bool:
        """
        Delete a VEVENT by UID from the given calendar.
        Returns True if deleted, else False (not found).
        """
        cal = _resolve_calendar(calendar_name_or_url)

        now = dt.datetime.now(dt.timezone.utc)
        start = now - dt.timedelta(days=365 * LOOKBACK_YEARS)
        end   = now + dt.timedelta(days=365 * LOOKBACK_YEARS)

        for ev in cal.search(event=True, start=start, end=end, expand=False):
            comp = ev.component
            if str(comp.get("uid", "")) == uid:
                ev.delete()
                return True
        return False

# Main

if __name__ == "__main__":
    log.info("Starting MCP HTTP server on %s:%s", SERVER_HOST, SERVER_PORT)
    log.info("CalDAV: %s  Apple ID: %r  TZ: %s  DR_ONLY=%s", CALDAV_URL, APPLE_ID, DEFAULT_TZID, DR_ONLY)
    mcp.run(transport="http", host=SERVER_HOST, port=SERVER_PORT, path="/mcp")
