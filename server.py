# server.py
# iCloud CalDAV - MCP connector

from __future__ import annotations

import os
import argparse
import logging
import datetime as dt
from pathlib import Path
from typing import List, Dict, Optional, Any, Union
from zoneinfo import ZoneInfo

import recurring_ical_events
from dotenv import load_dotenv
from fastmcp import FastMCP
from icalendar import Calendar as ICalendar, Event as IEvent, vRecur
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from caldav.davclient import DAVClient
from caldav.lib import error as dav_error

# Configuration / Env

# Load .env that lives next to this file, regardless of CWD.
load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=True)


def _require_env(name: str, default: Optional[str] = None) -> str:
    """Return a required environment variable, or raise if missing."""
    value = os.environ.get(name, default)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value.strip()

APPLE_ID: str    = _require_env("APPLE_ID")
APP_PW: str      = _require_env("ICLOUD_APP_PASSWORD")
CALDAV_URL: str  = _require_env("CALDAV_URL", "https://caldav.icloud.com")
DEFAULT_TZID: str = os.environ.get("TZID", "America/New_York").strip()

LOOKBACK_YEARS = 3  # for UID searches
DESCRIPTION_LIMIT = 500  # chars of DESCRIPTION returned by list_events
SERVER_HOST = os.environ.get("HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("PORT", "8000"))

# Deep Research (read-only) profile
DR_ONLY = os.environ.get("DR_PROFILE", "0") == "1"
SCAN_DAYS = int(os.environ.get("SCAN_DAYS", str(LOOKBACK_YEARS * 365)))

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("icloud-caldav")

# MCP app

mcp = FastMCP("icloud-caldav")

@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")

# CalDAV helpers

DateOrDateTime = Union[dt.date, dt.datetime]


def _client() -> DAVClient:
    """Return a new DAV client."""
    return DAVClient(url=CALDAV_URL, username=APPLE_ID, password=APP_PW)


def _principal():
    """Return the authenticated CalDAV principal (raises on auth failure)."""
    return _client().principal()


def _all_calendars():
    """Return all calendars for the authenticated principal."""
    return _principal().calendars()


def _cal_name(calendar) -> Optional[str]:
    """Display name of a calendar."""
    return calendar.get_display_name()


def _resolve_calendar(name_or_url: str):
    """Return a caldav.Calendar from a display name or absolute URL."""
    for calendar in _all_calendars():
        if _cal_name(calendar) == name_or_url or str(calendar.url) == name_or_url:
            return calendar
    # Fallback: instantiate by URL directly
    return _client().calendar(url=name_or_url)


def _find_event(calendar, uid: str):
    """Return the object holding ``uid`` in ``calendar``, or None.

    Falls back to scanning LOOKBACK_YEARS either side of today if the
    server rejects the UID query.
    """
    try:
        return calendar.event_by_uid(uid)
    except dav_error.NotFoundError:
        return None
    except dav_error.DAVError as exc:
        log.warning("UID lookup failed on %s (%s); scanning instead", _cal_name(calendar), exc)

    start, end = _uid_search_window()
    for ev in calendar.search(event=True, start=start, end=end, expand=False):
        if str(ev.component.get("uid", "")) == uid:
            return ev
    return None

def _parse_iso(s: str) -> dt.datetime:
    """
    Accept 'YYYY-MM-DDTHH:MM:SS' (naive/local) or '...Z' (UTC) or with offset.
    """
    if s.endswith("Z"):
        return dt.datetime.fromisoformat(s[:-1]).replace(tzinfo=dt.timezone.utc)
    return dt.datetime.fromisoformat(s)


def _parse_when(s: str) -> DateOrDateTime:
    """Like ``_parse_iso``, but a bare 'YYYY-MM-DD' becomes a date (all-day)."""
    s = s.strip()
    if len(s) == 10:
        return dt.date.fromisoformat(s)
    return _parse_iso(s)


def _is_all_day(value: DateOrDateTime) -> bool:
    """True for a plain date (all-day), False for a datetime."""
    return not isinstance(value, dt.datetime)


def _scan_window() -> tuple[dt.datetime, dt.datetime]:
    """Return the time window used for DR search/fetch operations."""
    now = dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=SCAN_DAYS)
    end = now + dt.timedelta(days=SCAN_DAYS)
    return start, end


