import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from flask import Flask, jsonify, send_from_directory, make_response, request
from flask_cors import CORS


app = Flask(__name__, static_folder=".")
CORS(app)

WL_API_KEY = os.environ.get("WL_API_KEY", "").strip()
WL_API_SECRET = os.environ.get("WL_API_SECRET", "").strip()
WL_STATION_ID = os.environ.get("WL_STATION_ID", "238059").strip()


def sign_weatherlink(params: dict) -> str:
    """
    WeatherLink v2 signs the concatenation of sorted key/value pairs using the API secret.
    """
    msg = "".join(k + str(params[k]) for k in sorted(params))
    return hmac.new(WL_API_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def fetch_json(url: str, timeout: int = 15):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "virreyes-weather/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
        return response.status, json.loads(raw)


def weatherlink_current_raw():
    if not WL_API_KEY or not WL_API_SECRET or not WL_STATION_ID:
        return 500, {
            "error": "Missing WeatherLink environment variables",
            "required": ["WL_API_KEY", "WL_API_SECRET", "WL_STATION_ID"],
        }

    t = int(time.time())
    params = {
        "api-key": WL_API_KEY,
        "station-id": WL_STATION_ID,
        "t": t,
    }
    signature = sign_weatherlink(params)
    query = urllib.parse.urlencode(
        {
            "api-key": WL_API_KEY,
            "t": t,
            "api-signature": signature,
        }
    )
    url = f"https://api.weatherlink.com/v2/current/{WL_STATION_ID}?{query}"

    try:
        return fetch_json(url)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(body)
        except Exception:
            body = {"error": body}
        return e.code, body
    except Exception as exc:
        return 502, {"error": str(exc)}


def first_number(*values):
    for value in values:
        if isinstance(value, (int, float)):
            return float(value)
    return None


def f_to_c(value):
    return None if value is None else (value - 32.0) * 5.0 / 9.0


def mph_to_kmh(value):
    return None if value is None else value * 1.609344


def inhg_to_hpa(value):
    return None if value is None else value * 33.8638866667


def inch_to_mm(value):
    return None if value is None else value * 25.4


def normalize_weatherlink(raw: dict) -> dict:
    """
    WeatherLink current payloads vary by station/sensor type.
    This parser searches every sensor data record and picks the first usable values.
    """
    records = []
    for sensor in raw.get("sensors", []):
        for datum in sensor.get("data", []):
            if isinstance(datum, dict):
                records.append(datum)

    temp = humidity = dew_point = wind_speed = wind_dir = pressure = rain_day = rain_rate = None
    rain_24h = rain_storm = rain_60 = None
    ts = None
    rain_size_seen = []

    for d in records:
        temp = first_number(temp, d.get("temp"), d.get("temperature"))
        humidity = first_number(humidity, d.get("hum"), d.get("humidity"))
        dew_point = first_number(dew_point, d.get("dew_point"), d.get("dewpoint"))
        wind_speed = first_number(
            wind_speed,
            d.get("wind_speed_last"),
            d.get("wind_speed_avg_last_1_min"),
            d.get("wind_speed_avg_last_2_min"),
            d.get("wind_speed_hi_last_10_min"),
        )
        wind_dir = first_number(wind_dir, d.get("wind_dir_last"), d.get("wind_dir_scalar_avg_last_1_min"))
        pressure = first_number(pressure, d.get("bar_sea_level"), d.get("bar_absolute"), d.get("bar"))
        # The station provides explicit millimeter fields — use them directly.
        rain_day = first_number(rain_day, d.get("rainfall_daily_mm"), d.get("rain_day_mm"))
        rain_24h = first_number(rain_24h, d.get("rainfall_last_24_hr_mm"))
        rain_storm = first_number(rain_storm, d.get("rain_storm_mm"))
        rain_60 = first_number(rain_60, d.get("rainfall_last_60_min_mm"))
        rain_rate = first_number(rain_rate, d.get("rain_rate_last_mm"), d.get("rain_rate_hi_mm"))
        if d.get("rain_size") is not None:
            rain_size_seen.append(d.get("rain_size"))
        ts = first_number(ts, d.get("ts"), d.get("timestamp"), d.get("time"))

    # Davis WeatherLink often returns US units. Convert conservatively.
    temp_c = f_to_c(temp) if temp is not None and temp > 45 else temp
    dew_c = f_to_c(dew_point) if dew_point is not None and dew_point > 45 else dew_point
    wind_kmh = mph_to_kmh(wind_speed) if wind_speed is not None else None
    pressure_hpa = inhg_to_hpa(pressure) if pressure is not None and pressure < 100 else pressure
    tip_mm = None
    if rain_size_seen:
        tip_mm = {1: 0.254, 2: 0.2, 3: 0.1, 4: 0.0254}.get(rain_size_seen[0])

    # All values above are already in millimeters from the WeatherLink API.
    rain_day_mm = rain_day
    rain_rate_mm = rain_rate

    if ts:
        updated_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    else:
        updated_iso = datetime.now(timezone.utc).isoformat()

    return {
        "station_id": WL_STATION_ID,
        "updated_utc": updated_iso,
        "temperature_c": None if temp_c is None else round(temp_c, 1),
        "humidity_pct": None if humidity is None else round(humidity),
        "dew_point_c": None if dew_c is None else round(dew_c, 1),
        "wind_speed_kmh": None if wind_kmh is None else round(wind_kmh, 1),
        "wind_direction_deg": None if wind_dir is None else round(wind_dir),
        "pressure_hpa": None if pressure_hpa is None else round(pressure_hpa, 1),
        "rain_day_mm": None if rain_day_mm is None else round(rain_day_mm, 1),
        "rain_rate_mm_h": None if rain_rate_mm is None else round(rain_rate_mm, 1),
        "rain_24h_mm": None if rain_24h is None else round(rain_24h, 1),
        "rain_storm_mm": None if rain_storm is None else round(rain_storm, 1),
        "rain_60min_mm": None if rain_60 is None else round(rain_60, 1),
    }


_PDIR = os.environ.get("DATA_DIR", "/data")
if not os.path.isdir(_PDIR):
    _PDIR = "/tmp"
PRESS_FILE = os.path.join(_PDIR, "press_hist.json")
try:
    with open(PRESS_FILE) as _fh:
        PRESS_HIST = json.load(_fh)
except Exception:
    PRESS_HIST = []


def record_pressure(hpa):
    """Append a pressure sample (max 1 per 2 min, keep 26 h) and return the
    3-hour tendency in hPa, normalized to exactly 3 h."""
    global PRESS_HIST
    if hpa is None:
        return None
    now = int(time.time())
    if not PRESS_HIST or now - PRESS_HIST[-1][0] >= 120:
        PRESS_HIST.append([now, hpa])
        PRESS_HIST = [p for p in PRESS_HIST if now - p[0] <= 26 * 3600]
        try:
            with open(PRESS_FILE, "w") as fh:
                json.dump(PRESS_HIST, fh)
        except Exception:
            pass
    target = now - 3 * 3600
    cands = [p for p in PRESS_HIST if abs(p[0] - target) <= 3600]
    if not cands:
        return None
    ref = min(cands, key=lambda p: abs(p[0] - target))
    span_h = (now - ref[0]) / 3600.0
    if span_h < 2.0:
        return None
    return round((hpa - ref[1]) * (3.0 / span_h), 1)


def pressure_trend_15m(hpa):
    """15-minute pressure tendency in hPa (normalized to 15 min). A sharp rise is
    the meso-high behind a convective outflow -> an early gust-front marker; a
    sharp fall can precede convective onset. Uses the same PRESS_HIST buffer as
    the 3 h tendency (record_pressure runs first to append the current sample)."""
    if hpa is None or not PRESS_HIST:
        return None
    now = int(time.time())
    target = now - 15 * 60
    cands = [p for p in PRESS_HIST if abs(p[0] - target) <= 300]
    if not cands:
        return None
    ref = min(cands, key=lambda p: abs(p[0] - target))
    span_min = (now - ref[0]) / 60.0
    if span_min < 8.0:
        return None
    return round((hpa - ref[1]) * (15.0 / span_min), 1)


# item 4: WH57 lightning via the Ecowitt cloud real_time API. Ingest + log +
# display only (no model, no ops-banner escalation here). Creds come from env
# vars; never hardcode or log them, and degrade gracefully (return None) if any
# is missing or the API errors.
_ECOWITT_CACHE = {"t": 0, "val": None}     # ~60 s TTL (WH57 transmits ~every 79 s)
_ECOWITT_LAST = {"count_day": None}        # baseline for the new_strikes delta


def _parse_ecowitt_lightning(body, now, prev_count):
    """Pure parser: (raw response, now_ts, previous count_day) -> (val|None, count_day).
    The WH57 reports today's strike count always; distance + last-strike time only
    appear AFTER the first strike, so both are optional. Distance may be km or mi
    (account setting) -> normalize to km. new_strikes is the count delta since the
    previous poll, with the local-midnight reset guarded (never negative)."""
    if not isinstance(body, dict) or body.get("code") != 0:
        return None, prev_count
    lt = (body.get("data") or {}).get("lightning") or {}
    if not lt:
        return None, prev_count
    batt = ((body.get("data") or {}).get("battery") or {}).get("lightning_sensor") or {}

    def _val(obj, cast):
        try:
            return cast((obj or {}).get("value"))
        except (TypeError, ValueError):
            return None

    count_day = _val(lt.get("count"), lambda x: int(float(x)))

    dist_obj = lt.get("distance") or {}
    dist_raw = _val(dist_obj, float)
    unit = (dist_obj.get("unit") or "").lower()
    dist_km = None
    if dist_raw is not None:
        dist_km = round(dist_raw * 1.60934, 1) if unit in ("mi", "mile", "miles") else round(dist_raw, 1)

    # the distance metric only refreshes on a strike, so its 'time' is the best
    # available last-strike timestamp (count.time is just the reading time).
    # Re-confirm on the first real strike.
    last_ts = None
    if dist_obj.get("time"):
        try:
            last_ts = int(dist_obj["time"])
        except (TypeError, ValueError):
            last_ts = None
    age_min = round((now - last_ts) / 60) if last_ts else None

    new_strikes = None
    if count_day is not None:
        if prev_count is None or count_day < prev_count:   # first read or midnight reset
            new_strikes = 0
        else:
            new_strikes = count_day - prev_count
        prev_count = count_day

    val = {"count_day": count_day, "new_strikes": new_strikes, "dist_km": dist_km,
           "last_ts": last_ts, "age_min": age_min, "battery": _val(batt, lambda x: int(float(x)))}
    return val, prev_count


def _fetch_ecowitt_lightning():
    """Cached WH57 lightning read. Always returns a dict with an 'available' flag
    (never raises): {"available": True, ...data} on success, else
    {"available": False, "reason": ...} with a NON-SECRET reason so a null result
    is debuggable in prod (creds vs API error). Caches success AND failure ~60 s,
    so a persistent error can't hammer the API. Never logs the credentials."""
    app = os.getenv("ECOWITT_APPLICATION_KEY")
    key = os.getenv("ECOWITT_API_KEY")
    mac = os.getenv("ECOWITT_MAC")
    if not (app and key and mac):
        return {"available": False, "reason": "no_creds"}
    now = int(time.time())
    if _ECOWITT_CACHE["val"] is not None and now - _ECOWITT_CACHE["t"] < 60:
        return _ECOWITT_CACHE["val"]
    try:
        params = urllib.parse.urlencode({"application_key": app, "api_key": key,
                                         "mac": mac, "call_back": "all"})
        url = "https://api.ecowitt.net/api/v3/device/real_time?" + params
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not isinstance(body, dict):
            result = {"available": False, "reason": "bad_response"}
        elif body.get("code") != 0:
            result = {"available": False, "reason": "api_code_%s" % body.get("code")}
        else:
            val, _ECOWITT_LAST["count_day"] = _parse_ecowitt_lightning(
                body, now, _ECOWITT_LAST["count_day"])
            result = ({**val, "available": True} if val is not None
                      else {"available": False, "reason": "no_lightning_field"})
    except Exception:
        result = {"available": False, "reason": "fetch_error"}
    _ECOWITT_CACHE["t"] = now            # cache success AND failure -> 60 s backoff
    _ECOWITT_CACHE["val"] = result
    return result


@app.after_request
def add_no_cache_headers(response):
    if response.content_type and ("application/json" in response.content_type or "text/html" in response.content_type):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/current")
@app.route("/current")
def current():
    status, raw = weatherlink_current_raw()
    if status != 200:
        return jsonify({"ok": False, "source": "weatherlink", "status": status, "error": raw}), status

    parsed = normalize_weatherlink(raw)
    parsed["pressure_trend_3h"] = record_pressure(parsed.get("pressure_hpa"))
    parsed["pressure_trend_15m"] = pressure_trend_15m(parsed.get("pressure_hpa"))
    parsed["lightning"] = _fetch_ecowitt_lightning()   # item 4: WH57 (None if no creds)
    return jsonify({"ok": True, "source": "weatherlink", "parsed": parsed, "raw": raw})


@app.route("/api/tile/<int:z>/<int:x>/<int:y>")
def tile_proxy(z, x, y):
    """Proxy CARTO dark basemap tiles (free for personal use, attribution shown
    in the app) so the canvas can compose them without CORS taint."""
    try:
        if not (0 <= z <= 19):
            raise ValueError("bad zoom")
        sub = "abcd"[(x + y) % 4]
        url = f"https://{sub}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png"
        req = urllib.request.Request(url, headers={"User-Agent": "virreyes-weather/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        r = make_response(data)
        r.headers["Content-Type"] = "image/png"
        r.headers["Cache-Control"] = "public, max-age=86400"
        return r
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


_FX_CACHE = {"t": 0, "data": None}
_ENS_CACHE = {"t": 0, "data": None}
FX_TTL = 600      # 10 min — forecast/env (hourly model output)
ENS_TTL = 1800    # 30 min — ensemble


@app.route("/api/forecast")
def forecast_proxy():
    """Server-cached Open-Meteo forecast shared by all clients. Serves stale on
    rate-limit so the dashboard never goes blank."""
    now = time.time()
    if _FX_CACHE["data"] and now - _FX_CACHE["t"] < FX_TTL:
        r = make_response(json.dumps(_FX_CACHE["data"]))
        r.headers["Content-Type"] = "application/json"
        r.headers["X-Cache"] = "hit"
        return r
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&hourly=temperature_2m,precipitation_probability,precipitation,weather_code,"
           "wind_speed_700hPa,wind_direction_700hPa,cape,lifted_index,freezing_level_height,"
           "wind_speed_10m,wind_direction_10m,wind_speed_500hPa,wind_direction_500hPa"
           "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
           "precipitation_probability_max,precipitation_sum"
           "&minutely_15=precipitation&current=precipitation,weather_code"
           "&past_hours=24&timezone=America%%2FMexico_City&forecast_days=5" % (LAT, LON))
    try:
        status, data = fetch_json(url)
        if status == 200 and isinstance(data, dict) and data.get("hourly"):
            _FX_CACHE["t"] = now
            _FX_CACHE["data"] = data
            r = make_response(json.dumps(data))
            r.headers["Content-Type"] = "application/json"
            r.headers["X-Cache"] = "miss"
            return r
        raise ValueError("status %s" % status)
    except Exception as exc:
        if _FX_CACHE["data"]:
            r = make_response(json.dumps(_FX_CACHE["data"]))
            r.headers["Content-Type"] = "application/json"
            r.headers["X-Cache"] = "stale"
            return r
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/ensemble")
def ensemble_proxy():
    """Server-cached Open-Meteo ensemble (precip percentiles), shared by clients."""
    now = time.time()
    if _ENS_CACHE["data"] and now - _ENS_CACHE["t"] < ENS_TTL:
        r = make_response(json.dumps(_ENS_CACHE["data"]))
        r.headers["Content-Type"] = "application/json"
        r.headers["X-Cache"] = "hit"
        return r
    url = ("https://ensemble-api.open-meteo.com/v1/ensemble?latitude=%s&longitude=%s"
           "&hourly=precipitation&models=gfs_seamless&forecast_hours=7"
           "&timezone=America%%2FMexico_City" % (LAT, LON))
    try:
        status, data = fetch_json(url)
        if status == 200 and isinstance(data, dict):
            _ENS_CACHE["t"] = now
            _ENS_CACHE["data"] = data
            r = make_response(json.dumps(data))
            r.headers["Content-Type"] = "application/json"
            return r
        raise ValueError("status %s" % status)
    except Exception as exc:
        if _ENS_CACHE["data"]:
            r = make_response(json.dumps(_ENS_CACHE["data"]))
            r.headers["Content-Type"] = "application/json"
            return r
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/dem")
def dem_endpoint():
    """Serve the cached DEM grid so the client interpolates the SAME terrain the
    server does (keeps displayed tracks and logged ETAs consistent)."""
    if _DEM:
        r = make_response(json.dumps(_DEM))
        r.headers["Content-Type"] = "application/json"
        r.headers["Cache-Control"] = "public, max-age=86400"
        return r
    return jsonify({"ready": False, "status": _DEM_STATUS}), 503


@app.route("/api/dem/fetch")
def dem_fetch_now():
    """Force a synchronous DEM fetch attempt (for testing/diagnosis)."""
    if _DEM:
        return jsonify({"ok": True, "already_loaded": True})
    try:
        ok = _dem_fetch()
        return jsonify({"ok": ok, "status": _DEM_STATUS,
                        "points": len(_DEM["elev"]) if _DEM else 0})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/rvmeta")
def rv_meta():
    """RainViewer frame catalog (free public API)."""
    try:
        req = urllib.request.Request(
            "https://api.rainviewer.com/public/weather-maps.json",
            headers={"User-Agent": "virreyes-weather/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        r = make_response(data)
        r.headers["Content-Type"] = "application/json"
        r.headers["Cache-Control"] = "public, max-age=120"
        return r
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/rvtile")
def rv_tile():
    """RainViewer radar tile via the exact frame path returned by weather-maps.json."""
    try:
        import re as _re
        fpath = request.args.get("path", "")
        z = int(request.args.get("z", 7))
        x = int(request.args.get("x", 0))
        y = int(request.args.get("y", 0))
        if not _re.fullmatch(r"/v[0-9]+/[A-Za-z0-9_\-/]+", fpath):
            raise ValueError("bad path")
        if not (0 <= z <= 7):
            raise ValueError("bad zoom")
        data = None
        for size in (512, 256):
            url = f"https://tilecache.rainviewer.com{fpath}/{size}/{z}/{x}/{y}/2/1_1.png"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "virreyes-weather/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                break
            except Exception:
                continue
        if data is None:
            raise ValueError("tile unavailable")
        r = make_response(data)
        r.headers["Content-Type"] = "image/png"
        r.headers["Cache-Control"] = "public, max-age=300"
        return r
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


# Persistent storage: use the Railway volume mounted at /data when present
# (set DATA_DIR env var to override). Falls back to /tmp if not mounted, so
# the app keeps working before the volume is attached.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
if not os.path.isdir(DATA_DIR):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        DATA_DIR = "/tmp"
EVENTLOG_FILE = os.path.join(DATA_DIR, "storm_events.jsonl")


@app.route("/api/log", methods=["POST"])
def log_event():
    """Append one storm-observation record (one JSON object per line) for later
    model training. Best-effort; never blocks the client."""
    try:
        rec = request.get_json(force=True, silent=True) or {}
        rec["server_ts"] = int(time.time())
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
        if len(line) > 20000:
            return jsonify({"ok": False, "error": "record too large"}), 413
        with open(EVENTLOG_FILE, "a") as fh:
            fh.write(line + "\n")
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/log/now")
def log_now():
    """Force one server-side log cycle immediately (for testing)."""
    try:
        n_cells, rain_rate = _auto_log_once()
        return jsonify({"ok": True, "cells": n_cells, "rain_rate": rain_rate})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def _episode_summary():
    """Group cell-bearing records into distinct storm episodes (gap > 90 min
    starts a new episode). Returns counts useful for training readiness."""
    times = []
    snaps_with_cells = 0
    try:
        with open(EVENTLOG_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                nc = o.get("n_cells")
                if nc is None:
                    nc = len(o.get("cells") or [])
                if nc and nc > 0:
                    snaps_with_cells += 1
                    ts = o.get("server_ts") or o.get("t")
                    if ts:
                        times.append(ts)
    except FileNotFoundError:
        pass
    times.sort()
    episodes = 0
    GAP = 90 * 60
    last = None
    for ts in times:
        if last is None or ts - last > GAP:
            episodes += 1
        last = ts
    return episodes, snaps_with_cells, (times[0] if times else None), (times[-1] if times else None)


@app.route("/api/log/ready")
def log_ready():
    episodes, snaps, first, last = _episode_summary()
    return jsonify({"ok": True, "episodes": episodes, "snapshots_with_cells": snaps,
                    "target_episodes": 40, "first_ts": first, "last_ts": last})


@app.route("/readiness")
def readiness_page():
    episodes, snaps, first, last = _episode_summary()
    target = 40
    pct = min(100, round(100 * episodes / target))
    import datetime as _dt
    def fmt(ts):
        if not ts:
            return "\u2014"
        return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).astimezone(
            _dt.timezone(_dt.timedelta(hours=-6))).strftime("%d %b %Y, %H:%M")
    span_days = round((last - first) / 86400, 1) if (first and last) else 0
    ready = episodes >= target
    bar_color = "#3fb950" if ready else ("#d29922" if episodes >= target * 0.5 else "#58a6ff")
    html = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Virreyes \u00b7 Datos para nowcaster</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:24px;max-width:560px;margin:0 auto}}
h1{{font-size:1.1rem;letter-spacing:.04em}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;margin:14px 0}}
.big{{font-size:2.6rem;font-weight:700;font-family:ui-monospace,monospace}}
.lbl{{font-size:.72rem;letter-spacing:.12em;text-transform:uppercase;color:#9aa3b2;margin-bottom:6px}}
.bar{{height:14px;background:#21262d;border-radius:7px;overflow:hidden;margin:14px 0}}
.fill{{height:100%;border-radius:7px;transition:width .6s;background:{bc};width:{pct}%}}
.row{{display:flex;justify-content:space-between;font-family:ui-monospace,monospace;font-size:.85rem;padding:6px 0;border-bottom:1px solid #21262d}}
.muted{{color:#9aa3b2}}
a{{color:#58a6ff}}
.tag{{display:inline-block;padding:4px 12px;border-radius:6px;font-size:.8rem;font-weight:600;font-family:ui-monospace,monospace}}
</style></head><body>
<h1>&#x26C5; Datos para el nowcaster &mdash; Estaci\u00f3n Virreyes</h1>
<div class="card">
  <div class="lbl">Episodios de tormenta registrados</div>
  <div class="big" style="color:{bc}">{ep} <span style="font-size:1.2rem;color:#9aa3b2">/ {tg}</span></div>
  <div class="bar"><div class="fill"></div></div>
  <div style="text-align:center">
    {tag}
  </div>
</div>
<div class="card">
  <div class="row"><span class="muted">Snapshots con celdas</span><span>{snaps}</span></div>
  <div class="row"><span class="muted">Episodios distintos (gap &gt;90 min)</span><span>{ep}</span></div>
  <div class="row"><span class="muted">Periodo de recoleccion</span><span>{span} d\u00edas</span></div>
  <div class="row"><span class="muted">Primer registro</span><span>{first}</span></div>
  <div class="row"><span class="muted">Ultimo registro</span><span>{last}</span></div>
</div>
<div class="card" style="font-size:.82rem;line-height:1.6;color:#aeb6c2">
  Cuando los <b>episodios distintos</b> lleguen a ~{tg}, hay suficientes datos para entrenar
  el modelo de trayectoria especifico para el Valle de M\u00e9xico. La etiqueta de
  entrenamiento es tu pluvi\u00f3metro Davis (lleg\u00f3 la lluvia y cu\u00e1ndo).<br><br>
  <a href="/api/log/export">&#x2B07; Descargar datos (JSONL)</a> &nbsp;\u00b7&nbsp;
  <a href="/api/log/stats">stats JSON</a> &nbsp;\u00b7&nbsp;
  <a href="/">&#x2190; Dashboard</a>
</div>
<p class="muted" style="font-size:.7rem;text-align:center">Recuerda descargar un respaldo periodicamente.</p>
</body></html>""".format(
        bc=bar_color, pct=pct, ep=episodes, tg=target, snaps=snaps, span=span_days,
        first=fmt(first), last=fmt(last),
        tag=('<span class="tag" style="background:#3fb95022;color:#3fb950">\u2713 LISTO PARA ENTRENAR</span>'
             if ready else
             '<span class="tag" style="background:#58a6ff22;color:#58a6ff">RECOLECTANDO \u2014 '+str(pct)+'%</span>'))
    r = make_response(html)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/api/outcomes/export")
def outcomes_export():
    """Download labeled prediction/outcome pairs (the supervised training set)."""
    try:
        with open(OUTCOME_FILE) as fh:
            data = fh.read()
    except FileNotFoundError:
        data = ""
    r = make_response(data)
    r.headers["Content-Type"] = "application/x-ndjson"
    r.headers["Content-Disposition"] = 'attachment; filename="labeled_outcomes.jsonl"'
    r.headers["Cache-Control"] = "no-store"
    return r


def _storm_day(ts):
    """CDMX (UTC-6) calendar day 'YYYY-MM-DD' for a unix timestamp."""
    import datetime as _dt
    tz = _dt.timezone(_dt.timedelta(hours=-6))
    return _dt.datetime.fromtimestamp(int(ts), tz).strftime("%Y-%m-%d")


def _onsets_with_coverage():
    """Rain onsets at Virreyes with a coverage flag. Returns
    [{"t":int, "n_cells":int, "covered":bool}].
    Onset = a wet Davis reading whose previous wet reading was >30 min earlier.
    Covered = lines up with a logged hit's measured onset, or falls inside a
    still-pending prediction's window (tight association, not a loose lookback)."""
    WET, DRY = 0.2, 30 * 60
    series = []
    try:
        with open(EVENTLOG_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                ts = o.get("server_ts") or o.get("t")
                if ts is None:
                    continue
                st = o.get("station") or {}
                nc = o.get("n_cells")
                if nc is None:
                    nc = len(o.get("cells") or [])
                series.append((int(ts), float(st.get("rain_rate") or 0), nc))
    except FileNotFoundError:
        return []
    series.sort()

    onsets, last_wet_t = [], None
    for (t, rr, nc) in series:
        if rr >= WET:
            if last_wet_t is None or (t - last_wet_t) > DRY:
                onsets.append((t, nc))
            last_wet_t = t

    TOL = 20 * 60
    hit_onsets, pendings = [], []
    if os.path.exists(OUTCOME_FILE):
        try:
            with open(OUTCOME_FILE) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if (o.get("outcome") == "hit" and o.get("t") is not None
                            and o.get("actual_min") is not None):
                        hit_onsets.append(int(o["t"]) + int(o["actual_min"]) * 60)
        except Exception:
            pass
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("t") is not None:
                        pendings.append((int(o["t"]), o.get("pred_eta_min")))
        except Exception:
            pass

    out = []
    for (t, nc) in onsets:
        cov = (any(abs(t - ho) <= TOL for ho in hit_onsets)
               or any(pt <= t <= pt + int(pe or 0) * 60 + TOL for (pt, pe) in pendings))
        out.append({"t": t, "n_cells": nc, "covered": bool(cov)})
    return out


def _recall_summary():
    """Aggregate event-level recall over all onsets (see _onsets_with_coverage)."""
    onsets = _onsets_with_coverage()
    n = len(onsets)
    covered = sum(1 for o in onsets if o["covered"])
    missed_in_situ = sum(1 for o in onsets if not o["covered"] and (o["n_cells"] or 0) == 0)
    return {"rain_onsets": n, "onsets_covered": covered, "onsets_missed": n - covered,
            "recall_pct": round(100 * covered / n) if n else None,
            "missed_in_situ": missed_in_situ}


def _mae_over(hits):
    errs = [abs(r["actual_min"] - r["pred_eta_min"]) for r in hits
            if r.get("actual_min") is not None and r.get("pred_eta_min") is not None]
    return round(sum(errs) / len(errs), 1) if errs else None


def _outcome_agg(rows):
    """Precision + ETA MAE over a list of outcome records. Also reports a
    post-fix-only ETA MAE (item 9): records carrying a frame_age_min key were
    logged WITH the radar-latency correction, so eta_mae_post_min over those is
    the clean now-anchored skill signal. The blended eta_mae_min still mixes old
    frame-anchored pairs and reads artificially high during the transition --
    read eta_mae_post_min (and pairs_post for its sample size) instead until the
    old pairs age out."""
    n = len(rows)
    hits = [r for r in rows if r.get("outcome") == "hit"]
    misses = [r for r in rows if r.get("outcome") == "miss"]
    post = [r for r in rows if "frame_age_min" in r]
    return {"pairs": n, "hits": len(hits), "misses": len(misses),
            "precision_pct": round(100 * len(hits) / n) if n else None,
            "eta_mae_min": _mae_over(hits),
            "pairs_post": len(post),
            "eta_mae_post_min": _mae_over([r for r in post if r.get("outcome") == "hit"])}


def _steering_bucket(r):
    """Proxy for advective vs weak/pulse regime from the prediction's steering speed."""
    st = r.get("steering")
    spd = st.get("spd") if isinstance(st, dict) else None
    if spd is None or spd < 10:
        return "weak", "weak/none (<10 km/h)"
    if spd < 22:
        return "mod", "moderate (10-22 km/h)"
    return "adv", "advective (>=22 km/h)"


def _skill_strata():
    """Stratify the heuristic's skill by storm-day and by steering regime, so one
    widespread day can't skew the blended baseline the learned model must beat."""
    outs = []
    if os.path.exists(OUTCOME_FILE):
        try:
            with open(OUTCOME_FILE) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        outs.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass

    onsets = _onsets_with_coverage()

    # by storm-day (precision/MAE from outcomes; recall from onsets)
    day_out, day_on = {}, {}
    for r in outs:
        if r.get("t") is not None:
            day_out.setdefault(_storm_day(r["t"]), []).append(r)
    for o in onsets:
        day_on.setdefault(_storm_day(o["t"]), []).append(o)
    by_day = []
    for d in sorted(set(day_out) | set(day_on), reverse=True):
        a = _outcome_agg(day_out.get(d, []))
        ons = day_on.get(d, [])
        cov = sum(1 for o in ons if o["covered"])
        a.update({"date": d, "onsets": len(ons), "covered": cov,
                  "missed": len(ons) - cov,
                  "recall_pct": round(100 * cov / len(ons)) if ons else None})
        by_day.append(a)

    # by steering regime
    buckets, labels = {}, {}
    for r in outs:
        k, lab = _steering_bucket(r)
        buckets.setdefault(k, []).append(r)
        labels[k] = lab
    by_steering = []
    for k in ("weak", "mod", "adv"):
        if k in buckets:
            a = _outcome_agg(buckets[k])
            a["bucket"] = k
            a["label"] = labels[k]
            by_steering.append(a)

    overall = _outcome_agg(outs)
    overall.update(_recall_summary())
    return {"ok": True, "overall": overall, "by_day": by_day, "by_steering": by_steering,
            "note": ("Per-day breakdown shows how much the aggregate is driven by single "
                     "widespread days. Steering buckets proxy advective (strong flow) vs "
                     "weak/pulse (in-situ) regimes; the heuristic is expected to verify "
                     "better under strong steering. Thresholds are tunable proxies.")}


@app.route("/api/outcomes/strata")
def outcomes_strata():
    try:
        return jsonify(_skill_strata())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def _load_event_snapshots():
    """All autolog event records (sorted by time) for precursor lookup."""
    snaps = []
    if os.path.exists(EVENTLOG_FILE):
        try:
            with open(EVENTLOG_FILE) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("t") is not None:
                        snaps.append(r)
        except Exception:
            pass
    snaps.sort(key=lambda r: r["t"])
    return snaps


def _cdmx_hour(ts):
    import datetime as _dt
    tz = _dt.timezone(_dt.timedelta(hours=-6))
    return _dt.datetime.fromtimestamp(int(ts), tz).hour


def _precursor_snapshot(snaps, onset_ts, offset_min, tol_min=4):
    """Nearest pre-onset snapshot to (onset - offset_min), within tol_min, <= onset.
    Windows are defined in MINUTES via timestamps, so this survives a cadence change."""
    target = onset_ts - offset_min * 60
    tol = tol_min * 60
    cands = [x for x in snaps if x["t"] <= onset_ts and abs(x["t"] - target) <= tol]
    if not cands:
        return None
    x = min(cands, key=lambda x: abs(x["t"] - target))
    st = x.get("station") or {}
    env = x.get("env") or {}
    temp, dew = st.get("temp_c"), st.get("dew_point_c")
    return {
        "dt_min": round((x["t"] - onset_ts) / 60),
        "cape": env.get("cape"),
        "humidity_pct": st.get("humidity_pct"),
        "temp_c": temp,
        "dew_point_c": dew,
        "t_td_spread": (round(temp - dew, 1) if temp is not None and dew is not None else None),
        "pressure_hpa": st.get("press"),
        "pressure_trend_3h": st.get("press_trend"),
        "pressure_trend_15m": st.get("press_trend_15m"),
        "rain_rate_mm_h": st.get("rain_rate"),
        "n_cells": x.get("n_cells"),
    }


def _cape_band(c):
    if c is None:
        return "unknown"
    return "<600" if c < 600 else ("600-1200" if c < 1200 else "1200+")


def _rh_band(h):
    if h is None:
        return "unknown"
    return "<70" if h < 70 else ("70-85" if h < 85 else "85+")


def _misses_summary():
    """Missed rain onsets + the environment precursor logged just before each.
    Analytics only -- turns the recall blind spots into a labeled precursor dataset.
    The missed-onset set comes from _onsets_with_coverage() (the SAME helper behind
    missed_in_situ in /api/outcomes/skill) so the two cannot disagree."""
    onsets = [o for o in _onsets_with_coverage() if not o["covered"]]
    snaps = _load_event_snapshots()
    rows = []
    for o in sorted(onsets, key=lambda o: o["t"], reverse=True):
        ts = o["t"]
        in_situ = (o.get("n_cells") or 0) == 0
        pre = {str(k): _precursor_snapshot(snaps, ts, k) for k in (5, 10, 20)}
        rep = pre["5"] or pre["10"] or pre["20"]   # closest available precursor

        def _trend(field):
            a = pre["20"][field] if pre["20"] else None
            late = pre["5"] or pre["10"]
            b = late[field] if late else None
            return round(b - a, 1) if (a is not None and b is not None) else None

        rows.append({
            "onset_ts": ts,
            "date": _storm_day(ts),
            "cdmx_hour": _cdmx_hour(ts),
            "in_situ": in_situ,
            "n_cells": o.get("n_cells"),
            "precursor": rep,
            "precursors": pre,
            "trend": {"d_cape": _trend("cape"), "d_humidity": _trend("humidity_pct"),
                      "press_15m": (rep.get("pressure_trend_15m") if rep else None)},
        })

    in_situ_rows = [r for r in rows if r["in_situ"]]
    had_cell_rows = [r for r in rows if not r["in_situ"]]

    def _hist(items, keyfn):
        h = {}
        for it in items:
            k = keyfn(it)
            h[k] = h.get(k, 0) + 1
        return h

    cap = lambda r: _cape_band(r["precursor"]["cape"] if r["precursor"] else None)
    rh = lambda r: _rh_band(r["precursor"]["humidity_pct"] if r["precursor"] else None)
    return {
        "ok": True,
        "total_missed": len(rows),
        "in_situ_missed": len(in_situ_rows),
        "had_cell_missed": len(had_cell_rows),
        "by_cdmx_hour": dict(sorted(_hist(in_situ_rows, lambda r: r["cdmx_hour"]).items())),
        "by_cape_band": _hist(in_situ_rows, cap),
        "by_rh_band": _hist(in_situ_rows, rh),
        "misses": rows,
        "note": ("Missed onsets (covered==False) with the logged pre-onset environment. "
                 "in_situ = no radar cell at onset (no radar warning possible); had-cell = "
                 "a cell was present but no prediction covered it (detection/prediction gap). "
                 "Precursor windows are -5/-10/-20 min by timestamp. CAPE is logged "
                 "historically; RH/dew/T/pressure-15m only from the item-13 schema change "
                 "forward (older onsets show nulls for those). Analytics only -- no model. "
                 "Consistency: in_situ_missed must equal missed_in_situ in /api/outcomes/skill."),
    }


STRATA_PAGE = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Virreyes \u00b7 Skill por dia / regimen</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:18px;max-width:760px;margin-left:auto;margin-right:auto}
h1{font-size:1.05rem;letter-spacing:.03em}
h2{font-size:.78rem;letter-spacing:.1em;text-transform:uppercase;color:#9aa3b2;margin:20px 0 6px}
.muted{color:#9aa3b2}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px;margin:10px 0}
table{width:100%;border-collapse:collapse;font-family:ui-monospace,monospace;font-size:.8rem}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid #21262d}
th:first-child,td:first-child{text-align:left}
th{color:#9aa3b2;font-weight:600;font-size:.66rem;letter-spacing:.04em}
.big{font-family:ui-monospace,monospace;font-size:1.5rem;font-weight:700}
.row{display:flex;gap:22px;flex-wrap:wrap}
.k{color:#9aa3b2;font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;display:block;margin-bottom:2px}
.good{color:#56d364}.warn{color:#f5a742}.bad{color:#ff7b72}
a{color:#58a6ff}
</style></head><body>
<h1>&#x1F4CA; Skill por dia y regimen &mdash; Virreyes</h1>
<div class="muted" style="font-size:.8rem;margin-bottom:6px">Desglose del baseline para que un solo dia generalizado no sesgue el promedio.</div>
<div class="card"><div class="row" id="overall"><span class="muted">Cargando...</span></div></div>
<h2>Por dia de tormenta (CDMX)</h2>
<div class="card"><table><thead><tr><th>Fecha</th><th>Pares</th><th>Prec%</th><th>MAE min</th><th>MAE post</th><th>Onsets</th><th>Recall%</th><th>Perdidos</th></tr></thead><tbody id="byday"></tbody></table></div>
<h2>Por regimen de flujo (steering)</h2>
<div class="card"><table><thead><tr><th>Regimen</th><th>Pares</th><th>Prec%</th><th>MAE min</th><th>MAE post</th></tr></thead><tbody id="bysteer"></tbody></table>
<div class="muted" style="font-size:.72rem;margin-top:10px" id="note"></div></div>
<p class="muted" style="font-size:.72rem"><a href="/api/outcomes/strata">JSON</a> &middot; <a href="/api/outcomes/skill">skill</a> &middot; <a href="/hotspots">hotspots</a> &middot; <a href="/">dashboard</a></p>
<script>
function pct(v){ if(v==null) return '<span class="muted">--</span>'; var c=v>=60?'good':v>=35?'warn':'bad'; return '<span class="'+c+'">'+v+'%</span>'; }
function num(v){ return v==null?'<span class="muted">--</span>':v; }
fetch('/api/outcomes/strata').then(function(r){return r.json();}).then(function(d){
  var o=d.overall||{};
  document.getElementById('overall').innerHTML=
    '<span><span class="k">Pares</span><b class="big">'+num(o.pairs)+'</b></span>'+
    '<span><span class="k">Precision</span><b class="big">'+(o.precision_pct==null?'--':o.precision_pct+'%')+'</b></span>'+
    '<span><span class="k">ETA MAE</span><b class="big">'+(o.eta_mae_min==null?'--':o.eta_mae_min)+'</b></span>'+
    '<span><span class="k">ETA MAE post-fix</span><b class="big">'+(o.eta_mae_post_min==null?'--':o.eta_mae_post_min)+'</b> <span class="k">('+num(o.pairs_post)+' pares)</span></span>'+
    '<span><span class="k">Recall</span><b class="big">'+(o.recall_pct==null?'--':o.recall_pct+'%')+'</b></span>'+
    '<span><span class="k">Perdidos in-situ</span><b class="big">'+num(o.missed_in_situ)+'</b></span>';
  document.getElementById('byday').innerHTML=(d.by_day||[]).map(function(r){
    return '<tr><td>'+r.date+'</td><td>'+r.pairs+'</td><td>'+pct(r.precision_pct)+'</td><td>'+num(r.eta_mae_min)+'</td><td>'+num(r.eta_mae_post_min)+'</td><td>'+r.onsets+'</td><td>'+pct(r.recall_pct)+'</td><td>'+r.missed+'</td></tr>';
  }).join('') || '<tr><td colspan="8" class="muted">Sin datos aun</td></tr>';
  document.getElementById('bysteer').innerHTML=(d.by_steering||[]).map(function(r){
    return '<tr><td>'+r.label+'</td><td>'+r.pairs+'</td><td>'+pct(r.precision_pct)+'</td><td>'+num(r.eta_mae_min)+'</td><td>'+num(r.eta_mae_post_min)+'</td></tr>';
  }).join('') || '<tr><td colspan="5" class="muted">Sin datos aun</td></tr>';
  document.getElementById('note').textContent=d.note||'';
}).catch(function(e){ document.getElementById('overall').textContent='Error: '+e; });
</script>
</body></html>"""


@app.route("/strata")
def strata_page():
    r = make_response(STRATA_PAGE)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    r.headers["Cache-Control"] = "no-store"
    return r


MISSES_PAGE = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Virreyes &middot; Onsets perdidos (precursor)</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:18px;max-width:820px;margin-left:auto;margin-right:auto}
h1{font-size:1.05rem;letter-spacing:.03em}
h2{font-size:.78rem;letter-spacing:.1em;text-transform:uppercase;color:#9aa3b2;margin:20px 0 6px}
.muted{color:#9aa3b2}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px;margin:10px 0}
table{width:100%;border-collapse:collapse;font-family:ui-monospace,monospace;font-size:.8rem}
th,td{text-align:right;padding:6px 8px;border-bottom:1px solid #21262d}
th:first-child,td:first-child{text-align:left}
th{color:#9aa3b2;font-weight:600;font-size:.66rem;letter-spacing:.04em}
.big{font-family:ui-monospace,monospace;font-size:1.5rem;font-weight:700}
.row{display:flex;gap:22px;flex-wrap:wrap}
.k{color:#9aa3b2;font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;display:block;margin-bottom:2px}
.good{color:#56d364}.warn{color:#f5a742}.bad{color:#ff7b72}
a{color:#58a6ff}
</style></head><body>
<h1>&#x1F50D; Onsets perdidos &mdash; entorno precursor</h1>
<div class="muted" style="font-size:.8rem;margin-bottom:6px">Cada onset de lluvia no anticipado, con el entorno que el autologger registro justo antes. Solo analisis &mdash; sin modelo.</div>
<div class="card"><div class="row" id="overall"><span class="muted">Cargando...</span></div></div>
<h2>In-situ por hora (CDMX)</h2>
<div class="card"><table><thead><tr><th>Hora</th><th>Onsets in-situ perdidos</th></tr></thead><tbody id="byhour"></tbody></table></div>
<h2>In-situ por banda (CAPE / HR)</h2>
<div class="card"><table><thead><tr><th>Banda</th><th>Onsets in-situ</th></tr></thead><tbody id="byband"></tbody></table></div>
<h2>Detalle (mas reciente primero)</h2>
<div class="card"><table><thead><tr><th>Fecha/hora</th><th>In-situ</th><th>Celdas</th><th>CAPE</th><th>HR%</th><th>T-Td</th><th>P 15m</th><th>dt min</th></tr></thead><tbody id="rows"></tbody></table></div>
<div class="muted" style="font-size:.72rem;margin-top:10px" id="note"></div>
<p class="muted" style="font-size:.72rem"><a href="/api/outcomes/misses">JSON</a> &middot; <a href="/strata">strata</a> &middot; <a href="/hotspots">hotspots</a> &middot; <a href="/">dashboard</a></p>
<script>
function num(v){ return (v==null)?'<span class="muted">--</span>':v; }
function fdt(ts){ var d=new Date(ts*1000); return d.toISOString().slice(5,16).replace('T',' '); }
fetch('/api/outcomes/misses').then(function(r){return r.json();}).then(function(d){
  document.getElementById('overall').innerHTML=
    '<span><span class="k">Onsets perdidos</span><b class="big">'+num(d.total_missed)+'</b></span>'+
    '<span><span class="k">In-situ (sin celda)</span><b class="big">'+num(d.in_situ_missed)+'</b></span>'+
    '<span><span class="k">Con celda (gap)</span><b class="big">'+num(d.had_cell_missed)+'</b></span>';
  var bh=d.by_cdmx_hour||{};
  document.getElementById('byhour').innerHTML=Object.keys(bh).map(function(h){
    return '<tr><td>'+h+':00</td><td>'+bh[h]+'</td></tr>'; }).join('')
    || '<tr><td colspan="2" class="muted">Sin datos aun</td></tr>';
  var bc=d.by_cape_band||{}, br=d.by_rh_band||{};
  var bandHtml=Object.keys(bc).map(function(k){return '<tr><td>CAPE '+k+'</td><td>'+bc[k]+'</td></tr>';}).join('')
    + Object.keys(br).map(function(k){return '<tr><td>HR '+k+'</td><td>'+br[k]+'</td></tr>';}).join('');
  document.getElementById('byband').innerHTML=bandHtml || '<tr><td colspan="2" class="muted">Sin datos aun</td></tr>';
  document.getElementById('rows').innerHTML=(d.misses||[]).map(function(r){
    var p=r.precursor||{};
    return '<tr><td>'+fdt(r.onset_ts)+'</td><td>'+(r.in_situ?'si':'no')+'</td><td>'+num(r.n_cells)+'</td><td>'+num(p.cape)+'</td><td>'+num(p.humidity_pct)+'</td><td>'+num(p.t_td_spread)+'</td><td>'+num(p.pressure_trend_15m)+'</td><td>'+(p.dt_min==null?'<span class="muted">--</span>':p.dt_min)+'</td></tr>';
  }).join('') || '<tr><td colspan="8" class="muted">Sin onsets perdidos</td></tr>';
  document.getElementById('note').textContent=d.note||'';
}).catch(function(e){ document.getElementById('overall').textContent='Error: '+e; });
</script>
</body></html>"""


@app.route("/api/outcomes/misses")
def outcomes_misses():
    try:
        return jsonify(_misses_summary())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/misses")
def misses_page():
    r = make_response(MISSES_PAGE)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/api/outcomes/skill")
def outcomes_skill():
    """Verification summary: how well the current heuristic ETA is doing.
    This is both a progress meter and the baseline a learned model must beat."""
    try:
        rows = []
        if os.path.exists(OUTCOME_FILE):
            with open(OUTCOME_FILE) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
        n = len(rows)
        hits = [r for r in rows if r.get("outcome") == "hit"]
        misses = [r for r in rows if r.get("outcome") == "miss"]
        mae = _mae_over(hits)
        # item 9: post-fix-only skill -- pairs carrying frame_age_min were logged
        # with the radar-latency correction. Read this during the transition; the
        # blended mae mixes old frame-anchored pairs and reads artificially high.
        post = [r for r in rows if "frame_age_min" in r]
        mae_post = _mae_over([r for r in post if r.get("outcome") == "hit"])
        pairs_post = len(post)
        # precision: of all predictions, how many actually produced rain
        precision = round(100 * len(hits) / n) if n else None
        # event-level recall (analytical, from the event-log rain series)
        rec = _recall_summary()
        return jsonify({
            "ok": True,
            "labeled_pairs": n,
            "hits": len(hits),
            "misses": len(misses),
            "precision_pct": precision,
            "eta_mae_min": mae,
            "labeled_pairs_post": pairs_post,
            "eta_mae_post_min": mae_post,
            "rain_onsets": rec["rain_onsets"],
            "onsets_covered": rec["onsets_covered"],
            "onsets_missed": rec["onsets_missed"],
            "recall_pct": rec["recall_pct"],
            "missed_in_situ": rec["missed_in_situ"],
            "note": "precision = of issued predictions, fraction that produced rain "
                    "(only scores trackable events). recall = of all rain onsets at "
                    "Virreyes, fraction a prediction covered within 120 min. "
                    "missed_in_situ = missed onsets with no radar cell present (the "
                    "'just appeared' blind spot). eta_mae_min = blended ETA error "
                    "(mixes pre-/post-latency-fix labels); eta_mae_post_min = MAE over "
                    "post-fix pairs only (item 9) -- read this during the transition.",
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/log/export")
def log_export():
    """Download the raw event log (JSONL) for offline training/analysis."""
    try:
        with open(EVENTLOG_FILE) as fh:
            data = fh.read()
    except FileNotFoundError:
        data = ""
    r = make_response(data)
    r.headers["Content-Type"] = "application/x-ndjson"
    r.headers["Content-Disposition"] = 'attachment; filename="storm_events.jsonl"'
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/api/log/stats")
def log_stats():
    """Quick summary: record count, first/last timestamps, size."""
    try:
        n = 0; first = None; last = None; srv = 0; brw = 0; withcells = 0
        with open(EVENTLOG_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                n += 1
                try:
                    obj = json.loads(line)
                    ts = obj.get("server_ts") or obj.get("t")
                    if ts:
                        first = ts if first is None else min(first, ts)
                        last = ts if last is None else max(last, ts)
                    if obj.get("src") == "server":
                        srv += 1
                    else:
                        brw += 1
                    nc = obj.get("n_cells")
                    if nc is None:
                        nc = len(obj.get("cells") or [])
                    if nc and nc > 0:
                        withcells += 1
                except Exception:
                    pass
        import os as _os
        size = _os.path.getsize(EVENTLOG_FILE) if _os.path.exists(EVENTLOG_FILE) else 0
        return jsonify({"ok": True, "records": n, "first_ts": first, "last_ts": last,
                        "bytes": size, "server_records": srv, "browser_records": brw,
                        "events_with_cells": withcells})
    except FileNotFoundError:
        return jsonify({"ok": True, "records": 0, "first_ts": None, "last_ts": None, "bytes": 0})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/health")
@app.route("/health")
def health():
    # storage diagnostics: is the persistent volume actually in use?
    persistent = DATA_DIR != "/tmp"
    log_exists = os.path.exists(EVENTLOG_FILE)
    log_bytes = os.path.getsize(EVENTLOG_FILE) if log_exists else 0
    # is /data present as a mount at all?
    data_mount_present = os.path.isdir("/data")
    writable = os.access(DATA_DIR, os.W_OK) if os.path.isdir(DATA_DIR) else False
    return jsonify(
        {
            "ok": True,
            "station_id": WL_STATION_ID,
            "has_api_key": bool(WL_API_KEY),
            "has_api_secret": bool(WL_API_SECRET),
            "storage": {
                "data_dir": DATA_DIR,
                "persistent": persistent,
                "data_mount_present": data_mount_present,
                "writable": writable,
                "log_file": EVENTLOG_FILE,
                "log_exists": log_exists,
                "log_bytes": log_bytes,
            },
            "autologger": dict(_AUTOLOG_STATUS, my_pid=os.getpid()),
            "dem": {"ready": _DEM is not None, "state": _DEM_STATUS.get("state"),
                    "last_error": _DEM_STATUS.get("last_error")},
            "rain_trace": {"points": len(_RAIN_TRACE),
                           "persisted": os.path.exists(RAIN_TRACE_FILE),
                           "newest_ts": _RAIN_TRACE[-1][0] if _RAIN_TRACE else None},
        }
    )


@app.route("/manifest.json")
def manifest():
    return send_from_directory(".", "manifest.json", mimetype="application/manifest+json")


@app.route("/sw.js")
def sw():
    return send_from_directory(".", "sw.js", mimetype="application/javascript")


@app.route("/icon-<int:size>.png")
def icon(size):
    return send_from_directory(".", f"icon-{size}.png", mimetype="image/png")


# ─────────────────────────────────────────────────────────────────────────
# SERVER-SIDE AUTO-LOGGER
# Runs in a background thread on the always-on Railway service so storm
# features are recorded 24/7 without any browser open. Replicates the core
# of the client analysis: RainViewer cell detection, 700 hPa steering,
# terrain elevation, Davis ground-truth. Lightning stays client-side.
# ─────────────────────────────────────────────────────────────────────────
import math
import threading

LAT = 19.41997
LON = -99.21059
KM_LAT = 111.0
KM_LON = 111.0 * math.cos(math.radians(LAT))

# analysis viewport (km half-width/height) and sampling resolution
AW_KM = 32.0
AH_KM = 30.0

TERRAIN_ANCHORS = [
    (19.4326, -99.1332, 2240), (19.40, -99.05, 2240), (19.36, -99.28, 3050),
    (19.30, -99.30, 3400), (19.25, -99.20, 3700), (19.60, -99.13, 2600),
    (19.55, -99.30, 2900), (19.10, -98.95, 3500), (19.43, -98.75, 4200),
    (19.50, -98.90, 2500),
]


def _anchor_elev(lat, lon):
    """Coarse fallback elevation from hand-placed anchors (used only until the
    real DEM grid has been fetched and cached)."""
    num = den = 0.0
    for a_lat, a_lon, e in TERRAIN_ANCHORS:
        dx = (lon - a_lon) * KM_LON
        dy = (lat - a_lat) * KM_LAT
        d2 = dx * dx + dy * dy + 4.0
        w = 1.0 / (d2 * d2)
        num += w * e
        den += w
    return num / den


# ── Real DEM (Copernicus GLO-90 via Open-Meteo elevation API) ────────────────
# A regular lat/lon grid over the Valley of Mexico, fetched ONCE and cached to
# the persistent volume. Bilinear-interpolated at query time. Falls back to the
# anchor model until the grid is loaded.
DEM_LAT0, DEM_LAT1 = 18.97, 19.87      # south, north  (0.90 deg span)
DEM_LON0, DEM_LON1 = -99.66, -98.76    # west, east    (0.90 deg span)
DEM_N = 37                              # points per axis -> 1369 total
DEM_VERSION = 1
_DEM = None                             # {lat0,lon0,dlat,dlon,nlat,nlon,elev:[...]}
_DEM_PATH = os.path.join(DATA_DIR, "dem.json")
_DEM_PARTIAL_PATH = os.path.join(DATA_DIR, "dem_partial.json")


def _dem_load_from_disk():
    global _DEM
    try:
        with open(_DEM_PATH) as fh:
            d = json.load(fh)
        if (d.get("version") == DEM_VERSION and d.get("nlat") == DEM_N
                and d.get("nlon") == DEM_N and len(d.get("elev", [])) == DEM_N * DEM_N
                and abs(d.get("lat0", 0) - DEM_LAT0) < 1e-6):
            _DEM = d
            return True
    except Exception:
        pass
    return False


_DEM_PARTIAL = []          # persisted partial progress across retries


def _dem_fetch():
    """Fetch the elevation grid from Open-Meteo, RESUMABLE across calls. Saves
    partial progress so a mid-way 429 just pauses; the next attempt continues
    from where it stopped instead of restarting (and re-tripping the limit)."""
    global _DEM, _DEM_PARTIAL
    dlat = (DEM_LAT1 - DEM_LAT0) / (DEM_N - 1)
    dlon = (DEM_LON1 - DEM_LON0) / (DEM_N - 1)
    coords = []
    for i in range(DEM_N):
        for j in range(DEM_N):
            coords.append((DEM_LAT0 + i * dlat, DEM_LON0 + j * dlon))
    total = len(coords)

    # resume from any saved partial progress
    elev = list(_DEM_PARTIAL)
    if _DEM_PARTIAL_PATH and not elev and os.path.exists(_DEM_PARTIAL_PATH):
        try:
            with open(_DEM_PARTIAL_PATH) as fh:
                elev = json.load(fh)
        except Exception:
            elev = []

    BATCH = 50                  # fewer total requests = friendlier to the limit
    start = (len(elev) // BATCH) * BATCH    # align to batch boundary
    elev = elev[:start]                     # trim any partial tail

    for k in range(start, total, BATCH):
        batch = coords[k:k + BATCH]
        lats = ",".join("%.5f" % c[0] for c in batch)
        lons = ",".join("%.5f" % c[1] for c in batch)
        url = ("https://api.open-meteo.com/v1/elevation?latitude=%s&longitude=%s"
               % (lats, lons))
        try:
            status, data = fetch_json(url, timeout=30)
        except Exception as exc:
            _DEM_STATUS["last_error"] = "batch %d: %s" % (k // BATCH, exc)
            _dem_save_partial(elev)
            return False
        if status == 429:
            _DEM_STATUS["last_error"] = ("rate-limited at %d/%d points; will resume"
                                         % (len(elev), total))
            _dem_save_partial(elev)        # keep what we have; resume next attempt
            return False
        if status != 200:
            reason = data.get("reason", "") if isinstance(data, dict) else ""
            _DEM_STATUS["last_error"] = "batch %d HTTP %s %s" % (k // BATCH, status, reason)
            _dem_save_partial(elev)
            return False
        if not isinstance(data, dict) or "elevation" not in data:
            _DEM_STATUS["last_error"] = "batch %d: no elevation field" % (k // BATCH)
            _dem_save_partial(elev)
            return False
        vals = data["elevation"]
        if len(vals) != len(batch):
            _DEM_STATUS["last_error"] = "batch %d: got %d of %d" % (k // BATCH, len(vals), len(batch))
            _dem_save_partial(elev)
            return False
        elev.extend(float(v) if v is not None else None for v in vals)
        _DEM_STATUS["last_error"] = "progress %d/%d points" % (len(elev), total)
        time.sleep(1.2)             # ~50 pts/1.2s -> well under per-minute limits

    if len(elev) != total or any(v is None for v in elev):
        nulls = sum(1 for v in elev if v is None)
        _DEM_STATUS["last_error"] = "grid incomplete (%d pts, %d nulls)" % (len(elev), nulls)
        _dem_save_partial(elev)
        return False
    grid = {"version": DEM_VERSION, "lat0": DEM_LAT0, "lon0": DEM_LON0,
            "dlat": dlat, "dlon": dlon, "nlat": DEM_N, "nlon": DEM_N,
            "elev": [round(v, 1) for v in elev]}
    try:
        tmp = _DEM_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(grid, fh, separators=(",", ":"))
        os.replace(tmp, _DEM_PATH)
        if _DEM_PARTIAL_PATH and os.path.exists(_DEM_PARTIAL_PATH):
            os.remove(_DEM_PARTIAL_PATH)    # done; clear partial
    except Exception:
        pass
    _DEM_PARTIAL = []
    _DEM = grid
    return True


def _dem_save_partial(elev):
    global _DEM_PARTIAL
    _DEM_PARTIAL = list(elev)
    if _DEM_PARTIAL_PATH:
        try:
            tmp = _DEM_PARTIAL_PATH + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(elev, fh, separators=(",", ":"))
            os.replace(tmp, _DEM_PARTIAL_PATH)
        except Exception:
            pass


_DEM_STATUS = {"state": "init", "attempts": 0, "last_error": None, "last_try": 0}


def _dem_start():
    """Background: load the cached DEM, or fetch it. Keeps retrying with backoff
    rather than giving up, since the elevation API can be transiently rate-limited."""
    def run():
        if _dem_load_from_disk():
            _DEM_STATUS["state"] = "loaded-from-disk"
            return
        delay = 60                    # start at 60s so the per-minute limit clears
        while _DEM is None:
            _DEM_STATUS["attempts"] += 1
            _DEM_STATUS["last_try"] = int(time.time())
            try:
                if _dem_fetch():
                    _DEM_STATUS["state"] = "fetched"
                    _DEM_STATUS["last_error"] = None
                    return
                _DEM_STATUS["state"] = "retrying"
                # NB: _dem_fetch() already set a specific last_error; keep it.
                if not _DEM_STATUS.get("last_error"):
                    _DEM_STATUS["last_error"] = "fetch failed (no detail)"
            except Exception as exc:
                _DEM_STATUS["state"] = "retrying"
                _DEM_STATUS["last_error"] = str(exc)
            time.sleep(delay)
            delay = min(delay * 2, 600)   # backoff up to 10 min, then steady
    threading.Thread(target=run, daemon=True).start()


def _dem_elev(lat, lon):
    """Bilinear interpolation over the cached DEM grid. None if not loaded."""
    d = _DEM
    if not d:
        return None
    fi = (lat - d["lat0"]) / d["dlat"]
    fj = (lon - d["lon0"]) / d["dlon"]
    nlat, nlon = d["nlat"], d["nlon"]
    if fi < 0: fi = 0.0
    if fj < 0: fj = 0.0
    if fi > nlat - 1: fi = nlat - 1
    if fj > nlon - 1: fj = nlon - 1
    i0 = int(fi); j0 = int(fj)
    i1 = min(i0 + 1, nlat - 1); j1 = min(j0 + 1, nlon - 1)
    ti = fi - i0; tj = fj - j0
    e = d["elev"]
    e00 = e[i0 * nlon + j0]; e01 = e[i0 * nlon + j1]
    e10 = e[i1 * nlon + j0]; e11 = e[i1 * nlon + j1]
    return (e00 * (1 - ti) * (1 - tj) + e01 * (1 - ti) * tj
            + e10 * ti * (1 - tj) + e11 * ti * tj)


def terrain_elev(lat, lon):
    """Real DEM when available, else the anchor fallback."""
    v = _dem_elev(lat, lon)
    return v if v is not None else _anchor_elev(lat, lon)


_dem_start()


def _rv_palette_mm(r, g, b, a):
    if a < 40:
        return 0.0
    if r > 170 and g < 130 and b > 150:
        return 14.0
    if r > 190 and g < 130 and b < 130:
        return 9.0
    if r > 200 and 130 < g < 200 and b < 120:
        return 5.0
    if r > 195 and g > 195 and b < 150:
        return 3.0
    if b > 150 and r < 150:
        return 0.5 if g > 175 else 1.5
    if r > 210 and g > 210 and b > 210:
        return 0.3
    return 0.8


def _lon2tx(lon, z):
    return (lon + 180.0) / 360.0 * (2 ** z)


def _lat2ty(lat, z):
    r = math.radians(lat)
    return (1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * (2 ** z)


def _fetch_rv_meta():
    req = urllib.request.Request(
        "https://api.rainviewer.com/public/weather-maps.json",
        headers={"User-Agent": "virreyes-weather/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _fetch_rv_field(host, fpath):
    """Composite RainViewer z=7 tiles over the viewport into an mm/h grid.
    Returns (grid, gw, gh, px_per_km) or None. Requires Pillow."""
    try:
        from PIL import Image
    except Exception:
        return None
    import io

    Z = 7
    px_per_km = 7.2
    AW = int(AW_KM * 2 * px_per_km)   # ~460
    AH = int(AH_KM * 2 * px_per_km)   # ~432
    canvas = Image.new("RGBA", (AW, AH), (0, 0, 0, 0))

    min_lon = LON - AW_KM / KM_LON
    max_lon = LON + AW_KM / KM_LON
    min_lat = LAT - AH_KM / KM_LAT
    max_lat = LAT + AH_KM / KM_LAT
    tx0, tx1 = int(_lon2tx(min_lon, Z)), int(_lon2tx(max_lon, Z))
    ty0, ty1 = int(_lat2ty(max_lat, Z)), int(_lat2ty(min_lat, Z))

    def to_px_x(lon):
        return AW / 2 + (lon - LON) * KM_LON * px_per_km

    def to_px_y(lat):
        return AH / 2 - (lat - LAT) * KM_LAT * px_per_km

    def tx2lon(tx):
        return tx / (2 ** Z) * 360.0 - 180.0

    def ty2lat(ty):
        n = math.pi - 2 * math.pi * ty / (2 ** Z)
        return math.degrees(math.atan(0.5 * (math.exp(n) - math.exp(-n))))

    got = False
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            data = None
            for size in (512, 256):
                url = f"{host}{fpath}/{size}/{Z}/{tx}/{ty}/2/1_1.png"
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "virreyes-weather/1.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = resp.read()
                    break
                except Exception:
                    continue
            if not data:
                continue
            try:
                tile = Image.open(io.BytesIO(data)).convert("RGBA")
            except Exception:
                continue
            xa, ya = to_px_x(tx2lon(tx)), to_px_y(ty2lat(ty))
            xb, yb = to_px_x(tx2lon(tx + 1)), to_px_y(ty2lat(ty + 1))
            w, h = max(1, int(round(xb - xa))), max(1, int(round(yb - ya)))
            try:
                tile = tile.resize((w, h))
                canvas.paste(tile, (int(round(xa)), int(round(ya))), tile)
                got = True
            except Exception:
                continue
    if not got:
        return None

    px = canvas.load()
    return (px, AW, AH, px_per_km)


# cell-detection floor expressed as a dBZ threshold -- single source of truth,
# mirrors the client's CELL_DBZ_MIN. 30 dBZ ~= 2.73 mm/h via the Z-R relation,
# so the ">=30 dBZ" labels in the UI are now literally true.
CELL_DBZ_MIN = 30


def _dbz_to_mm(dbz):
    return (10 ** (dbz / 10.0) / 200.0) ** (1.0 / 1.6)


CELL_MM_THR = _dbz_to_mm(CELL_DBZ_MIN)


def _detect_cells(field):
    """Connected-component cells over the mm/h field. field=(px,AW,AH,ppk)."""
    px, AW, AH, ppk = field
    S = 3
    GW, GH = AW // S, AH // S
    g = [0.0] * (GW * GH)
    for j in range(GH):
        for i in range(GW):
            r, gg, b, a = px[i * S + 1, j * S + 1]
            g[j * GW + i] = _rv_palette_mm(r, gg, b, a)
    seen = bytearray(GW * GH)
    THR = CELL_MM_THR
    cells = []
    for j in range(GH):
        for i in range(GW):
            idx = j * GW + i
            if seen[idx] or g[idx] < THR:
                continue
            stack = [idx]
            seen[idx] = 1
            n = 0
            sx = sy = 0
            mx = 0.0
            while stack:
                k = stack.pop()
                ki, kj = k % GW, k // GW
                n += 1
                sx += ki
                sy += kj
                if g[k] > mx:
                    mx = g[k]
                for nk in (k + 1, k - 1, k + GW, k - GW):
                    if 0 <= nk < GW * GH and not seen[nk] and g[nk] >= THR:
                        # guard horizontal wrap
                        if nk in (k + 1, k - 1) and (nk // GW) != kj:
                            continue
                        seen[nk] = 1
                        stack.append(nk)
            area_km = n * (S / ppk) ** 2
            if area_km < 5:
                continue
            cpx, cpy = sx / n * S, sy / n * S
            x_km = (cpx - AW / 2) / ppk
            y_km = (AH / 2 - cpy) / ppk
            cells.append({
                "x": round(x_km, 1), "y": round(y_km, 1),
                "mm": round(mx, 1),
                "dbz": round(10 * math.log10(200 * (max(mx, 0.05) ** 1.6))),
                "dist": round(math.hypot(x_km, y_km), 1),
                "brg": round((math.degrees(math.atan2(x_km, y_km)) + 360) % 360),
                "elev": round(terrain_elev(LAT + y_km / KM_LAT, LON + x_km / KM_LON)),
            })
    cells.sort(key=lambda c: -c["mm"])
    return cells[:8]


_STEER_CACHE = {"t": 0, "val": (None, None, None, None)}

def _fetch_steering():
    # cache for 20 min: the pressure-level/CAPE fields are hourly model output, so
    # re-fetching every logger cycle just burns the Open-Meteo rate limit.
    if time.time() - _STEER_CACHE["t"] < 20 * 60 and _STEER_CACHE["val"][0] is not None:
        return _STEER_CACHE["val"]
    try:
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
               "&hourly=wind_speed_850hPa,wind_direction_850hPa,"
               "wind_speed_700hPa,wind_direction_700hPa,"
               "wind_speed_500hPa,wind_direction_500hPa,cape,lifted_index"
               "&forecast_hours=1&timezone=America%%2FMexico_City" % (LAT, LON))
        _, data = fetch_json(url)
        h = data.get("hourly", {})
        wd = (h.get("wind_direction_700hPa") or [None])[0]
        ws = (h.get("wind_speed_700hPa") or [None])[0]
        cape = (h.get("cape") or [None])[0]
        li = (h.get("lifted_index") or [None])[0]
        steer = None
        if wd is not None and ws is not None and ws >= 4:
            toward = (wd + 180) % 360
            steer = {"dir": round(toward), "spd": round(ws * 0.85), "src": "700hPa"}
        # item 1: log the full low/mid/upper steering profile (raw model winds,
        # km/h, dir = FROM). Diagnostic/training data only -- 'steer' above is still
        # the single 700 hPa input used for ETA, so train/serve parity is unchanged.
        profile = {}
        for lv in ("850", "700", "500"):
            d = (h.get("wind_direction_%shPa" % lv) or [None])[0]
            sp = (h.get("wind_speed_%shPa" % lv) or [None])[0]
            if d is not None and sp is not None:
                profile[lv] = {"spd": round(sp), "dir": round(d)}
        profile = profile or None
        _STEER_CACHE["t"] = time.time()
        _STEER_CACHE["val"] = (steer, cape, li, profile)
        return steer, cape, li, profile
    except Exception:
        # serve last good value on error/rate-limit rather than going blind
        if _STEER_CACHE["val"][0] is not None:
            return _STEER_CACHE["val"]
        return None, None, None, None


PENDING_FILE = os.path.join(DATA_DIR, "pending_pred.jsonl")
OUTCOME_FILE = os.path.join(DATA_DIR, "labeled_outcomes.jsonl")
RAIN_TRACE_FILE = os.path.join(DATA_DIR, "rain_trace.json")


def _load_rain_trace():
    """Restore the rain trace from disk on startup. Without this, a container
    restart or deploy blanks the in-memory trace, so any rain onset recorded
    before the restart is lost and pending predictions that should resolve as
    hits wrongly resolve as misses -- quietly depressing the precision baseline."""
    try:
        with open(RAIN_TRACE_FILE) as fh:
            data = json.load(fh)
        cutoff = int(time.time()) - 3 * 3600
        return [(int(ts), float(rr)) for ts, rr in data if int(ts) >= cutoff]
    except Exception:
        return []


def _save_rain_trace():
    """Persist the rain trace atomically so it survives restarts/deploys."""
    try:
        tmp = RAIN_TRACE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(_RAIN_TRACE, fh, separators=(",", ":"))
        os.replace(tmp, RAIN_TRACE_FILE)
    except Exception:
        pass


_RAIN_TRACE = _load_rain_trace()   # (ts, rain_rate) ring buffer for outcome detection

# ── Explicit first-echo logging (sharpens the initiation-hotspot map) ────────
# Each logger cycle, match detected cells against the previous cycle's cells; any
# cell with no predecessor is an authoritative "birth" (first echo). Records cell
# births live instead of reconstructing them post-hoc. We only log births we
# actually witness: on a cold start (no previous frame) we seed and skip, so we
# never invent a birth for a cell that predates our observation.
FIRST_ECHO_FILE = os.path.join(DATA_DIR, "first_echo.jsonl")
_PREV_CELLS = []
_PREV_CELLS_T = 0


def _new_cells(cells, prev_cells, dt_sec):
    """Cells with no predecessor in prev_cells within HS_MATCH_KM (greedy nearest
    match). dt_sec gates linkage to within an episode. Returns [(cell, kind)]."""
    linkable = prev_cells if (prev_cells and dt_sec is not None and dt_sec <= HS_GAP) else []
    pairs = []
    for ci, c in enumerate(cells):
        for pi, p in enumerate(linkable):
            pairs.append((math.hypot(c["x"] - p["x"], c["y"] - p["y"]), ci, pi))
    pairs.sort()
    matched, used_p = set(), set()
    for d, ci, pi in pairs:
        if d > HS_MATCH_KM:
            break
        if ci in matched or pi in used_p:
            continue
        matched.add(ci)
        used_p.add(pi)
    out = []
    for ci, c in enumerate(cells):
        if ci in matched:
            continue
        x, y = c["x"], c["y"]
        edge = (abs(x) > AW_KM - HS_EDGE_KM) or (abs(y) > AH_KM - HS_EDGE_KM)
        out.append((c, "edge" if edge else "interior"))
    return out


def _terrain_grad(xkm, ykm):
    h = 1.5
    e_x1 = terrain_elev(LAT + ykm / KM_LAT, LON + (xkm + h) / KM_LON)
    e_x0 = terrain_elev(LAT + ykm / KM_LAT, LON + (xkm - h) / KM_LON)
    e_y1 = terrain_elev(LAT + (ykm + h) / KM_LAT, LON + xkm / KM_LON)
    e_y0 = terrain_elev(LAT + (ykm - h) / KM_LAT, LON + xkm / KM_LON)
    return (e_x1 - e_x0) / (2 * h), (e_y1 - e_y0) / (2 * h)


def _terrain_correct(xkm, ykm, vx, vy):
    # Conservative — mirrors the client. Only deflect on a strong gradient with
    # the storm driving decisively into a barrier; otherwise keep the straight
    # steering track. Keeps logged ETA consistent with what the map shows.
    speed = math.hypot(vx, vy)
    if speed < 3:
        return vx, vy
    gx, gy = _terrain_grad(xkm, ykm)
    gmag = math.hypot(gx, gy)
    if gmag < 12:
        return vx, vy
    ux, uy = vx / speed, vy / speed
    nx, ny = gx / gmag, gy / gmag
    uphill = ux * nx + uy * ny
    if uphill <= 0.4:
        return vx, vy
    steep = min(1.0, (gmag - 12) / 20.0)
    damp = 0.35 * steep * uphill
    cvx = vx - damp * nx * speed
    cvy = vy - damp * ny * speed
    tx, ty = -ny, nx
    sense = 1 if (ux * tx + uy * ty) >= 0 else -1
    deflect = 0.20 * steep * uphill
    cvx += sense * deflect * tx * speed
    cvy += sense * deflect * ty * speed
    cs = math.hypot(cvx, cvy)
    if cs > 0:
        clamped = max(0.7 * speed, min(1.15 * speed, cs))
        cvx, cvy = cvx / cs * clamped, cvy / cs * clamped
        # cap the turn at 30 deg, matching the client so the logged ETA path
        # never curves more sharply than the track the map shows (train/serve parity)
        cos_t = (vx * cvx + vy * cvy) / (speed * clamped)
        if cos_t < math.cos(math.radians(30)):
            ang0 = math.atan2(vy, vx)
            angc = math.atan2(cvy, cvx)
            d_a = angc - ang0
            while d_a > math.pi:
                d_a -= 2 * math.pi
            while d_a < -math.pi:
                d_a += 2 * math.pi
            lim_ang = ang0 + (1 if d_a >= 0 else -1) * math.radians(30)
            cvx, cvy = math.cos(lim_ang) * clamped, math.sin(lim_ang) * clamped
    return cvx, cvy


def _predict_eta(cells, steer):
    """Heuristic ETA for the nearest approaching cell, terrain-curved.
    Returns (eta_min, cell) or (None, None)."""
    if not steer:
        return None, None
    spd = steer["spd"]
    if spd < 3:
        return None, None
    toward = math.radians(steer["dir"])
    vx0 = spd * math.sin(toward)
    vy0 = spd * math.cos(toward)
    best = None
    for c in cells:
        x, y = c["x"], c["y"]
        cvx, cvy = vx0, vy0
        # step the curved path 6-min increments to 60 min
        bx, by = x, y
        best_miss, best_t = 1e9, None
        t = 0
        px, py = x, y
        while t < 60:
            cvx, cvy = _terrain_correct(px, py, cvx, cvy)
            px += cvx * 0.1
            py += cvy * 0.1
            t += 6
            d = math.hypot(px, py)
            if d < best_miss:
                best_miss, best_t = d, t
        if best_miss < 10 and best_t:
            start = math.hypot(x, y)
            if best_miss < start - 1:
                if best is None or best_t < best["eta"]:
                    best = {"eta": best_t, "cell": c}
    if best:
        return best["eta"], best["cell"]
    return None, None


def _resolve_outcomes():
    """Match pending predictions against the Davis rain trace. A prediction is
    resolved when (a) rain starts at Virreyes (rate >= 0.2 mm/h) -> hit with
    actual lead time, or (b) 120 min elapse with no rain -> miss."""
    try:
        if not os.path.exists(PENDING_FILE):
            return
        with open(PENDING_FILE) as fh:
            pend = [json.loads(l) for l in fh if l.strip()]
    except Exception:
        return
    if not pend:
        return
    now = int(time.time())
    keep, resolved = [], []
    for p in pend:
        pt = p["t"]
        # did rain start after the prediction time?
        onset = None
        for ts, rr in _RAIN_TRACE:
            if ts >= pt and rr >= 0.2:
                onset = ts
                break
        if onset is not None:
            resolved.append({**p, "outcome": "hit",
                             "actual_min": round((onset - pt) / 60),
                             "resolved_t": now})
        elif now - pt >= 120 * 60:
            resolved.append({**p, "outcome": "miss",
                             "actual_min": None, "resolved_t": now})
        else:
            keep.append(p)
    if resolved:
        try:
            with open(OUTCOME_FILE, "a") as fh:
                for r in resolved:
                    fh.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n")
        except Exception:
            pass
    try:
        with open(PENDING_FILE, "w") as fh:
            for p in keep:
                fh.write(json.dumps(p, separators=(",", ":"), ensure_ascii=False) + "\n")
    except Exception:
        pass


def _auto_log_once():
    try:
        # Davis ground-truth
        status, raw = weatherlink_current_raw()
        davis = normalize_weatherlink(raw) if status == 200 else {}
        rain_rate = davis.get("rain_rate_mm_h") or 0
        rain_day = davis.get("rain_day_mm") or 0
        # item 13: keep PRESS_HIST fresh 24/7 (not only when /current is hit) and
        # compute the pressure tendencies here so every autolog snapshot carries
        # the full precursor set for missed-onset analysis, not just CAPE.
        _press_hpa = davis.get("pressure_hpa")
        _pt3 = record_pressure(_press_hpa)
        _pt15 = pressure_trend_15m(_press_hpa)
        _lt = _fetch_ecowitt_lightning() or {}   # item 4: WH57 lightning precursor

        cells = []
        rv_time = None
        try:
            meta = _fetch_rv_meta()
            host = meta.get("host", "https://tilecache.rainviewer.com")
            past = (meta.get("radar") or {}).get("past") or []
            if past:
                fr = past[-1]
                rv_time = fr["time"]
                field = _fetch_rv_field(host, fr["path"])
                if field:
                    cells = _detect_cells(field)
        except Exception:
            pass

        steer, cape, li, steer_profile = _fetch_steering()
        now_ts = int(time.time())

        # explicit first-echo logging: record authoritative cell births by matching
        # against the previous cycle (skips the cold-start cycle so no false births)
        global _PREV_CELLS, _PREV_CELLS_T
        if _PREV_CELLS_T:
            dt = now_ts - _PREV_CELLS_T
            for c, kind in _new_cells(cells, _PREV_CELLS, dt):
                phase = "start" if dt > HS_GAP else "mid"
                try:
                    with open(FIRST_ECHO_FILE, "a") as fh:
                        fh.write(json.dumps({
                            "t": now_ts,
                            "x": c.get("x"), "y": c.get("y"),
                            "lat": round(LAT + (c.get("y") or 0) / KM_LAT, 5),
                            "lon": round(LON + (c.get("x") or 0) / KM_LON, 5),
                            "dbz": c.get("dbz"), "mm": c.get("mm"),
                            "kind": kind, "phase": phase, "n_cells": len(cells),
                        }, separators=(",", ":"), ensure_ascii=False) + "\n")
                except Exception:
                    pass
        _PREV_CELLS = cells
        _PREV_CELLS_T = now_ts

        # maintain a rain trace (last 3h) for outcome resolution
        _RAIN_TRACE.append((now_ts, rain_rate or 0))
        cutoff = now_ts - 3 * 3600
        while _RAIN_TRACE and _RAIN_TRACE[0][0] < cutoff:
            _RAIN_TRACE.pop(0)
        _save_rain_trace()          # persist so a restart/deploy can't blank the trace

        # terrain-aware ETA prediction for the nearest approaching cell
        eta_min, eta_cell = _predict_eta(cells, steer)

        # item 9: radar-latency correction. _predict_eta anchors the ETA to the
        # radar FRAME time (rv_time), but this prediction is made at now_ts and is
        # resolved against rain onset measured from now_ts (_resolve_outcomes:
        # actual_min = (onset - t)/60). The raw ETA therefore runs systematically
        # LATE by the frame age, biasing the ETA-MAE metric. Subtract it so the
        # logged label is now-anchored and comparable to actual_min. MUST stay in
        # sync with the client (index.html analyzeStorm, same frame_age subtract).
        # Geometry: the cell stays on its motion line -> miss distance unchanged,
        # only the time shifts. rv_time + frame_age_min are logged so pre-/post-fix
        # labels stay comparable (old frame-anchored = new + frame_age_min).
        frame_age_min = round((now_ts - rv_time) / 60.0, 1) if rv_time else None
        if eta_min is not None and frame_age_min:
            eta_min = max(0, round(eta_min - frame_age_min))

        rec = {
            "t": now_ts,
            "src": "server",
            "station": {"rain_rate": rain_rate, "rain_day": rain_day,
                        "temp_c": davis.get("temperature_c"),
                        "humidity_pct": davis.get("humidity_pct"),
                        "dew_point_c": davis.get("dew_point_c"),
                        "press": _press_hpa,
                        "press_trend": _pt3,
                        "press_trend_15m": _pt15},  # item 13: precursor fields
            "env": {"cape": round(cape) if cape is not None else None,
                    "li": li},
            "steering": steer,
            "steering_profile": steer_profile,  # item 1: 850/700/500 hPa wind
            "rv_time": rv_time,
            "frame_age_min": frame_age_min,
            "cells": cells,
            "n_cells": len(cells),
            "pred_eta_min": eta_min,
            # item 4: WH57 lightning precursor (None-safe; absent if creds/API down)
            "lightning_count_day": _lt.get("count_day"),
            "lightning_new": _lt.get("new_strikes"),
            "lightning_dist_km": _lt.get("dist_km"),
            "lightning_last_ts": _lt.get("last_ts"),
            "lightning_age_min": _lt.get("age_min"),
            "lightning_batt": _lt.get("battery"),
        }
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
        with open(EVENTLOG_FILE, "a") as fh:
            fh.write(line + "\n")

        # emit a pending prediction only when rain is NOT already falling
        # (we want to predict onset, not log rain that already started)
        if eta_min is not None and (not rain_rate or rain_rate < 0.2):
            pred = {
                "t": now_ts,
                "pred_eta_min": eta_min,
                "frame_age_min": frame_age_min,
                "cell": {"dbz": eta_cell["dbz"], "mm": eta_cell["mm"],
                         "dist": eta_cell["dist"], "brg": eta_cell["brg"],
                         "x": eta_cell["x"], "y": eta_cell["y"], "elev": eta_cell["elev"]},
                "steering": steer,
                "env": {"cape": round(cape) if cape is not None else None, "li": li},
                "press_trend": davis.get("pressure_trend_3h"),
            }
            try:
                with open(PENDING_FILE, "a") as fh:
                    fh.write(json.dumps(pred, separators=(",", ":"), ensure_ascii=False) + "\n")
            except Exception:
                pass

        # resolve any pending predictions against the rain trace
        _resolve_outcomes()

        return len(cells), rain_rate
    except Exception as exc:
        try:
            with open(EVENTLOG_FILE, "a") as fh:
                fh.write(json.dumps({"t": int(time.time()), "src": "server", "error": str(exc)}) + "\n")
        except Exception:
            pass
        return 0, 0


_AUTOLOG_STATUS = {"state": "not-started", "runs": 0, "last_run": 0,
                   "last_error": None, "owner_pid": None, "thread_alive": False}


def _auto_log_loop():
    # adaptive cadence: 3 min when cells/rain present or predictions pending
    # (so rain onset is caught for outcome labeling), else 20 min. The lock is
    # refreshed on a faster heartbeat (every 5 min) so it never goes stale.
    _AUTOLOG_STATUS["state"] = "running"
    _AUTOLOG_STATUS["owner_pid"] = os.getpid()
    next_log = 0
    while True:
        now = time.time()
        if now >= next_log:
            try:
                n_cells, rain_rate = _auto_log_once()
                _AUTOLOG_STATUS["runs"] += 1
                _AUTOLOG_STATUS["last_run"] = int(now)
                _AUTOLOG_STATUS["last_error"] = None
                pending = os.path.exists(PENDING_FILE) and os.path.getsize(PENDING_FILE) > 0
                active = (n_cells > 0) or (rain_rate and rain_rate > 0) or pending
            except Exception as exc:
                _AUTOLOG_STATUS["last_error"] = repr(exc)
                active = False
            next_log = now + (3 * 60 if active else 20 * 60)
        try:
            os.utime(_AUTOLOG_LOCK, None)   # heartbeat: keep ownership fresh
        except Exception:
            pass
        time.sleep(5 * 60)                  # heartbeat interval < 15-min stale window


_AUTOLOG_LOCK = os.path.join(DATA_DIR, "autolog.lock")


_AUTOLOG_STARTED = False   # in-process guard (we run a single worker now)


def _claim_autolog_owner():
    """Single-worker model: the real guard is the in-process flag below. The
    on-disk lock is advisory only and is always (re)claimed on startup, because
    a persisted lock from a prior container is stale by definition — and in
    containers PIDs get reused, so a liveness probe on an old PID is unreliable.
    """
    global _AUTOLOG_STARTED
    if _AUTOLOG_STARTED:
        return False              # already running in THIS process
    _AUTOLOG_STARTED = True
    try:
        fd = os.open(_AUTOLOG_LOCK, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except Exception:
        pass
    return True


def start_autologger():
    if os.environ.get("AUTOLOG", "1") != "1":
        _AUTOLOG_STATUS["state"] = "disabled (AUTOLOG!=1)"
        return
    if not _claim_autolog_owner():
        _AUTOLOG_STATUS["state"] = "not-owner (another worker holds lock)"
        return
    _AUTOLOG_STATUS["state"] = "claimed-starting"

    def _supervise():
        # restart the loop thread if it ever dies, so logging is self-healing
        while True:
            t = threading.Thread(target=_auto_log_loop, daemon=True)
            t.start()
            _AUTOLOG_STATUS["thread_alive"] = True
            t.join()                       # returns only if the loop thread exits
            _AUTOLOG_STATUS["thread_alive"] = False
            _AUTOLOG_STATUS["state"] = "loop-died-restarting"
            time.sleep(10)
    threading.Thread(target=_supervise, daemon=True).start()


# Start the background logger as soon as the module is imported (covers
# gunicorn, which imports the app rather than running __main__).
start_autologger()


# ─────────────────────────────────────────────────────────────────────────
# STORM INITIATION HOTSPOTS
# Reconstructs, from the logged cell snapshots, WHERE cells first appear
# ("first echo"). CDMX convection is largely in-situ/pulse rather than advected,
# so the place and time a cell first shows up is itself the signal: it reveals
# the orographic trigger zones and diurnal timing the learned model should weight.
# Pure post-hoc analysis over storm_events.jsonl — no new capture required.
# Caveats: 3-min cadence bounds temporal precision; a one-frame radar flicker is
# coasted (not counted as a new echo); episode-start cells are first-seen, not
# necessarily first-formed. Interior vs edge separates in-situ from advected-in.
# ─────────────────────────────────────────────────────────────────────────
HS_GAP = 90 * 60          # episode boundary (mirrors _episode_summary)
HS_MATCH_KM = 18.0        # max same-cell jump between consecutive snapshots
HS_EDGE_KM = 7.0          # first echo within this of the viewport edge -> likely advected in
HS_BIN_KM = 3.0           # spatial bin size for the density grid


def _hs_snapshots():
    """Cell-bearing snapshots as (ts, [{x,y,dbz,mm}]), sorted by time."""
    snaps = []
    try:
        with open(EVENTLOG_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                cells = o.get("cells") or []
                if not cells:
                    continue
                ts = o.get("server_ts") or o.get("t")
                if not ts:
                    continue
                pts = []
                for c in cells:
                    x = c.get("x", c.get("x_km"))
                    y = c.get("y", c.get("y_km"))
                    if x is None or y is None:
                        continue
                    pts.append({"x": float(x), "y": float(y),
                                "dbz": c.get("dbz"),
                                "mm": c.get("mm", c.get("maxMM"))})
                if pts:
                    snaps.append((int(ts), pts))
    except FileNotFoundError:
        return []
    snaps.sort(key=lambda s: s[0])
    return snaps


def _hs_initiations():
    """First-echo (initiation) points: reconstruct cell tracks within each
    episode and flag cells with no predecessor in the prior frame."""
    snaps = _hs_snapshots()
    inits = []
    episodes = 0

    def flush(ep):
        active = []   # [{x,y,miss}] tracks alive in/near the previous frame
        for k, (ts, pts) in enumerate(ep):
            # global greedy nearest-neighbor match current cells <-> active tracks
            pairs = []
            for ci, c in enumerate(pts):
                for ai, a in enumerate(active):
                    pairs.append((math.hypot(a["x"] - c["x"], a["y"] - c["y"]), ci, ai))
            pairs.sort()
            mc, ma = {}, {}
            for d, ci, ai in pairs:
                if d > HS_MATCH_KM:
                    break
                if ci in mc or ai in ma:
                    continue
                mc[ci] = ai
                ma[ai] = ci
            new_active = []
            for ci, c in enumerate(pts):
                new_active.append({"x": c["x"], "y": c["y"], "miss": 0})
                if ci in mc:
                    continue
                x, y = c["x"], c["y"]
                edge = (abs(x) > AW_KM - HS_EDGE_KM) or (abs(y) > AH_KM - HS_EDGE_KM)
                inits.append({
                    "t": ts,
                    "lat": round(LAT + y / KM_LAT, 5),
                    "lon": round(LON + x / KM_LON, 5),
                    "x": round(x, 1), "y": round(y, 1),
                    "dbz": c.get("dbz"), "mm": c.get("mm"),
                    "kind": "edge" if edge else "interior",
                    "phase": "start" if k == 0 else "mid",
                })
            # coast unmatched tracks one frame so a flicker isn't a new echo
            for ai, a in enumerate(active):
                if ai not in ma and a["miss"] < 1:
                    new_active.append({"x": a["x"], "y": a["y"], "miss": a["miss"] + 1})
            active = new_active

    ep = []
    last = None
    for ts, pts in snaps:
        if last is not None and ts - last > HS_GAP:
            episodes += 1
            flush(ep)
            ep = []
        ep.append((ts, pts))
        last = ts
    if ep:
        episodes += 1
        flush(ep)
    return inits, episodes


def _hs_grid(points):
    """Bin interior initiations into ~HS_BIN_KM cells for a density view."""
    bins = {}
    for p in points:
        if p["kind"] != "interior":
            continue
        key = (math.floor(p["x"] / HS_BIN_KM), math.floor(p["y"] / HS_BIN_KM))
        bins[key] = bins.get(key, 0) + 1
    out = []
    for (bx, by), n in bins.items():
        cx = (bx + 0.5) * HS_BIN_KM
        cy = (by + 0.5) * HS_BIN_KM
        out.append({"lat": round(LAT + cy / KM_LAT, 5),
                    "lon": round(LON + cx / KM_LON, 5), "count": n})
    out.sort(key=lambda b: -b["count"])
    return out


def _hs_by_hour(points):
    """CDMX local-hour histogram of initiations (the diurnal signal)."""
    import datetime as _dt
    tz = _dt.timezone(_dt.timedelta(hours=-6))
    hours = [0] * 24
    for p in points:
        hours[_dt.datetime.fromtimestamp(p["t"], tz).hour] += 1
    return hours


def _first_echo_count():
    try:
        with open(FIRST_ECHO_FILE) as fh:
            return sum(1 for ln in fh if ln.strip())
    except Exception:
        return 0


@app.route("/api/hotspots")
def hotspots_json():
    pts, episodes = _hs_initiations()
    interior = [p for p in pts if p["kind"] == "interior"]
    edge = [p for p in pts if p["kind"] == "edge"]
    return jsonify({
        "ok": True,
        "episodes": episodes,
        "n_initiations": len(pts),
        "interior": len(interior),
        "edge": len(edge),
        "points": pts,
        "grid": _hs_grid(pts),
        "by_hour": _hs_by_hour(pts),
        "params": {"gap_min": HS_GAP // 60, "match_km": HS_MATCH_KM,
                   "edge_km": HS_EDGE_KM, "bin_km": HS_BIN_KM,
                   "viewport_km": [AW_KM, AH_KM]},
        "explicit_first_echoes": _first_echo_count(),
        "note": ("Interior first-echoes are in-situ initiation candidates; 'edge' "
                 "first-echoes likely advected into the radar viewport. Preliminary "
                 "until the episode count is high. 'explicit_first_echoes' counts "
                 "live-logged cell births accumulating for a future authoritative map."),
    })


HS_PAGE = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Virreyes \u00b7 Hotspots de iniciacion</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:18px;max-width:900px;margin-left:auto;margin-right:auto}
h1{font-size:1.05rem;letter-spacing:.03em}
.muted{color:#9aa3b2}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px;margin:12px 0}
#map{height:520px;border-radius:12px;border:1px solid #30363d}
.row{display:flex;gap:18px;flex-wrap:wrap;font-family:ui-monospace,monospace;font-size:.85rem}
.k{color:#9aa3b2}
.hours{display:flex;gap:3px;align-items:flex-end;height:70px;margin-top:8px}
.hb{flex:1;background:#f78166;border-radius:2px 2px 0 0;min-height:2px}
.hl{display:flex;gap:3px;font-family:ui-monospace,monospace;font-size:.55rem;color:#7d8694;margin-top:3px}
.hl span{flex:1;text-align:center}
.lg{font-family:ui-monospace,monospace;font-size:.7rem;color:#aeb6c2;margin-top:8px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;vertical-align:-1px;margin-right:4px}
a{color:#58a6ff}
</style></head><body>
<h1>&#x26C8; Hotspots de iniciacion &mdash; Valle de Mexico</h1>
<div class="muted" style="font-size:.8rem;margin-bottom:6px">Donde aparecen por primera vez las celdas (primer eco) en el registro de la estacion.</div>
<div class="card"><div class="row" id="summary"><span class="muted">Cargando...</span></div>
<div class="lg"><span class="dot" style="background:#f78166"></span>iniciacion in-situ (interior)
&nbsp;&nbsp;<span class="dot" style="background:#8b949e"></span>entro por el borde (probable adveccion)
&nbsp;&nbsp;<span class="dot" style="background:#58a6ff"></span>Virreyes</div></div>
<div id="map"></div>
<div class="card">
<div class="k" style="font-size:.7rem;letter-spacing:.1em;text-transform:uppercase">Iniciaciones por hora (CDMX)</div>
<div class="hours" id="hours"></div>
<div class="hl" id="hourlbl"></div>
</div>
<div class="card muted" style="font-size:.78rem;line-height:1.6" id="note"></div>
<p class="muted" style="font-size:.72rem"><a href="/api/hotspots">JSON</a> &middot; <a href="/readiness">readiness</a> &middot; <a href="/">dashboard</a></p>
<script>
const LAT=19.41997, LON=-99.21059;
const map=L.map('map').setView([LAT,LON],10);
L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png',
  {attribution:'&copy; CARTO &copy; OpenStreetMap',maxZoom:19,subdomains:'abcd'}).addTo(map);
[10000,20000].forEach(r=>L.circle([LAT,LON],{radius:r,color:'#3d4654',weight:1,dashArray:'5,5',fill:false,opacity:.5}).addTo(map));
L.circleMarker([LAT,LON],{radius:7,color:'#0d1117',weight:2,fillColor:'#58a6ff',fillOpacity:1}).addTo(map);
fetch('/api/hotspots').then(r=>r.json()).then(d=>{
  document.getElementById('summary').innerHTML=
    '<span><span class="k">Episodios</span> '+d.episodes+'</span>'+
    '<span><span class="k">Iniciaciones</span> '+d.n_initiations+'</span>'+
    '<span><span class="k">Interior (in-situ)</span> '+d.interior+'</span>'+
    '<span><span class="k">Borde</span> '+d.edge+'</span>';
  (d.grid||[]).forEach(b=>{ if(b.count>=2){
    const dlat=1.5/111.0, dlon=1.5/(111.0*Math.cos(LAT*Math.PI/180));
    L.rectangle([[b.lat-dlat,b.lon-dlon],[b.lat+dlat,b.lon+dlon]],
      {color:'#f78166',weight:0,fillColor:'#f78166',fillOpacity:Math.min(.5,.12*b.count)}).addTo(map);
  }});
  (d.points||[]).forEach(p=>{
    if(p.kind==='edge'){
      L.circleMarker([p.lat,p.lon],{radius:3,color:'#8b949e',weight:1,fillColor:'#8b949e',fillOpacity:.5}).addTo(map);
    } else {
      const rad=p.dbz?Math.max(4,Math.min(9,(p.dbz-25)/4+4)):5;
      L.circleMarker([p.lat,p.lon],{radius:rad,color:'#0d1117',weight:1,fillColor:'#f78166',fillOpacity:.85})
        .addTo(map).bindPopup('Primer eco '+(p.dbz||'?')+' dBZ'+(p.phase==='mid'?' (nueva en episodio)':''));
    }
  });
  const hours=d.by_hour||[]; const mx=Math.max(1,...hours);
  document.getElementById('hours').innerHTML=hours.map(h=>'<div class="hb" style="height:'+Math.round(100*h/mx)+'%" title="'+h+'h: '+h+'"></div>').join('');
  document.getElementById('hourlbl').innerHTML=hours.map((h,i)=> i%3===0?('<span>'+i+'</span>'):'<span></span>').join('');
  document.getElementById('note').textContent=d.note+' Parametros: gap '+d.params.gap_min+' min, salto max '+d.params.match_km+' km, borde '+d.params.edge_km+' km.';
}).catch(e=>{ document.getElementById('summary').textContent='Error cargando datos: '+e; });
</script>
</body></html>"""


@app.route("/hotspots")
def hotspots_page():
    r = make_response(HS_PAGE)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    r.headers["Cache-Control"] = "no-store"
    return r


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
