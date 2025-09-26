from copy import deepcopy
from flask import Flask, request, jsonify
from datetime import datetime, timezone
import re
from dateutil import parser as du_parser
from zoneinfo import ZoneInfo
import dateparser

app = Flask(__name__)

TIME_REGEX = re.compile(r"\b(\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?)\b", re.I)
OFFSET_REGEX = re.compile(r"(Z|[+\-]\d{2}:?\d{2})$")

def has_time_part(s: str) -> bool:
    s = s.strip()
    if TIME_REGEX.search(s):
        return True
    if "T" in s:
        try:
            du_parser.isoparse(s)
            return True
        except Exception:
            pass
    return True

def normalize_date_only(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=timezone.utc)

def parse_fuzzy(q: str, prefer_dmy: bool, assume_current_year: bool, ref_tz: str | None):
    if not q or not q.strip():
        return None
    s = q.strip()

    # 1) Strict ISO date-only: always Y-M-D, never flip month/day
    if ISO_DATE_ONLY.match(s):
        try:
            return datetime.fromisoformat(s)
        except Exception:
            pass

    # 2) ISO datetime: prefer isoparse first so offsets and ordering are honored
    if "T" in s:
        try:
            return du_parser.isoparse(s)
        except Exception:
            pass

    # 3) Fallbacks: dateparser first, then dateutil.parse with dayfirst according to prefer_dmy
    settings = {
        "DATE_ORDER": "DMY" if prefer_dmy else "MDY",
        "PREFER_DAY_OF_MONTH": "first",
        "RELATIVE_BASE": datetime.now(),
        "RETURN_AS_TIMEZONE_AWARE": False,
        "STRICT_PARSING": False,
    }
    dt = dateparser.parse(s, settings=settings, languages=["en"])
    if dt:
        # Optionally: if assume_current_year and input had no explicit year, pin to current year
        if assume_current_year and re.search(r"\b\d{4}\b", s) is None:
            now = datetime.now()
            dt = dt.replace(year=now.year)
        return dt

    try:
        return du_parser.parse(s, dayfirst=prefer_dmy, yearfirst=False, fuzzy=True)
    except Exception:
        return None

def build_response(q, ref_tz, out_tz, prefer_dmy=True, assume_current_year=True):
    parsed = parse_fuzzy(q, prefer_dmy, assume_current_year, ref_tz)
    if not parsed:
        return None, {"error": "Could not parse date string", "input": q}

    time_present = has_time_part(q)
    explicit_offset = bool(OFFSET_REGEX.search(q.strip()))

    ref_zone = None
    if ref_tz:
        try:
            ref_zone = ZoneInfo(ref_tz)
        except Exception:
            ref_zone = None

    if not time_present:
        out_dt = normalize_date_only(parsed)
    else:
        if ref_zone is None and not explicit_offset:
            ref_zone = timezone.utc
        if explicit_offset:
            try:
                iso_dt = du_parser.isoparse(q)
                if iso_dt.tzinfo is None:
                    iso_dt = iso_dt.replace(tzinfo=ref_zone or timezone.utc)
                out_dt = iso_dt.astimezone(ZoneInfo(out_tz)) if out_tz else iso_dt.astimezone(timezone.utc)
            except Exception:
                aware = parsed.replace(tzinfo=ref_zone or timezone.utc)
                out_dt = aware.astimezone(ZoneInfo(out_tz)) if out_tz else aware.astimezone(timezone.utc)
        else:
            aware = parsed.replace(tzinfo=ref_zone or timezone.utc)
            out_dt = aware.astimezone(ZoneInfo(out_tz)) if out_tz else aware.astimezone(timezone.utc)

    out = {
        "iso_utc": out_dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "iso_in_tz": out_dt.replace(microsecond=0).isoformat(),
        "unix_ms": int(out_dt.timestamp() * 1000),
        "components": {
            "year": out_dt.year, "month": out_dt.month, "day": out_dt.day,
            "hour": out_dt.hour, "minute": out_dt.minute, "second": out_dt.second,
        },
        "has_time": time_present,
    }
    return out, None

def _get_by_path(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur

def _set_by_path(obj, path, value):
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value

@app.get("/parse")
def parse_endpoint():
    q = request.args.get("q", "")
    ref_tz = request.args.get("ref_tz")  # e.g. Europe/London
    out_tz = request.args.get("out_tz") or "UTC"
    prefer_dmy = request.args.get("prefer_dmy", "1") != "0"     # default true
    assume_current_year = request.args.get("assume_current_year", "1") != "0"

    result, err = build_response(q, ref_tz, out_tz, prefer_dmy, assume_current_year)
    if err:
        return jsonify(err), 400
    return jsonify(result)

@app.post("/format")
def format_endpoint():
    payload = request.get_json(force=True, silent=True) or {}
    items = payload.get("items", [])
    ref_tz = payload.get("ref_tz")
    out_tz = payload.get("out_tz") or "UTC"
    prefer_dmy = bool(int(str(payload.get("prefer_dmy", "1")) != "0"))
    assume_current_year = bool(int(str(payload.get("assume_current_year", "1")) != "0"))
    suffix = payload.get("suffix", "_local")
    replace = bool(payload.get("replace", False))

    result_items = []
    for it in items:
        data = deepcopy(it.get("data", {}))
        fields = it.get("fields", [])
        meta = {}
        for path in fields:
            raw = _get_by_path(data, path)
            if raw is None:
                meta[path] = {"error": "missing"}
                continue
            converted, err = build_response(
                str(raw), ref_tz=ref_tz, out_tz=out_tz,
                prefer_dmy=prefer_dmy, assume_current_year=assume_current_year
            )
            if err:
                meta[path] = err
                continue

            # write back
            if replace:
                _set_by_path(data, path, converted["iso_in_tz"])
            else:
                # add alongside: <field><suffix>
                if "." in path:
                    parent = ".".join(path.split(".")[:-1])
                    leaf = path.split(".")[-1] + suffix
                    parent_obj = _get_by_path(data, parent) or {}
                    parent_obj[leaf] = converted["iso_in_tz"]
                    _set_by_path(data, parent, parent_obj)
                else:
                    data[path + suffix] = converted["iso_in_tz"]
            meta[path] = converted

        result_items.append({"data": data, "meta": meta})

    return jsonify({"ok": True, "items": result_items})