def _uid_search_window() -> tuple[dt.datetime, dt.datetime]:
    """Return the wide time window used for UID-based lookups."""
    now = dt.datetime.now(dt.timezone.utc)
    delta = dt.timedelta(days=365 * LOOKBACK_YEARS)
    return now - delta, now + delta

def _to_iso(o) -> Optional[str]:
    """ISO string for a date/datetime; None stays None."""
    return o.isoformat() if o is not None else None


def _normalize_to_tz(ts: dt.datetime, tzid: str) -> dt.datetime:
    """Return ``ts`` normalized into the given IANA timezone.

    Naive datetimes are treated as wall time in ``tzid``; aware
    datetimes are converted.
    """
    tz = ZoneInfo(tzid)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=tz)
    return ts.astimezone(tz)


def _to_local(value: DateOrDateTime) -> DateOrDateTime:
    """Express aware datetimes in DEFAULT_TZID; dates and floating times are unchanged."""
    if isinstance(value, dt.datetime) and value.tzinfo is not None:
        return value.astimezone(ZoneInfo(DEFAULT_TZID))
    return value


def _sort_key(value: DateOrDateTime) -> dt.datetime:
    """Comparable instant for a date, floating datetime or aware datetime."""
    if _is_all_day(value):
        return dt.datetime.combine(value, dt.time(), ZoneInfo(DEFAULT_TZID))
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo(DEFAULT_TZID))
    return value


def _local_day(value: DateOrDateTime) -> dt.date:
    """Calendar day of a date/datetime in DEFAULT_TZID."""
    return value if _is_all_day(value) else _sort_key(value).astimezone(ZoneInfo(DEFAULT_TZID)).date()


def _occurrence_matches(start: DateOrDateTime, wanted: DateOrDateTime) -> bool:
    """A bare date matches any occurrence that day; otherwise the starts must be equal."""
    if _is_all_day(start) != _is_all_day(wanted):
        return _local_day(start) == _local_day(wanted)
    return _sort_key(start) == _sort_key(wanted)


def _same_kind(template: DateOrDateTime, value: DateOrDateTime) -> DateOrDateTime:
    """Convert ``value`` to the kind of ``template``: date, floating time, or time in its zone."""
    if _is_all_day(template):
        return value if _is_all_day(value) else _to_local(value).date()
    if template.tzinfo is None:
        return _to_local(value).replace(tzinfo=None) if value.tzinfo else value
    return value.astimezone(template.tzinfo) if value.tzinfo else value.replace(tzinfo=template.tzinfo)


def _coerce_span(start: DateOrDateTime, end: DateOrDateTime, tzid: str) -> tuple:
    """Validate a start/end pair: both dates (all-day) or both datetimes in ``tzid``."""
    if _is_all_day(start) != _is_all_day(end):
        raise ValueError("start and end must both be dates (all-day) or both be datetimes")
    if _is_all_day(start):
        if end == start:
            end = start + dt.timedelta(days=1)  # start == end on a date means that one day
    else:
        start, end = _normalize_to_tz(start, tzid), _normalize_to_tz(end, tzid)
    if end <= start:
        raise ValueError("end must be after start (end is exclusive)")
    return start, end


def _text(comp, name: str) -> str:
    """Return a text property as str ('' if absent)."""
    value = comp.get(name)
    return str(value) if value is not None else ""


def _set_text(comp, name: str, value: str) -> None:
    """Replace a text property; an empty string removes it."""
    comp.pop(name, None)
    if value:
        comp.add(name, value)


