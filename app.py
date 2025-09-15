from flask import Flask, request, jsonify
from datetime import datetime, timezone
import re
from dateutil import parser as du_parser
from zoneinfo import ZoneInfo  # Python 3.9+
import dateparser

app = Flask(__name__)

TIME_REGEX = re.compile(r"\b(\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?)\b", re.I)
OFFSET_REGEX = re.compile(r"(Z|[+\-]\d{2}:?\d{2})$")

def has_time_part(s: str) -> bool:
    s = s.strip()
    if TIME_REGEX.search(s):
        return True
    # Also catch compact forms like 2025-09-15T15:00, 15:00Z, etc.
    if "T" in s:
        try:
            du_parser.isoparse(s)
            return True
        except Exception:
            pass
    return True

def normalize_date_only(dt: datetime) -> datetime:
    # Return midnight UTC with the same Y-M-D (no shift)
    return datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=timezone.utc)

def parse_fuzzy(q: str, prefer_dmy: bool, assume_current_year: bool, ref_tz: str | None):
    """
    Try robust parse with dateparser first, then dateutil as fallback.
    """
    if not q or not q.strip():
        return None

    settings = {
        "DATE_ORDER": "DMY" if prefer_dmy else "MDY",
        "PREFER_DAY_OF_MONTH": "first",
        "RELATIVE_BASE": datetime.now(),   # used for missing pieces
        "RETURN_AS_TIMEZONE_AWARE": False, # we'll apply tz rules ourselves
        "STRICT_PARSING": False,
    }
    if not assume_current_year:
        # If year is missing, dateparser will pick current year anyway; we’ll just flag precision later.
        pass

    dt = dateparser.parse(q, settings=settings, languages=["en"])
    if dt:
        return dt

    # Fallback to dateutil
    try:
        return du_parser.parse(q, dayfirst=prefer_dmy, yearfirst=False, fuzzy=True)
    except Exception:
        return None

def build_response(q, ref_tz, out_tz, prefer_dmy, assume_current_year):
    parsed = parse_fuzzy(q, prefer_dmy, assume_current_year, ref_tz)
    if not parsed:
        return {
            "ok": False,
            "input": q,
            "error": "Could not parse date string",
            "parser": "dateparser + python-dateutil",
            "ref_timezone": ref_tz,
        }

    # Determine if input had a time part
    time_present = has_time_part(q)

    # Detect whether original text included an explicit offset (e.g. Z or +01:00)
    explicit_offset = bool(OFFSET_REGEX.search(q.strip()))

    # If we have a timezone name for reference, use it for time-bearing inputs
    ref_zone = None
    if ref_tz:
        try:
            ref_zone = ZoneInfo(ref_tz)
        except Exception:
            ref_zone = None

    # Components/precision
    precision = {
        "hasYear": parsed.year is not None,
        "hasMonth": parsed.month is not None,
        "hasDay": parsed.day is not None,
        "hasTime": time_present,
    }

    notes = []
    if not time_present:
        notes.append("No time provided; defaulted to 00:00 and treated as date-only (no timezone shift)")
    if not precision["hasYear"]:
        notes.append("No year provided; used reference year")
    notes.append("DMY preference enabled" if prefer_dmy else "DMY preference disabled")

    # Build final datetimes
    if not time_present:
        # date-only → keep date as-is, no tz shift
        out_dt = normalize_date_only(parsed)
    else:
        # With time → apply timezone logic.
        # If the string included an explicit offset (Z/+hh:mm), dateutil likely kept it,
        # but our `parsed` is naive. We treat naive as ref_tz, then convert to out_tz.
        if ref_zone is None and not explicit_offset:
            # No ref_tz → assume UTC (deterministic)
            ref_zone = timezone.utc

        if explicit_offset:
            # Try to re-parse strictly as ISO to capture the offset, else assume ref_tz
            try:
                iso_dt = du_parser.isoparse(q)
                if iso_dt.tzinfo is None:
                    iso_dt = iso_dt.replace(tzinfo=ref_zone or timezone.utc)
                out_dt = iso_dt.astimezone(ZoneInfo(out_tz)) if out_tz else iso_dt.astimezone(timezone.utc)
            except Exception:
                # fallback: use parsed as ref_tz
                aware = parsed.replace(tzinfo=ref_zone or timezone.utc)
                out_dt = aware.astimezone(ZoneInfo(out_tz)) if out_tz else aware.astimezone(timezone.utc)
        else:
            aware = parsed.replace(tzinfo=ref_zone or timezone.utc)
            out_dt = aware.astimezone(ZoneInfo(out_tz)) if out_tz else aware.astimezone(timezone.utc)

    iso_utc = out_dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    iso_in_tz = out_dt.replace(microsecond=0).isoformat()

    return {
        "ok": True,
        "input": q,
        "parser": "dateparser + python-dateutil",
        "ref_timezone": ref_tz or None,
        "output_timezone": (out_tz or "UTC"),
        "iso_utc": iso_utc,
        "iso_in_tz": iso_in_tz,
        "unix_ms": int(out_dt.timestamp() * 1000),
        "components": {
            "year": out_dt.year,
            "month": out_dt.month,
            "day": out_dt.day,
            "hour": out_dt.hour,
            "minute": out_dt.minute,
            "second": out_dt.second,
        },
        "precision": precision,
        "notes": notes,
    }

@app.get("/parse")
def parse_endpoint():
    q = request.args.get("q", "")
    ref_tz = request.args.get("ref_tz")  # e.g. Europe/London
    out_tz = request.args.get("out_tz") or "UTC"
    prefer_dmy = request.args.get("prefer_dmy", "1") != "0"     # default true
    assume_current_year = request.args.get("assume_current_year", "1") != "0"

    res = build_response(q, ref_tz, out_tz, prefer_dmy, assume_current_year)
    return jsonify(res)