def _event_end(comp) -> Optional[DateOrDateTime]:
    """Return DTEND, or DTSTART + DURATION, or None."""
    if comp.get("DTEND") is not None:
        return comp.decoded("DTEND")
    if comp.get("DURATION") is not None:
        return comp.decoded("DTSTART") + comp.decoded("DURATION")
    return None


def _event_row(comp, calendar_name: str, raw: Optional[str] = None) -> Dict[str, Any]:
    """Row returned by list_events for one VEVENT or expanded occurrence."""
    start = comp.decoded("dtstart")
    end = _event_end(comp)
    description = _text(comp, "DESCRIPTION")
    if len(description) > DESCRIPTION_LIMIT:
        description = description[:DESCRIPTION_LIMIT] + "..."
    row: Dict[str, Any] = {
        "uid": _text(comp, "UID"),
        "summary": _text(comp, "SUMMARY"),
        "start": _to_iso(_to_local(start)),
        "end": _to_iso(_to_local(end)),
        "all_day": _is_all_day(start),
        "location": _text(comp, "LOCATION") or None,
        "description": description or None,
        "calendar": calendar_name,
        "recurring": comp.get("RECURRENCE-ID") is not None or comp.get("RRULE") is not None,
    }
    if raw is not None:
        row["raw"] = raw
    return row


def _master_vevent(ical, uid: str):
    """Return the series VEVENT for ``uid`` (the one without RECURRENCE-ID)."""
    events = [c for c in ical.walk("VEVENT") if _text(c, "UID") == uid] or list(ical.walk("VEVENT"))
    for comp in events:
        if comp.get("RECURRENCE-ID") is None:
            return comp
    return events[0]


def _exdates(comp) -> List[DateOrDateTime]:
    """Return all EXDATE values of a component."""
    prop = comp.get("EXDATE")
    if prop is None:
        return []
    props = prop if isinstance(prop, list) else [prop]
    return [d.dt for p in props for d in p.dts]


def _drop_overrides(ical, uid: str, recurrence_id: Optional[DateOrDateTime] = None) -> None:
    """Remove modified occurrences of ``uid``: all of them, or the one at ``recurrence_id``."""
    keep = []
    for comp in ical.subcomponents:
        is_override = comp.name == "VEVENT" and _text(comp, "UID") == uid and comp.get("RECURRENCE-ID") is not None
        if is_override and (
            recurrence_id is None or _sort_key(comp.decoded("RECURRENCE-ID")) == _sort_key(recurrence_id)
        ):
            continue
        keep.append(comp)
    ical.subcomponents[:] = keep


def _shift_exceptions(ical, master, delta: dt.timedelta) -> None:
    """Move EXDATEs and RECURRENCE-IDs with the series so they still line up."""
    exdates = _exdates(master)
    master.pop("EXDATE", None)
    for value in exdates:
        master.add("EXDATE", value + delta)
    uid = _text(master, "UID")
    for comp in ical.walk("VEVENT"):
        if comp is not master and _text(comp, "UID") == uid and comp.get("RECURRENCE-ID") is not None:
            rid = comp.decoded("RECURRENCE-ID")
            comp.pop("RECURRENCE-ID")
            comp.add("RECURRENCE-ID", rid + delta)


def _reschedule(ical, master, start: Optional[str], end: Optional[str], tzid: str) -> None:
    """Apply new start/end to the series; with only ``start``, keep the duration."""
    old_start = master.decoded("DTSTART")
    old_end = _event_end(master)

    new_start = _parse_when(start) if start is not None else old_start
    if end is not None:
        new_end = _parse_when(end)
    elif old_end is not None and _is_all_day(new_start) == _is_all_day(old_start):
        new_end = new_start + (old_end - old_start)
    else:
        new_end = new_start + (dt.timedelta(days=1) if _is_all_day(new_start) else dt.timedelta(hours=1))
    new_start, new_end = _coerce_span(new_start, new_end, tzid)

    if _is_all_day(new_start) == _is_all_day(old_start):
        old_ref = old_start if _is_all_day(old_start) else _normalize_to_tz(old_start, tzid)
        delta = new_start - old_ref
        if delta:
            _shift_exceptions(ical, master, delta)

    for name in ("DTSTART", "DTEND", "DURATION"):
        master.pop(name, None)
    master.add("DTSTART", new_start)
    master.add("DTEND", new_end)


def _find_occurrence(ical, uid: str, wanted: DateOrDateTime) -> Optional[DateOrDateTime]:
    """RECURRENCE-ID (original start) of the occurrence of ``uid`` at ``wanted``, or None."""
    anchor = _sort_key(wanted)
    for inst in recurring_ical_events.of(ical).between(anchor - dt.timedelta(days=1), anchor + dt.timedelta(days=2)):
        if _text(inst, "UID") != uid or not _occurrence_matches(inst.decoded("DTSTART"), wanted):
            continue
        if inst.get("RECURRENCE-ID") is not None:
            return inst.decoded("RECURRENCE-ID")
        return inst.decoded("DTSTART")
    return None


def _touch(comp) -> None:
    """Refresh timestamps (caldav's save() bumps an existing SEQUENCE)."""
    now = dt.datetime.now(dt.timezone.utc)
    for name in ("DTSTAMP", "LAST-MODIFIED"):
        comp.pop(name, None)
    comp.add("DTSTAMP", now)
    comp.add("LAST-MODIFIED", now)


def _save(target, ical) -> None:
    """Write an edited calendar object back to the server."""
    target.data = ical.to_ical().decode()
    target.save()


def _build_vevent_ics(
    uid: str,
    summary: str,
    start: DateOrDateTime,
    end: DateOrDateTime,
    description: Optional[str],
    location: Optional[str],
    rrule: Optional[str],
) -> str:
    """Build a single-VEVENT ICS blob. Date start/end produce an all-day event."""
    event = IEvent()
    event.add("UID", uid)
    event.add("DTSTAMP", dt.datetime.now(dt.timezone.utc))
    event.add("SUMMARY", summary)
    event.add("DTSTART", start)
    event.add("DTEND", end)
    if location:
        event.add("LOCATION", location)
    if description:
        event.add("DESCRIPTION", description)
    if rrule:
        event.add("RRULE", vRecur.from_ical(rrule))

    cal = ICalendar()
    cal.add("PRODID", "-//icloud-mcp//EN")
    cal.add("VERSION", "2.0")
    cal.add_component(event)
    return cal.to_ical().decode()

def _build_rrule(
    recurrence: Optional[Dict[str, Any]],
    tzid: str,
    dtstart: Optional[DateOrDateTime] = None,
) -> Optional[str]:
    """
    Build an RFC5545 RRULE value from a high-level recurrence dict.

    recurrence:
      {
        "frequency": "daily" | "weekly" | "monthly" | "yearly" | "custom",
        "interval": int (default 1),
        "by_weekday": ["MO","TU",...],      # optional, for weekly/custom
        "by_monthday": [1,15,...],         # optional, for monthly/custom
        "end": {
          "type": "on_date",               # UNTIL
          "date": "YYYY-MM-DD" | ISO dt
          # or
          # "type": "after_occurrences",   # COUNT
          # "count": int
        },
        # for frequency == "custom":
        # "rrule": "FREQ=...;BYDAY=...;..."
      }
    """
    if not recurrence:
        return None

    freq = (recurrence.get("frequency") or "").lower()
    if not freq:
        return None

    # Custom raw RRULE passthrough
    if freq == "custom":
        raw = recurrence.get("rrule")
        return str(raw).strip() if raw else None

    freq_map = {
        "daily": "DAILY",
        "weekly": "WEEKLY",
        "monthly": "MONTHLY",
        "yearly": "YEARLY",
    }
    if freq not in freq_map:
        return None

    parts: List[str] = [f"FREQ={freq_map[freq]}"]

    interval = recurrence.get("interval")
    if isinstance(interval, int) and interval > 1:
        parts.append(f"INTERVAL={interval}")

    by_weekday = recurrence.get("by_weekday") or []
    if by_weekday:
        days = [str(d).upper() for d in by_weekday]
        parts.append(f"BYDAY={','.join(days)}")
    elif freq == "weekly" and dtstart is not None:
        # Default weekly: same weekday as dtstart
        weekday_map = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
        parts.append(f"BYDAY={weekday_map[dtstart.weekday()]}")

    by_monthday = recurrence.get("by_monthday") or []
    if by_monthday:
        days = [str(int(d)) for d in by_monthday]
        parts.append(f"BYMONTHDAY={','.join(days)}")

    end = recurrence.get("end") or {}
    end_type = (end.get("type") or "").lower()
    if end_type == "on_date":
        date_str = end.get("date")
        if date_str:
            # Interpret as local in tzid, convert to UTC, format as UNTIL=...Z
            try:
                if len(date_str) == 10:
                    y, m, d = map(int, date_str.split("-"))
                    local_dt = dt.datetime(y, m, d, 23, 59, 59)
                else:
                    local_dt = dt.datetime.fromisoformat(date_str)
                if dtstart is not None and _is_all_day(dtstart):
                    # RFC 5545: UNTIL must be a DATE when DTSTART is a DATE
                    parts.append(f"UNTIL={local_dt.strftime('%Y%m%d')}")
                else:
                    if local_dt.tzinfo is None:
                        local_dt = local_dt.replace(tzinfo=ZoneInfo(tzid))
                    until_utc = local_dt.astimezone(dt.timezone.utc)
                    until_str = until_utc.strftime("%Y%m%dT%H%M%SZ")
                    parts.append(f"UNTIL={until_str}")
            except Exception:
                # If parsing fails, skip UNTIL
                pass
    elif end_type == "after_occurrences":
        count = end.get("count")
        if isinstance(count, int) and count > 0:
            parts.append(f"COUNT={count}")

    return ";".join(parts) if parts else None


def _require_rrule(
    recurrence: Dict[str, Any],
    tzid: str,
    dtstart: DateOrDateTime,
) -> str:
    """Like ``_build_rrule``, but raise on a recurrence it can't turn into an RRULE."""
    rrule = _build_rrule(recurrence, tzid=tzid, dtstart=dtstart)
    if not rrule:
        raise ValueError(
            "Unrecognized recurrence: need frequency daily|weekly|monthly|yearly, "
            "or frequency 'custom' with an 'rrule' string"
        )
    return rrule

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

        start, end = _scan_window()

        rows: List[Dict[str, Any]] = []
        for cal in _all_calendars():
            calname = _cal_name(cal) or str(cal.url)
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
                        "snippet": f"{when} - {calname}",
                    })
        return rows[:200]

    @mcp.tool(name="fetch")
    def fetch(ids: List[str]) -> List[Dict[str, Any]]:
        """
        Fetch raw ICS for ids returned by search().
        Returns [{ id, mimeType: 'text/calendar', content }]
        """
        ids = ids or []
        calendars = {str(calendar.url): calendar for calendar in _all_calendars()}
        start, end = _scan_window()

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
        calendars = _all_calendars()
        out: List[Dict[str, Any]] = []
        for calendar in calendars:
            out.append(
                {
                    "name": _cal_name(calendar),
                    "url": str(calendar.url),
                    "id": getattr(calendar, "id", None),
                }
            )
        return out

    @mcp.tool()
    def list_calendars_with_events(
        start: str,
        end: str,
        expand_recurring: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Return calendars that have at least one event between ISO datetimes
        [start, end).

        Each item mirrors ``list_calendars`` but is filtered to only those
        calendars that contain at least one matching event in the range.
        """
        s = _parse_iso(start)
        e = _parse_iso(end)

        calendars = _all_calendars()
        out: List[Dict[str, Any]] = []

        for calendar in calendars:
            try:
                has_event = False
                for _ in calendar.search(event=True, start=s, end=e, expand=expand_recurring):
                    has_event = True
                    break
                if has_event:
                    out.append(
                        {
                            "name": _cal_name(calendar),
                            "url": str(calendar.url),
                            "id": getattr(calendar, "id", None),
                        }
                    )
            except dav_error.DAVError as exc:
                log.warning("CalDAV search failed for calendar %s: %s", _cal_name(calendar), exc)
            except Exception:
                log.exception("Unexpected error while scanning calendar %r for events", _cal_name(calendar))

        return out

    @mcp.tool()
    def list_events(
        start: str,
        end: str,
        calendar_name_or_url: Optional[str] = None,
        expand_recurring: bool = True,
        query: Optional[str] = None,
        include_raw: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        List events between ISO datetimes [start, end), sorted by start.

        calendar_name_or_url: display name or CalDAV URL; omit to search all calendars.
        query: optional case-insensitive filter on summary, location and description.
        include_raw: also return each event's ICS text (large; off by default).

        Times come back in the server's TZID. All-day events have all_day=true
        and date-only start/end (end is exclusive). For recurring events each
        occurrence is its own row sharing the series uid; pass its start as
        delete_event(occurrence_start=...) to remove just that occurrence.
        """
        s = _normalize_to_tz(_parse_iso(start), DEFAULT_TZID)
        e = _normalize_to_tz(_parse_iso(end), DEFAULT_TZID)
        needle = (query or "").strip().lower()
        calendars = [_resolve_calendar(calendar_name_or_url)] if calendar_name_or_url else _all_calendars()

        rows: List[tuple] = []
        for calendar in calendars:
            calname = _cal_name(calendar) or str(calendar.url)
            try:
                events = calendar.search(event=True, start=s, end=e, expand=expand_recurring)
            except dav_error.DAVError as exc:
                if calendar_name_or_url:
                    raise
                log.warning("CalDAV search failed for calendar %s: %s", calname, exc)
                continue
            for ev in events:
                comp = ev.component  # icalendar.Event
                if needle:
                    haystack = " ".join(_text(comp, k) for k in ("SUMMARY", "LOCATION", "DESCRIPTION")).lower()
                    if needle not in haystack:
                        continue
                row = _event_row(comp, calname, raw=ev.data if include_raw else None)
                rows.append((_sort_key(comp.decoded("dtstart")), row))

        rows.sort(key=lambda pair: pair[0])
        return [row for _, row in rows]

    @mcp.tool()
    def create_event(
        calendar_name_or_url: str,
        summary: str,
        start: str,
        end: str,
        tzid: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        recurrence: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create an event in the given calendar.

        start/end: ISO datetimes, local or '...Z' for UTC. For an all-day
                  event pass dates ('YYYY-MM-DD'); end is exclusive, so a
                  single day is start=D, end=D+1.
        tzid:     IANA TZ name (e.g., 'America/New_York'); used if times are naive.
        recurrence: optional dict, e.g.:

          {
            "frequency": "daily" | "weekly" | "monthly" | "yearly" | "custom",
            "interval": 1,
            "by_weekday": ["MO","WE"],
            "by_monthday": [1,15],
            "end": {
              "type": "on_date",           # or "after_occurrences"
              "date": "2025-12-31",       # for on_date
              # or:
              # "type": "after_occurrences",
              # "count": 10
            },
            # for custom:
            # "rrule": "FREQ=MONTHLY;BYDAY=MO,TU;BYSETPOS=1"
          }
        """
        tzid = tzid or DEFAULT_TZID

        s, e = _coerce_span(_parse_when(start), _parse_when(end), tzid)
        rrule = _require_rrule(recurrence, tzid=tzid, dtstart=s) if recurrence else None

        cal = _resolve_calendar(calendar_name_or_url)

        uid = os.urandom(16).hex() + "@icloud-mcp"

        ics_text = _build_vevent_ics(
            uid=uid,
            summary=summary,
            start=s,
            end=e,
            description=description,
            location=location,
            rrule=rrule,
        )

        cal.save_event(ics_text)
        return uid

    @mcp.tool()
    def update_event(
        calendar_name_or_url: str,
        uid: str,
        summary: Optional[str] = None,
        start: Optional[str] = None,   # ISO datetime, or date for all-day
        end: Optional[str] = None,     # ISO datetime, or date for all-day
        tzid: Optional[str] = None,
        description: Optional[str] = None,
        location: Optional[str] = None,
        recurrence: Optional[Dict[str, Any]] = None,
        clear_recurrence: bool = False,
    ) -> bool:
        """
        Update the event identified by UID. Edits it in place, so alarms,
        attendees, exceptions and other fields are kept.

        - For recurring events this changes the whole series; its EXDATEs and
          moved occurrences shift along when the start moves.
        - Only start given: the event keeps its duration.
        - description/location: omit to keep, "" to clear.
        - recurrence replaces the RRULE (same shape as create_event).
        - clear_recurrence=True removes the RRULE and all exceptions; it wins
          over recurrence.
        Returns False if the UID is not found.
        """
        tzid = tzid or DEFAULT_TZID

        cal = _resolve_calendar(calendar_name_or_url)
        target = _find_event(cal, uid)
        if target is None:
            return False

        ical = ICalendar.from_ical(target.data)
        master = _master_vevent(ical, uid)

        if summary is not None:
            _set_text(master, "SUMMARY", summary)
        if description is not None:
            _set_text(master, "DESCRIPTION", description)
        if location is not None:
            _set_text(master, "LOCATION", location)

        if start is not None or end is not None:
            _reschedule(ical, master, start, end, tzid)

        if clear_recurrence:
            for name in ("RRULE", "RDATE", "EXDATE"):
                master.pop(name, None)
            _drop_overrides(ical, uid)
        elif recurrence is not None:
            rrule = _require_rrule(recurrence, tzid=tzid, dtstart=master.decoded("DTSTART"))
            master.pop("RRULE", None)
            master.add("RRULE", vRecur.from_ical(rrule))

        _touch(master)
        _save(target, ical)
        return True

    @mcp.tool()
    def delete_event(
        calendar_name_or_url: str,
        uid: str,
        occurrence_start: Optional[str] = None,
    ) -> bool:
        """
        Delete an event by UID from the given calendar.

        occurrence_start: for a recurring event, the start of the one
        occurrence to delete, as list_events returned it; the rest of the
        series is kept. Omit to delete the whole event or series.
        Returns True if deleted, else False (not found).
        """
        cal = _resolve_calendar(calendar_name_or_url)
        target = _find_event(cal, uid)
        if target is None:
            return False

        if occurrence_start is None:
            target.delete()
            return True

        wanted = _parse_when(occurrence_start)
        if not _is_all_day(wanted):
            wanted = _normalize_to_tz(wanted, DEFAULT_TZID)

        ical = ICalendar.from_ical(target.data)
        master = _master_vevent(ical, uid)
        recurrence_id = _find_occurrence(ical, uid, wanted)
        if recurrence_id is None:
            return False

        if master.get("RRULE") is None and master.get("RDATE") is None:
            target.delete()  # not recurring: the occurrence is the event
            return True

        _drop_overrides(ical, uid, recurrence_id)
        master.add("EXDATE", _same_kind(master.decoded("DTSTART"), recurrence_id))
        _touch(master)
        _save(target, ical)
        return True

# Main

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="iCloud CalDAV MCP server")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default=os.environ.get("MCP_TRANSPORT", "http"),
        help="http serves HOST:PORT/mcp; stdio is for local clients such as Claude Code/Desktop",
    )
    args = parser.parse_args()

    log.info("CalDAV: %s  Apple ID: %r  TZ: %s  DR_ONLY=%s", CALDAV_URL, APPLE_ID, DEFAULT_TZID, DR_ONLY)
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        log.info("Starting MCP HTTP server on %s:%s", SERVER_HOST, SERVER_PORT)
        mcp.run(transport="http", host=SERVER_HOST, port=SERVER_PORT, path="/mcp")
