import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import threading
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

# item 4b: WH57 interference / Blitzortung-corroboration gate (all tunable).
WH57_K_STUCK = 3            # K identical distances within the last N -> EMI "stuck distance"
WH57_N_STUCK = 6            # window for the K-of-N stuck test (was: 3 consecutive)
WH57_R_ECHO_KM = 40.0       # Phase-1 env flag: no radar cell within this at strike time
WH57_RH_DRY_PCT = 50.0      # Phase-1 env flag: low-level RH below this = implausibly dry
WH57_DT_WINDOW = 5 * 60     # Phase-2 corroboration window (s, +/-), UTC epoch
WH57_R_CORROB_KM = 40.0     # Phase-2 corroboration radius (presence test); log min_km to tighten
_WH57_DIST_HIST = []        # ring buffer of recent strike distances (cap WH57_K_STUCK)
_WH57_PERSIST = {"verdict": None}   # last per-strike verdict (loaded from /data at boot)


def _wh57_gate_file():
    # resolved at call time: DATA_DIR is defined further down the module
    return os.path.join(DATA_DIR, "wh57_gate.json")


def _wh57_day():
    import datetime as _dt
    return _dt.datetime.fromtimestamp(time.time(), _dt.timezone(_dt.timedelta(hours=-6))).strftime("%Y-%m-%d")


def _wh57_flagged_update(val):
    """Maintain today's fingerprint-flagged strike list (display-suppression
    semantics: stuck fingerprint AND not corroborated). Later corroboration
    un-flags. Persisted via the gate file -> survives restarts, converges
    across processes like the verdicts."""
    d = _wh57_day()
    if _WH57_PERSIST.get("flag_day") != d:
        _WH57_PERSIST["flag_day"] = d
        _WH57_PERSIST["flagged"] = []
    ts = val.get("last_ts")
    if ts is None:
        return
    lst = _WH57_PERSIST.setdefault("flagged", [])
    fp = bool(val.get("interference_stuck"))
    corrob = val.get("corrob_status") == "corroborated"
    if fp and not corrob:
        if ts not in lst:
            lst.append(ts)
    elif corrob and ts in lst:
        lst.remove(ts)


def _wh57_gate_save(val):
    """Persist the stuck-distance ring + the last strike's verdict to /data
    (atomic write), so a restart neither resets stuck-detection nor resurrects
    ghosts (a pre-restart phantom re-reading as 'unavailable' and un-suppressing)."""
    try:
        v = {"last_ts": val.get("last_ts"), "corrob_status": val.get("corrob_status"),
             "corrob_n": val.get("corrob_n"), "corrob_min_km": val.get("corrob_min_km"),
             "suspected_interference": val.get("suspected_interference"),
             "interference_reason": val.get("interference_reason")}
        # no-downgrade: 'unavailable' (couldn't check) must never overwrite a
        # persisted REAL verdict for the same strike
        pv = _WH57_PERSIST.get("verdict") or {}
        if (v.get("corrob_status") == "unavailable" and pv.get("last_ts") == v.get("last_ts")
                and pv.get("corrob_status") in ("corroborated", "uncorroborated")):
            v = pv
        _WH57_PERSIST["verdict"] = v
        tmp = _wh57_gate_file() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"dist_hist": list(_WH57_DIST_HIST),
                       "count_day": _ECOWITT_LAST.get("count_day"),
                       "verdict": v,
                       "flag_day": _WH57_PERSIST.get("flag_day"),
                       "flagged": _WH57_PERSIST.get("flagged", [])}, fh)
        os.replace(tmp, _wh57_gate_file())
    except Exception:
        pass


def _wh57_gate_load():
    try:
        with open(_wh57_gate_file()) as fh:
            d = json.load(fh)
        _WH57_DIST_HIST[:] = list(d.get("dist_hist") or [])[-WH57_N_STUCK:]
        if d.get("count_day") is not None:
            _ECOWITT_LAST["count_day"] = d["count_day"]
        _WH57_PERSIST["verdict"] = d.get("verdict") or None
        _WH57_PERSIST["flag_day"] = d.get("flag_day")
        _WH57_PERSIST["flagged"] = list(d.get("flagged") or [])
    except Exception:
        pass


# item 14 Phase 2: server-side Blitzortung listener for WH57 corroboration. The
# Blitz feed is WebSocket-only and global; we keep a small buffer of recent strikes
# NEAR the station and corroborate each WH57 strike against it. Conservative: feed
# down / window not covered -> 'unavailable' (never 'interference').
_BLITZ_STRIKES = []                 # [(ts_epoch_s, lat, lon)] near-station, time-pruned
_BLITZ_LOCK = threading.Lock()
_BLITZ_LAST_MSG = 0                 # epoch s of the last decoded strike (feed-health probe)
_BLITZ_SINCE = 0                    # epoch s of first received data (coverage start)
_BLITZ_STATE = {"connected": False, "host": None, "msgs": 0, "near": 0, "last_error": None}


def _blitz_decode(text):
    """LZW-style decode mirroring the client ltDecode() (unicode in/out)."""
    if not text:
        return ""
    curr = text[0]; old = curr; out = [curr]; code = 256; d = {}
    for i in range(1, len(text)):
        cc = ord(text[i])
        phrase = text[i] if cc < 256 else d.get(cc, old + curr)
        out.append(phrase)
        curr = phrase[0]
        d[code] = old + curr; code += 1
        old = phrase
    return "".join(out)


def _haversine_km(lat1, lon1, lat2, lon2):
    rad = math.radians
    p1, p2 = rad(lat1), rad(lat2)
    a = (math.sin(rad(lat2 - lat1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(rad(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def _blitz_corroborate(wh57_ts):
    """Corroborate a WH57 strike (epoch s) against the Blitz buffer ->
    (status, n_in_radius, nearest_km_in_window). status: corroborated /
    uncorroborated / unavailable. 'unavailable' = couldn't actually check (no
    strike, stale feed, or window predates coverage) -- never 'no lightning'."""
    if wh57_ts is None:
        return "unavailable", None, None
    now = int(time.time())
    if not _BLITZ_LAST_MSG or (now - _BLITZ_LAST_MSG) > 180:
        return "unavailable", None, None
    if not _BLITZ_SINCE or (wh57_ts + WH57_DT_WINDOW) < _BLITZ_SINCE:
        return "unavailable", None, None
    lo, hi = wh57_ts - WH57_DT_WINDOW, wh57_ts + WH57_DT_WINDOW
    with _BLITZ_LOCK:
        strikes = list(_BLITZ_STRIKES)
    n, min_km = 0, None
    for (ts, lat, lon) in strikes:
        if lo <= ts <= hi:
            dkm = _haversine_km(LAT, LON, lat, lon)
            if min_km is None or dkm < min_km:
                min_km = dkm
            if dkm <= WH57_R_CORROB_KM:
                n += 1
    status = "corroborated" if n > 0 else "uncorroborated"
    return status, n, (round(min_km, 1) if min_km is not None else None)


def _blitz_listener():
    """Daemon: keep a Blitzortung WS connection, decode strikes, maintain a small
    near-station buffer. Reconnects with backoff; no-ops gracefully (status stays
    'unavailable') if websocket-client is missing or the feed is down."""
    global _BLITZ_LAST_MSG, _BLITZ_SINCE
    try:
        import websocket
    except Exception as exc:
        _BLITZ_STATE["last_error"] = "websocket-client missing: %r" % (exc,)
        return
    hosts = ["wss://ws1.blitzortung.org/", "wss://ws7.blitzortung.org/", "wss://ws8.blitzortung.org/"]
    hi = 0
    while True:
        url = hosts[hi % len(hosts)]; hi += 1
        ws = None
        try:
            ws = websocket.create_connection(url, timeout=20)
            ws.send(json.dumps({"a": 111}))
            _BLITZ_STATE.update({"connected": True, "host": url, "last_error": None})
            while True:
                msg = ws.recv()
                if not msg:
                    continue
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "ignore")
                try:
                    strike = json.loads(_blitz_decode(msg))
                except Exception:
                    continue
                lat = strike.get("lat"); lon = strike.get("lon"); t = strike.get("time")
                if lat is None or lon is None:
                    continue
                nowi = int(time.time())
                _BLITZ_LAST_MSG = nowi
                _BLITZ_STATE["msgs"] += 1
                if not _BLITZ_SINCE:
                    _BLITZ_SINCE = nowi
                ts = int(t / 1e9) if t else nowi          # Blitz 'time' is nanoseconds
                if _haversine_km(LAT, LON, lat, lon) <= 150:
                    cutoff = nowi - 2 * 3600
                    with _BLITZ_LOCK:
                        _BLITZ_STRIKES.append((ts, lat, lon))
                        if len(_BLITZ_STRIKES) > 5000 or _BLITZ_STRIKES[0][0] < cutoff:
                            _BLITZ_STRIKES[:] = [x for x in _BLITZ_STRIKES if x[0] >= cutoff][-5000:]
                    _BLITZ_STATE["near"] += 1
        except Exception as exc:
            _BLITZ_STATE.update({"connected": False, "last_error": repr(exc)})
        finally:
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
        time.sleep(6)


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
            # cross-process convergence: another process (or a pre-restart run) may
            # have advanced the ring/baseline or stamped a verdict since our boot.
            # The in-memory copy is only authoritative for THIS process; /data is
            # the shared truth. Cheap: tiny file, and this path runs <=1/min.
            _wh57_gate_load()
            val, _ECOWITT_LAST["count_day"] = _parse_ecowitt_lightning(
                body, now, _ECOWITT_LAST["count_day"])
            if val is None:
                result = {"available": False, "reason": "no_lightning_field"}
            else:
                # item 4b Phase 1: stuck-distance EMI fingerprint. Each new strike
                # pushes its distance into a small ring; K identical consecutive
                # distances = suspected interference (real strikes scatter in range).
                if val.get("new_strikes"):
                    _WH57_DIST_HIST.append(val.get("dist_km"))
                    del _WH57_DIST_HIST[:-WH57_N_STUCK]
                _ring = _WH57_DIST_HIST[-WH57_N_STUCK:]
                stuck = (val.get("dist_km") is not None
                         and _ring.count(val.get("dist_km")) >= WH57_K_STUCK)
                val["interference_stuck"] = stuck
                # item 14 Phase 2: corroborate the last strike against Blitzortung.
                _st, _cn, _cmin = _blitz_corroborate(val.get("last_ts"))
                # gate persistence: a restart resets Blitz coverage, so a strike
                # verdicted BEFORE the restart re-reads 'unavailable' and would
                # un-suppress (ghost resurrection). If this exact strike already
                # carries a persisted REAL verdict, adopt it instead.
                _pv = _WH57_PERSIST.get("verdict") or {}
                if (_st == "unavailable" and _pv.get("last_ts") == val.get("last_ts")
                        and _pv.get("corrob_status") in ("corroborated", "uncorroborated")):
                    _st, _cn, _cmin = _pv["corrob_status"], _pv.get("corrob_n"), _pv.get("corrob_min_km")
                val["corrob_status"] = _st
                val["corrob_n"] = _cn
                val["corrob_min_km"] = _cmin
                val["corroborated"] = (_st == "corroborated")
                _reasons = []
                if stuck:
                    _reasons.append("stuck_distance")
                if _st == "uncorroborated":      # checked and no real strike near -> suspect
                    _reasons.append("no_blitz_corroboration")
                val["suspected_interference"] = bool(_reasons)   # env check added autolog-side
                val["interference_reason"] = "+".join(_reasons) if _reasons else None
                _wh57_flagged_update(val)                        # passenger: server count
                val["flagged_today"] = len(_WH57_PERSIST.get("flagged", []))
                _wh57_gate_save(val)     # persist ring + verdict + flags (survives restarts)
                result = {**val, "available": True}
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
    # beta instruments (item 19): read-through of the latest shadow score +
    # subthresh block. DISPLAY-ONLY downstream -- feeds no ops/banner logic.
    try:
        with open(BETA_LAST_FILE) as fh:
            parsed["beta"] = json.load(fh)
    except Exception:
        parsed["beta"] = None
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
# template only -- LAT/LON are defined further down the module, so the URL is
# formatted at call time (module-level formatting was a boot NameError, 26-jul)
FX_URL_TMPL = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
               "&hourly=temperature_2m,precipitation_probability,precipitation,weather_code,"
               "wind_speed_700hPa,wind_direction_700hPa,cape,lifted_index,freezing_level_height,"
               "wind_speed_10m,wind_direction_10m,wind_speed_500hPa,wind_direction_500hPa"
               "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
               "precipitation_probability_max,precipitation_sum"
               "&minutely_15=precipitation&current=precipitation,weather_code"
               "&past_hours=24&timezone=America%%2FMexico_City&forecast_days=5")


def _fx_get():
    """Fresh-enough shared Open-Meteo payload (fetch if stale, keep stale on
    failure). Owner-process safe: the autologger may hold a cold cache even
    while clients hit the other worker's proxy. Never raises."""
    now = time.time()
    if not _FX_CACHE["data"] or now - _FX_CACHE["t"] >= FX_TTL:
        try:
            status, data = fetch_json(FX_URL_TMPL % (LAT, LON))
            if status == 200 and isinstance(data, dict) and data.get("hourly"):
                _FX_CACHE["t"] = now
                _FX_CACHE["data"] = data
        except Exception:
            pass
    return _FX_CACHE["data"]


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
    try:
        status, data = fetch_json(FX_URL_TMPL % (LAT, LON))
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
# beta-gauge read-through: latest shadow score + subthresh block, written by
# the autolog full cycle, read by /api/current. On /data so ANY process can
# serve it truthfully (item-16 lesson: boot-load / in-memory is not a
# cross-process propagation story).
BETA_LAST_FILE = os.path.join(DATA_DIR, "beta_last.json")


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
  <a href="/weakgate">backtest gate eco-debil</a> &nbsp;\u00b7&nbsp;
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


# item 11 watch-point 1: onset debounce is TIME-based. At 1-min cadence a
# single bucket-tip can show a transient rate >= WET for one reading; an onset
# now requires either an instant-heavy rate or a CONFIRMING wet reading within
# ONSET_CONFIRM_S. (At the old 5-min cadence, consecutive wet rows confirm
# naturally, so historical onsets are essentially unchanged; isolated one-row
# blips are dropped by design.)
ONSET_CONFIRM_S = 6 * 60      # a second wet reading within this confirms
ONSET_INSTANT_MM = 1.0        # or a single reading at/above this rate


def _confirmed_wet(series, i):
    """series = [(t, rr, ...)] sorted; True if the wet reading at i is a real
    onset start (instant-heavy, or confirmed by another wet reading soon)."""
    t, rr = series[i][0], series[i][1]
    if rr >= ONSET_INSTANT_MM:
        return True
    for j in range(i + 1, len(series)):
        t2, rr2 = series[j][0], series[j][1]
        if t2 - t > ONSET_CONFIRM_S:
            break
        if rr2 >= 0.2:
            return True
    return False


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
                if o.get("src") == "station":
                    nc = None      # 1-min station row: no radar context of its own
                else:
                    nc = o.get("n_cells")
                    if nc is None:
                        nc = len(o.get("cells") or [])
                series.append((int(ts), float(st.get("rain_rate") or 0), nc))
    except FileNotFoundError:
        return []
    series.sort()

    full_nc = [(t, nc) for (t, rr, nc) in series if nc is not None]

    def _nc_at(t):
        """Radar context for a station-row onset: nearest full snapshot <=10 min."""
        best = None
        for (t2, nc2) in full_nc:
            if best is None or abs(t2 - t) < abs(best[0] - t):
                best = (t2, nc2)
        return best[1] if (best and abs(best[0] - t) <= 600) else 0

    onsets, last_wet_t = [], None
    for i, (t, rr, nc) in enumerate(series):
        if rr >= WET:
            if (last_wet_t is None or (t - last_wet_t) > DRY) and _confirmed_wet(series, i):
                onsets.append((t, nc if nc is not None else _nc_at(t)))
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
                    if r.get("t") is not None and r.get("src") != "station":
                        snaps.append(r)      # item 11: 1-min station rows are for
        except Exception:                    # onset timing; precursor/closest-
            pass                             # approach keep full snapshots only
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
            "blitz": {**_BLITZ_STATE, "buffered": len(_BLITZ_STRIKES),
                      "last_msg_age_s": (int(time.time()) - _BLITZ_LAST_MSG) if _BLITZ_LAST_MSG else None},
            "insitu_v1": {"loaded": _INSITU["model"] is not None,
                          "version": (_INSITU["model"] or {}).get("version"),
                          "features": len((_INSITU["model"] or {}).get("features", [])),
                          "threshold": (_INSITU["model"] or {}).get("threshold"),
                          "reason": _INSITU["reason"]},
            "nowcast": _nowcast_health(),
            "goes": _goes_health(),
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


# white = RainViewer snow/mixed-phase: heavy frozen precip, NOT drizzle. The
# old 0.3 mapping punched artificial low-intensity holes in sierra storm
# masses (the initiation upstream), corrupting subthresh counts, high-terrain
# cell detection, and the nowcast input. MUST equal the client's
# SNOW_MIXED_MMH (train/serve parity). ~2.0 mm/h ~= 27.8 dBZ: counts in the
# subthresh 25-30 band, below the 30 dBZ cell floor by choice.
SNOW_MIXED_MMH = 2.0


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
        return SNOW_MIXED_MMH
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


# ── sub-threshold echo logging (v2 runway) ──────────────────────────────
# Counts RainViewer pixels in two bands BELOW the 30 dBZ cell floor (20-25,
# 25-30 dBZ via the same Z-R mapping as CELL_DBZ_MIN), their ~10-min trend, and
# the fraction falling inside known initiation-hotspot bins. Pure add-only
# logging for future trigger-observation features; runs every cycle, so the
# scan reuses _detect_cells' S=3 downsampling (each sample ~ (3/ppk)^2 km^2).
# NOTE: the RV viewport spans +/-32 x +/-30 km, so the effective radius is the
# viewport (max corner ~44 km), inside the requested 60 km everywhere.
SUBTHR_MM_20 = _dbz_to_mm(20)
SUBTHR_MM_25 = _dbz_to_mm(25)
SUBTHR_HOT_MIN = 2          # a hotspot bin = >= this many logged interior initiations
_SUBTHR_HIST = []           # [(rv_time, n2025, n2530)] last few frames
_SUBTHR_HOT = {"t": 0.0, "bins": None}


def _subthresh_hot_bins():
    """Set of hotspot (bx,by) bins from the initiation history; cached 6 h
    (the reconstruction reads the whole event log)."""
    now = time.time()
    if _SUBTHR_HOT["bins"] is not None and now - _SUBTHR_HOT["t"] < 6 * 3600:
        return _SUBTHR_HOT["bins"]
    bins = set()
    try:
        pts, _ = _hs_initiations()
        counts = {}
        for pt in pts:
            if pt.get("kind") != "interior":
                continue
            key = (math.floor(pt["x"] / HS_BIN_KM), math.floor(pt["y"] / HS_BIN_KM))
            counts[key] = counts.get(key, 0) + 1
        bins = {k for k, n in counts.items() if n >= SUBTHR_HOT_MIN}
    except Exception:
        bins = set()
    _SUBTHR_HOT["t"] = now
    _SUBTHR_HOT["bins"] = bins
    return bins


def _subthresh_scan(field):
    """One S=3 pass over the RV frame -> band counts + hotspot overlap."""
    px, AW, AH, ppk = field
    S = 3
    thr30 = CELL_MM_THR
    hot = _subthresh_hot_bins()
    n2025 = n2530 = h2025 = h2530 = 0
    for j in range(AH // S):
        yy = j * S + 1
        y_km = (AH / 2 - yy) / ppk
        for i in range(AW // S):
            r, g, b, a = px[i * S + 1, yy]
            v = _rv_palette_mm(r, g, b, a)
            if v < SUBTHR_MM_20 or v >= thr30:
                continue
            x_km = (i * S + 1 - AW / 2) / ppk
            if x_km * x_km + y_km * y_km > 3600.0:      # 60 km cap (viewport is inside it)
                continue
            inhot = (math.floor(x_km / HS_BIN_KM), math.floor(y_km / HS_BIN_KM)) in hot
            if v < SUBTHR_MM_25:
                n2025 += 1; h2025 += inhot
            else:
                n2530 += 1; h2530 += inhot
    return n2025, n2530, h2025, h2530


def _subthresh_block(field, rv_time):
    """Assemble the snapshot block; caches per RV frame; trend vs the most
    recent PRIOR frame within 25 min. Never raises past the caller's guard."""
    global _SUBTHR_HIST
    if field is None or rv_time is None:
        return None, "no_rv_field"
    hist = {t: (a, b) for (t, a, b) in _SUBTHR_HIST}
    if rv_time in hist:
        n2025, n2530 = hist[rv_time]
        # hotspot fractions not cached; rescan avoided -> reuse previous block shape
        h2025 = h2530 = None
    else:
        n2025, n2530, h2025, h2530 = _subthresh_scan(field)
        _SUBTHR_HIST.append((rv_time, n2025, n2530))
        _SUBTHR_HIST = _SUBTHR_HIST[-4:]
    prev = [(t, a, b) for (t, a, b) in _SUBTHR_HIST if t < rv_time and rv_time - t <= 25 * 60]
    d2025 = d2530 = None
    if prev:
        t0, a0, b0 = max(prev, key=lambda x: x[0])
        d2025, d2530 = n2025 - a0, n2530 - b0
    blk = {"n_px_20_25": n2025, "n_px_25_30": n2530,
           "d_px_20_25": d2025, "d_px_25_30": d2530,
           "hot_frac_20_25": (round(h2025 / n2025, 3) if (h2025 is not None and n2025) else (0.0 if h2025 is not None else None)),
           "hot_frac_25_30": (round(h2530 / n2530, 3) if (h2530 is not None and n2530) else (0.0 if h2530 is not None else None)),
           "ds": 3, "rv_time": rv_time}
    return blk, None


# ─────────────────────────────────────────────────────────────────────────
# "+30 min extrapolación" NOWCAST (client-facing; server-computed)
# Dense optical flow (OpenCV Farneback) on the last 3 RainViewer frames over a
# 300x300 km box -> the two pair-flows averaged -> semi-Lagrangian advection
# to +5..+30 min in 5-min steps. PURE MOTION EXTRAPOLATION: it moves existing
# echoes and cannot create new ones -- the UI must say so (pulse-storm days
# initiate in place; a clear +30 frame is NOT "no rain coming").
# Runs owner-side strictly AFTER each full cycle, fail-safe wrapped (any error
# -> nowcast unavailable, cycle unharmed). Artifacts persist to /data/nowcast
# (one PNG per step + meta.json, atomic) so ANY process can serve them --
# item-16 lesson: cross-process state lives on disk.
# Scope-gate numbers (18 Jul, local, then-200x200 grid): flow 0.04 s + advect
# 0.003 s + encode <0.01 s; ~3 s/frame fetch (4 z=7 512px tiles, ~26 KB);
# steady state refetches only NEW frames (radar cadence 10 min > cycle 3 min,
# so most cycles skip entirely). Deps: numpy + opencv-python-headless.
NC_HALF_KM = 150.0
# NATIVE-RESOLUTION grid (20 Jul, final display setting): advection runs at
# the source canvas resolution (~0.58 km/px, ~520 px over the 300 km box) --
# the ingest downsample is gone entirely. Evidence: user side-by-side (18:36)
# showed the coarser grid hiding real cores the observed tiles resolve
# crisply; radar-scale pixels ARE the observed-tile format. Grid size derives
# from the fetched canvas at runtime (km/px = 2*NC_HALF_KM / n); PNGs render
# 1:1 and the client shows them pixelated, same as the observed layer.
NC_Z = 7
NC_LEADS = (5, 10, 15, 20, 25, 30)
NC_FRESH_S = 25 * 60         # serveable while younger than this (2 radar frames)
NC_MAX_FRAME_GAP_S = 20 * 60  # refuse flow across a gappy frame history
NC_DIR = os.path.join(DATA_DIR, "nowcast")
_NC_CACHE = {"grids": {}}    # frame path -> (frame_time, mm/h ndarray)
_NC_LAST = {"computed_at": None, "base_time": None, "base_path": None,
            "compute_s": None,
            "ok": None, "error": None}   # owner-process view (B4 row fields)
NC_STALE_BASE_S = 15 * 60    # base older than list head by this -> withhold all


def _nc_class_set(rgba, np):
    """Palette classes present in an RGBA frame (via the shared decode)."""
    return set(np.unique(_nc_palette_mm_vec(rgba, np)).tolist())


def _nc_assert_subset(warped, base_classes, np, label):
    """24-jul invariant (post-freeze regression report): every pixel class in
    a served nowcast frame must exist in its declared base frame — the
    nearest-warp guarantee. Fail-safe: a violation raises, the cycle serves
    NO nowcast, and the violation is logged; stale/invented rain is never
    preferable to an absent overlay."""
    extra = _nc_class_set(warped, np) - base_classes - {0.0}
    if extra:
        detail = "%s:extra_classes=%s" % (label, sorted(extra))
        try:
            with open(os.path.join(NC_DIR, "violations.jsonl"), "a") as fh:
                fh.write(json.dumps({"ts": int(time.time()), "detail": detail}) + "\n")
        except Exception:
            pass
        raise RuntimeError("invariant_violation:" + detail)


def _nc_palette_mm_vec(arr, np):
    """Vectorized _rv_palette_mm -- MUST stay in sync with the scalar version
    (assignments in reverse priority order so earlier scalar branches win)."""
    r = arr[..., 0].astype(np.int16); g = arr[..., 1].astype(np.int16)
    b = arr[..., 2].astype(np.int16); a = arr[..., 3]
    out = np.full(r.shape, 0.8, dtype=np.float32)
    out[(r > 210) & (g > 210) & (b > 210)] = SNOW_MIXED_MMH
    out[(b > 150) & (r < 150) & (g <= 175)] = 1.5
    out[(b > 150) & (r < 150) & (g > 175)] = 0.5
    out[(r > 195) & (g > 195) & (b < 150)] = 3.0
    out[(r > 200) & (g > 130) & (g < 200) & (b < 120)] = 5.0
    out[(r > 190) & (g < 130) & (b < 130)] = 9.0
    out[(r > 170) & (g < 130) & (b > 150)] = 14.0
    out[a < 40] = 0.0
    return out


def _nc_fetch_grid(host, fpath, np):
    """Compose z=7 512px tiles over the 300 km box -> native-resolution
    (~520 x ~520, ~0.58 km/px) mm/h array, or None. Same tile source/palette
    as _fetch_rv_field, wider box."""
    from PIL import Image
    import io as _io
    min_lon = LON - NC_HALF_KM / KM_LON
    max_lon = LON + NC_HALF_KM / KM_LON
    min_lat = LAT - NC_HALF_KM / KM_LAT
    max_lat = LAT + NC_HALF_KM / KM_LAT
    tx0, tx1 = int(_lon2tx(min_lon, NC_Z)), int(_lon2tx(max_lon, NC_Z))
    ty0, ty1 = int(_lat2ty(max_lat, NC_Z)), int(_lat2ty(min_lat, NC_Z))

    def tx2lon(tx):
        return tx / (2 ** NC_Z) * 360.0 - 180.0

    def ty2lat(ty):
        n = math.pi - 2 * math.pi * ty / (2 ** NC_Z)
        return math.degrees(math.atan(0.5 * (math.exp(n) - math.exp(-n))))

    ppk = 512.0 / ((tx2lon(tx0 + 1) - tx2lon(tx0)) * KM_LON)   # px per km
    W = int(NC_HALF_KM * 2 * ppk)
    canvas = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    got = False
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            data = None
            for size in (512, 256):
                url = f"{host}{fpath}/{size}/{NC_Z}/{tx}/{ty}/2/1_1.png"
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "virreyes-weather/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = resp.read()
                    break
                except Exception:
                    continue
            if not data:
                continue
            try:
                tile = Image.open(_io.BytesIO(data)).convert("RGBA")
            except Exception:
                continue
            xa = W / 2 + (tx2lon(tx) - LON) * KM_LON * ppk
            ya = W / 2 - (ty2lat(ty) - LAT) * KM_LAT * ppk
            xb = W / 2 + (tx2lon(tx + 1) - LON) * KM_LON * ppk
            yb = W / 2 - (ty2lat(ty + 1) - LAT) * KM_LAT * ppk
            w, h = max(1, int(round(xb - xa))), max(1, int(round(yb - ya)))
            try:
                canvas.paste(tile.resize((w, h)), (int(round(xa)), int(round(ya))),
                             tile.resize((w, h)))
                got = True
            except Exception:
                continue
    if not got:
        return None
    arr = np.asarray(canvas, dtype=np.uint8)
    # (mm/h for flow/analysis, raw RGBA for display) -- the display path warps
    # SOURCE pixels verbatim: the 20 Jul passthrough test measured our old
    # decode->bands re-colorize collapsing 5,956 source colors to 5.
    return _nc_palette_mm_vec(arr, np), arr


def _nowcast_health():
    """Health block read from DISK (any process can answer truthfully)."""
    try:
        with open(os.path.join(NC_DIR, "meta.json")) as fh:
            m = json.load(fh)
        att = m.get("last_attempt") or {}
        head = m.get("list_head") or {}
        return {"last_computed": m.get("computed_at"),
                "age_s": (int(time.time()) - m["computed_at"]) if m.get("computed_at") else None,
                "ok": att.get("ok"), "error": att.get("error"),
                "compute_s": m.get("compute_s"),
                "base_frame_time": m.get("base_frame_time"),
                "base_frame_path": m.get("base_frame_path"),
                "base_superseded": bool(head.get("path") and m.get("base_frame_path")
                                        and head["path"] != m["base_frame_path"]),
                "invariant": m.get("invariant")}
    except Exception:
        return {"last_computed": None, "ok": None, "error": "never_computed",
                "compute_s": None}


def _nc_write_meta(update):
    """Merge-update meta.json atomically (keeps last-good frames on failure)."""
    os.makedirs(NC_DIR, exist_ok=True)
    path = os.path.join(NC_DIR, "meta.json")
    try:
        with open(path) as fh:
            m = json.load(fh)
    except Exception:
        m = {}
    m.update(update)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(m, fh)
    os.replace(tmp, path)


def _nowcast_cycle():
    """Compute + persist the extrapolation. Owner-side, after the full cycle.
    NEVER raises: any failure just marks the nowcast unavailable."""
    t0 = time.time()
    base = None
    try:
        import numpy as np       # lazy: a missing/broken wheel must not
        import cv2               # touch boot or the logging cycle
        meta = _fetch_rv_meta()
        host = meta.get("host", "https://tilecache.rainviewer.com")
        past = (meta.get("radar") or {}).get("past") or []
        if len(past) < 3:
            raise RuntimeError("lt3_frames")
        frames = past[-3:]
        base = frames[-1]
        # record the CURRENT list head every cycle (before any heavy work):
        # the serve path compares it against the published base so a nowcast
        # stuck on a superseded frame is withheld instead of shown (24-jul
        # phantom-pixel report: stale base advected over a clean morning).
        _nc_write_meta({"list_head": {"time": base["time"], "path": base["path"],
                                      "seen_at": int(time.time())}})
        if (_NC_LAST.get("ok") and _NC_LAST.get("base_time") == base["time"]
                and _NC_LAST.get("base_path") == base["path"]):
            # same radar frame (time AND path: RainViewer re-QCs frames and
            # republishes the SAME timestamp under a NEW path — e.g. morning
            # AP/clutter removal; time-only equality kept advecting the dirty
            # cached grid while the observed layer showed the clean re-issue)
            # -> +5..+30 unchanged, but the AGE-CORRECTED "now" frame must
            # track wall clock: re-advect from cached flow/base so its
            # correction never goes stale between frames.
            _nowcast_now_frame()
            return
        if (frames[2]["time"] - frames[1]["time"] > NC_MAX_FRAME_GAP_S
                or frames[1]["time"] - frames[0]["time"] > NC_MAX_FRAME_GAP_S):
            raise RuntimeError("frames_gappy")
        grids = []
        for fr in frames:
            ent = _NC_CACHE["grids"].get(fr["path"])
            if ent is None:
                g = _nc_fetch_grid(host, fr["path"], np)
                if g is None:
                    raise RuntimeError("tile_fetch_failed")
                ent = (fr["time"], g[0], g[1])
                _NC_CACHE["grids"][fr["path"]] = ent
            grids.append(ent)
        keep = {f["path"] for f in past}
        for k in list(_NC_CACHE["grids"]):
            if k not in keep:
                del _NC_CACHE["grids"][k]

        (t1, g1, _r1), (t2, g2, _r2), (t3, g3, r3) = grids

        def prep(g):
            return (np.log1p(g) / np.log1p(20.0) * 255.0).clip(0, 255).astype(np.uint8)

        fb = dict(pyr_scale=0.5, levels=4, winsize=25, iterations=3,
                  poly_n=7, poly_sigma=1.5, flags=0)
        flow = (cv2.calcOpticalFlowFarneback(prep(g1), prep(g2), None, **fb)
                + cv2.calcOpticalFlowFarneback(prep(g2), prep(g3), None, **fb)) * 0.5
        step = 300.0 / max(1.0, float(t3 - t2))       # px per 5-min step
        n = g3.shape[0]                               # native canvas resolution
        km_px = NC_HALF_KM * 2 / n
        xs, ys = np.meshgrid(np.arange(n, dtype=np.float32),
                             np.arange(n, dtype=np.float32))
        mapx = (xs - flow[..., 0] * step).astype(np.float32)
        mapy = (ys - flow[..., 1] * step).astype(np.float32)
        wet = g3 >= 0.5
        spd = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
        mean_kmh = (float(np.mean(spd[wet])) * km_px / ((t3 - t2) / 3600.0)
                    if wet.any() else 0.0)

        from PIL import Image
        os.makedirs(NC_DIR, exist_ok=True)
        # display path warps the SOURCE RGBA verbatim -- full RainViewer
        # palette (5,956 colors / 152 alpha levels measured vs the 5 our old
        # re-colorize kept). NEAREST, not LINEAR: bilinear re-averages every
        # step, compounding over 6 steps (measured 20 Jul: 5-9 mm/h band
        # 211px -> linear 91 / nearest 167); nearest also cannot invent colors.
        # render ALL leads first, assert the invariant on each, publish only
        # then — a violation must never leave a half-published frame set
        base_classes = _nc_class_set(r3, np)
        cur, rendered = r3, []
        for lead in NC_LEADS:
            cur = cv2.remap(cur, mapx, mapy, cv2.INTER_NEAREST,
                            borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            _nc_assert_subset(cur, base_classes, np, "step+%02d" % lead)
            rendered.append((lead, cur))
        for lead, arr in rendered:
            p = os.path.join(NC_DIR, "step_%02d.png" % lead)
            Image.fromarray(arr).save(p + ".tmp", format="PNG")
            os.replace(p + ".tmp", p)

        # cache base RGBA + flow so the "now" frame can re-advect between
        # radar frames (arbitrary-dt generalization of the fixed 5-min step)
        _NC_CACHE["now_src"] = (base["time"], base["path"], r3, flow, float(t3 - t2))

        now = int(time.time())
        compute_s = round(time.time() - t0, 2)
        _nc_write_meta({
            "computed_at": now, "base_frame_time": base["time"],
            "base_frame_path": base["path"],
            "invariant": {"ok": True, "ts": now, "checked_leads": list(NC_LEADS)},
            "steps": list(NC_LEADS), "compute_s": compute_s,
            "mean_motion_kmh": round(mean_kmh, 1),
            "grid": {"half_km": NC_HALF_KM, "km_px": round(km_px, 3), "n": n,
                     "render_px": n, "palette": "source"},
            "bounds": {"min_lat": round(LAT - NC_HALF_KM / KM_LAT, 5),
                       "max_lat": round(LAT + NC_HALF_KM / KM_LAT, 5),
                       "min_lon": round(LON - NC_HALF_KM / KM_LON, 5),
                       "max_lon": round(LON + NC_HALF_KM / KM_LON, 5)},
            "last_attempt": {"ts": now, "ok": True, "error": None},
        })
        _NC_LAST.update({"computed_at": now, "base_time": base["time"],
                         "base_path": base["path"],
                         "compute_s": compute_s, "ok": True, "error": None})
        _nowcast_now_frame()             # own guard; +5..+30 already published
    except Exception as exc:
        _NC_LAST.update({"ok": False, "error": repr(exc)})
        try:
            _nc_write_meta({"last_attempt": {"ts": int(time.time()), "ok": False,
                                             "error": repr(exc)}})
            if isinstance(exc, RuntimeError) and "invariant_violation" in repr(exc):
                _nc_write_meta({"invariant": {"ok": False, "ts": int(time.time()),
                                              "detail": repr(exc)}})
            # fail-VISIBLE, not fail-stale: if this failed attempt was for a
            # NEWER frame than the published base, the published AHORA is now
            # advecting superseded content — withhold it (home falls back to
            # the raw latest observed frame) instead of serving old echo.
            if (base is not None and _NC_LAST.get("base_path")
                    and base.get("path") != _NC_LAST.get("base_path")):
                try:
                    os.remove(os.path.join(NC_DIR, "step_00.png"))
                except OSError:
                    pass
                _nc_write_meta({"now": None, "now_error": "stale_base_withheld"})
        except Exception:
            pass


def _nowcast_now_frame():
    """Age-corrected 'now': advect the latest observed frame forward by its
    CURRENT frame age (arbitrary dt, one warp). Runs every cycle so the
    correction tracks wall clock between radar frames. OWN fail-safe: any
    error -> no now-frame served (meta['now'] cleared), +5..+30 and the
    cycle unharmed."""
    t0 = time.time()
    try:
        import numpy as np
        import cv2
        from PIL import Image
        src = _NC_CACHE.get("now_src")
        if not src:
            raise RuntimeError("no_cached_flow")
        base_time, base_path, r3, flow, dt_frame = src
        dt_now = time.time() - base_time
        if not (0 < dt_now <= 30 * 60):
            raise RuntimeError("age_out_of_range:%.0fs" % dt_now)
        scale = dt_now / max(1.0, dt_frame)
        n = r3.shape[0]
        xs, ys = np.meshgrid(np.arange(n, dtype=np.float32),
                             np.arange(n, dtype=np.float32))
        mapx = (xs - flow[..., 0] * scale).astype(np.float32)
        mapy = (ys - flow[..., 1] * scale).astype(np.float32)
        est = cv2.remap(r3, mapx, mapy, cv2.INTER_NEAREST,
                        borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        _nc_assert_subset(est, _nc_class_set(r3, np), np, "step_00")
        os.makedirs(NC_DIR, exist_ok=True)
        p = os.path.join(NC_DIR, "step_00.png")
        Image.fromarray(est).save(p + ".tmp", format="PNG")
        os.replace(p + ".tmp", p)
        _nc_write_meta({"now": {"computed_at": int(time.time()),
                                "base_frame_time": int(base_time),
                                "base_frame_path": base_path,
                                "age_corrected_by_min": round(dt_now / 60.0, 1),
                                "compute_s": round(time.time() - t0, 3)}})
    except Exception as exc:
        try:
            # remove the file too: frame/0 serves whatever exists on disk, so
            # clearing only the meta left an orphan stale AHORA behind
            try:
                os.remove(os.path.join(NC_DIR, "step_00.png"))
            except OSError:
                pass
            _nc_write_meta({"now": None, "now_error": repr(exc)})
        except Exception:
            pass


@app.route("/api/nowcast")
def api_nowcast():
    try:
        with open(os.path.join(NC_DIR, "meta.json")) as fh:
            m = json.load(fh)
    except Exception:
        return jsonify({"available": False, "reason": "not_computed"})
    fresh = bool(m.get("computed_at")
                 and time.time() - m["computed_at"] <= NC_FRESH_S)
    # 24-jul stale-base gate: the frame list head (recorded every cycle) is
    # the truth about "latest observed". A published base that head has
    # superseded must not serve as AHORA; a long-superseded (or same-time
    # re-QC-replaced) base withholds the whole nowcast.
    head = m.get("list_head") or {}
    base_superseded = bool(head.get("path") and m.get("base_frame_path")
                           and head["path"] != m["base_frame_path"])
    stale_base = bool(head.get("time") and m.get("base_frame_time") and (
        head["time"] - m["base_frame_time"] > NC_STALE_BASE_S
        or (head["time"] == m["base_frame_time"] and base_superseded)))
    now_block = None if base_superseded else m.get("now")
    available = fresh and not stale_base
    reason = None if available else ("stale_base" if (fresh and stale_base) else "stale")
    return jsonify({**m, "now": now_block, "available": available,
                    "reason": reason, "base_superseded": base_superseded,
                    "frames": [{"lead_min": l,
                                "time": (m.get("base_frame_time") or 0) + l * 60,
                                "url": "/api/nowcast/frame/%d" % l}
                               for l in (m.get("steps") or [])]})


@app.route("/api/nowcast/frame/<int:lead>")
def api_nowcast_frame(lead):
    if lead != 0 and lead not in NC_LEADS:   # 0 = the age-corrected now frame
        return ("bad lead", 404)
    name = "step_%02d.png" % lead
    if not os.path.exists(os.path.join(NC_DIR, name)):
        return ("not computed", 404)
    return send_from_directory(NC_DIR, name, mimetype="image/png")


# ─────────────────────────────────────────────────────────────────────────
# GOES-19 ABI ingest (item 17 build; v2 training-data runway. LOGGING ONLY:
# fail-safe wrapped, add-only fields, nothing user-visible.)
# Product per the scoping memo: ABI-L2-CMIPC band 13 (clean IR window, BT in
# Kelvin), GOES-East `noaa-goes19` S3, anonymous HTTPS, ~5-min CONUS cadence,
# granule ~3.9 MB. Extraction: ±40 km box around the station via the fixed-
# grid forward projection computed from the file's own projection constants.
# FIELD SEMANTICS (do not invert): ctt_cool_15m_k = ctt_min(now) - ctt_min
# (~15 min ago) -> NEGATIVE = cooling = cloud-top growth.
# Cadence respect: the hour-prefix listing is checked each full cycle (cheap
# XML), the granule is downloaded only when a NEW key appears; same-granule
# cycles reuse the cached stats. Measured (18 Jul local): list 0.7 s + fetch
# 1.3 s + parse 0.01 s = 2.0 s -> runs IN-CYCLE (alongside nowcast's ~6.5 s,
# far inside the 3-min active budget); decoupling to its own thread was
# considered and skipped at these numbers.
# cool-ring persistence: /data/goes_ring.json (atomic) -> the 15-min delta
# SURVIVES restarts instead of nulling after every deploy.
GOES_BUCKET = "https://noaa-goes19.s3.amazonaws.com"
GOES_PRODUCT = "ABI-L2-CMIPC"
GOES_BAND_PREFIX = "OR_ABI-L2-CMIPC-M6C13_G19"
GOES_BOX_KM = 40.0
GOES_RING_FILE = os.path.join(DATA_DIR, "goes_ring.json")
GOES_STATUS_FILE = os.path.join(DATA_DIR, "goes_status.json")
_GOES_STATUS = {"last_granule_time": None, "last_fetch_ok": None,
                "fetch_seconds": None, "consecutive_failures": 0}
_GOES_CACHE = {"key": None, "granule_time": None, "mean": None, "min": None}
_GOES_RING = None   # [[granule_time, ctt_min_k], ...] newest last


def _goes_ring_load():
    global _GOES_RING
    if _GOES_RING is None:
        try:
            with open(GOES_RING_FILE) as fh:
                _GOES_RING = json.load(fh)
        except Exception:
            _GOES_RING = []
    return _GOES_RING


def _goes_ring_add(gt, ctt_min):
    ring = _goes_ring_load()
    if not any(e[0] == gt for e in ring):
        ring.append([gt, ctt_min])
    ring.sort()
    del ring[:-14]                       # ~70 min of 5-min granules
    try:
        with open(GOES_RING_FILE + ".tmp", "w") as fh:
            json.dump(ring, fh)
        os.replace(GOES_RING_FILE + ".tmp", GOES_RING_FILE)
    except Exception:
        pass


def _goes_cool_15m(gt, ctt_min):
    """ctt_min(now) - ctt_min(~15 min ago); nearest ring entry to t-900 within
    +/-240 s, else None (accepted null while the ring warms)."""
    best, bd = None, 241
    for t, v in _goes_ring_load():
        d = abs((gt - 900) - t)
        if d < bd:
            best, bd = v, d
    return round(ctt_min - best, 1) if best is not None else None


def _goes_latest_key(now_utc):
    import re as _re
    for back in (0, 3600):
        t = time.gmtime(now_utc - back)
        prefix = "%s/%04d/%03d/%02d/%s" % (GOES_PRODUCT, t.tm_year, t.tm_yday,
                                           t.tm_hour, GOES_BAND_PREFIX)
        url = GOES_BUCKET + "/?list-type=2&prefix=" + urllib.parse.quote(prefix)
        req = urllib.request.Request(url, headers={"User-Agent": "virreyes-weather/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml = resp.read().decode()
        keys = _re.findall(r"<Key>([^<]+)</Key>", xml)
        if keys:
            return sorted(keys)[-1]
    return None


def _goes_fetch_stats(key):
    """Download one granule, slice the +/-40 km box, return (gt, mean, min)."""
    import re as _re
    import numpy as np
    import h5py, io as _io, calendar
    m = _re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", key)
    st = time.strptime("%s-%s %s:%s:%s" % m.groups(), "%Y-%j %H:%M:%S")
    gt = calendar.timegm(st)
    req_ = urllib.request.Request(GOES_BUCKET + "/" + key,
                                  headers={"User-Agent": "virreyes-weather/1.0"})
    with urllib.request.urlopen(req_, timeout=30) as resp:
        data = resp.read()
    f = h5py.File(_io.BytesIO(data), "r")
    proj = f["goes_imager_projection"]
    A1 = lambda a: float(np.atleast_1d(a)[0])
    lam0 = math.radians(A1(proj.attrs["longitude_of_projection_origin"]))
    req_ax = A1(proj.attrs["semi_major_axis"]); rpol = A1(proj.attrs["semi_minor_axis"])
    Hsat = A1(proj.attrs["perspective_point_height"]) + req_ax

    def fwd(lat_d, lon_d):
        lat = math.radians(lat_d); lon = math.radians(lon_d)
        e2 = (req_ax**2 - rpol**2) / req_ax**2
        phic = math.atan((rpol**2 / req_ax**2) * math.tan(lat))
        rc = rpol / math.sqrt(1 - e2 * math.cos(phic)**2)
        sx = Hsat - rc * math.cos(phic) * math.cos(lon - lam0)
        sy = -rc * math.cos(phic) * math.sin(lon - lam0)
        sz = rc * math.sin(phic)
        return (math.asin(-sy / math.sqrt(sx*sx + sy*sy + sz*sz)),
                math.atan2(sz, sx))

    xv, yv = f["x"], f["y"]
    xsf, xao = A1(xv.attrs["scale_factor"]), A1(xv.attrs["add_offset"])
    ysf, yao = A1(yv.attrs["scale_factor"]), A1(yv.attrs["add_offset"])
    dlat = GOES_BOX_KM / KM_LAT; dlon = GOES_BOX_KM / KM_LON
    corners = [fwd(LAT + a, LON + b) for a in (-dlat, dlat) for b in (-dlon, dlon)]
    ixs = [int(round((c[0] - xao) / xsf)) for c in corners]
    iys = [int(round((c[1] - yao) / ysf)) for c in corners]
    cmi = f["CMI"]
    sf = A1(cmi.attrs["scale_factor"]); ao = A1(cmi.attrs["add_offset"])
    fill = int(A1(cmi.attrs["_FillValue"]))
    raw = cmi[min(iys):max(iys)+1, min(ixs):max(ixs)+1]
    bt = raw.astype(np.float64) * sf + ao
    bt[raw == fill] = np.nan
    if not np.isfinite(bt).any():
        raise RuntimeError("goes_box_all_fill")
    return gt, round(float(np.nanmean(bt)), 1), round(float(np.nanmin(bt)), 1)


def _goes_status_save():
    """Persist fetch status to DATA_DIR (atomic, like _nc_write_meta) so any
    process — not just the lease owner — can answer /api/health truthfully."""
    try:
        with open(GOES_STATUS_FILE + ".tmp", "w") as fh:
            json.dump(dict(_GOES_STATUS, saved_at=int(time.time())), fh)
        os.replace(GOES_STATUS_FILE + ".tmp", GOES_STATUS_FILE)
    except Exception:
        pass


def _goes_health():
    """Health block read from DISK (any process can answer truthfully)."""
    try:
        with open(GOES_STATUS_FILE) as fh:
            return json.load(fh)
    except Exception:
        return dict(_GOES_STATUS)


def _goes_block(now_ts):
    """Assemble the add-only snapshot block. Never raises past its guard:
    (block, None) or (None, reason). The cycle's uptime outranks this."""
    try:
        t0 = time.time()
        key = _goes_latest_key(now_ts)
        if key is None:
            raise RuntimeError("no_granule_listed")
        if key != _GOES_CACHE["key"]:
            gt, bt_mean, bt_min = _goes_fetch_stats(key)
            _GOES_CACHE.update({"key": key, "granule_time": gt,
                                "mean": bt_mean, "min": bt_min})
            _goes_ring_add(gt, bt_min)
            _GOES_STATUS["fetch_seconds"] = round(time.time() - t0, 2)
        gt = _GOES_CACHE["granule_time"]
        _GOES_STATUS["last_granule_time"] = gt
        _GOES_STATUS["last_fetch_ok"] = True
        _GOES_STATUS["consecutive_failures"] = 0
        _goes_status_save()
        return {"ctt_mean_k": _GOES_CACHE["mean"],
                "ctt_min_k": _GOES_CACHE["min"],
                "ctt_cool_15m_k": _goes_cool_15m(gt, _GOES_CACHE["min"]),
                "granule_time": gt,
                "age_min": round((now_ts - gt) / 60.0, 1)}, None
    except Exception as exc:
        _GOES_STATUS["last_fetch_ok"] = False
        _GOES_STATUS["consecutive_failures"] = _GOES_STATUS.get("consecutive_failures", 0) + 1
        _goes_status_save()
        return None, "goes_error:%r" % (exc,)


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


# ── in-situ v1 classifier — SHADOW MODE ─────────────────────────────────
# Log-only live scoring of the learned in-situ onset classifier. The model file
# lives on /data (NOT in git); this code carries only its SHA-256, so the public
# install route below can only ever install the exact file we trained. Shadow
# mode: the probability goes into the autolog snapshot and /api/health — it must
# NEVER touch the UI, the ETA heuristic, or crash the autologger.
INSITU_MODEL_FILE = os.path.join(DATA_DIR, "insitu_v1_model.json")
INSITU_V1_SHA256 = "97f246044ada81b7ab9c817eca71967e8e6514ddbb3fd0c62694395463128bd0"   # v1.1 (full-diurnal negatives)
_INSITU = {"model": None, "reason": "not_loaded"}


def _insitu_canonical_sha(obj):
    import hashlib as _h
    return _h.sha256(json.dumps(obj, sort_keys=True,
                                separators=(",", ":")).encode("utf-8")).hexdigest()


def _insitu_load():
    try:
        with open(INSITU_MODEL_FILE) as fh:
            m = json.load(fh)
        if _insitu_canonical_sha(m) != INSITU_V1_SHA256:
            _INSITU.update({"model": None, "reason": "sha_mismatch"})
            return False
        n = len(m["features"])
        assert n == len(m["weights_standardized"]) == len(m["scaler_mean"]) == len(m["scaler_std"])
        _INSITU.update({"model": m, "reason": None})
        return True
    except FileNotFoundError:
        _INSITU.update({"model": None, "reason": "model_file_missing"})
        return False
    except Exception as exc:
        _INSITU.update({"model": None, "reason": "load_error:%r" % (exc,)})
        return False


@app.route("/api/insitu/model", methods=["POST"])
def insitu_model_upload():
    """Install the v1 model to /data. The body's canonical SHA-256 must equal the
    hash pinned in code, so this route can only install the trained artifact —
    it cannot be used to inject anything else."""
    try:
        body = request.get_json(force=True)
        if _insitu_canonical_sha(body) != INSITU_V1_SHA256:
            return jsonify({"ok": False, "error": "sha_mismatch"}), 400
        tmp = INSITU_MODEL_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(body, fh, sort_keys=True, separators=(",", ":"))
        os.replace(tmp, INSITU_MODEL_FILE)
        ok = _insitu_load()
        return jsonify({"ok": ok, "loaded": _INSITU["model"] is not None,
                        "reason": _INSITU["reason"]})
    except Exception as exc:
        return jsonify({"ok": False, "error": repr(exc)}), 500


def _insitu_score(now_ts, cape, rh, temp_c, dew_c, steer_profile, steer, p15):
    """Fail-safe shadow scorer -> (prob, reason). Feature ORDER and scaling are
    exactly the training pipeline's: [CAPE, RH, T-dew, steer_spd(700 hPa profile,
    legacy fallback), sin_hour, cos_hour(CDMX), press_trend_15m(median-imputed)].
    Any missing feature / error -> (None, reason); never raises."""
    m = _INSITU["model"]
    if not m and _INSITU.get("reason") in ("model_file_missing", "sha_mismatch"):
        _insitu_load()          # lazy retry: /data may have been installed after
        m = _INSITU["model"]    # this process booted (multi-process safe)
    if not m:
        return None, (_INSITU["reason"] or "not_loaded")
    try:
        spd = None
        if steer_profile and steer_profile.get("700"):
            spd = steer_profile["700"].get("spd")
        if spd is None and isinstance(steer, dict):
            spd = steer.get("spd")
        if cape is None or rh is None or temp_c is None or dew_c is None or spd is None:
            return None, "feature_null"
        import datetime as _dt
        tz = _dt.timezone(_dt.timedelta(hours=-6))
        d = _dt.datetime.fromtimestamp(int(now_ts), tz)
        h = d.hour + d.minute / 60.0
        x = [float(cape), float(rh), float(temp_c) - float(dew_c), float(spd),
             math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24),
             (float(p15) if p15 is not None else float(m["impute_median_p15"]))]
        z = float(m["intercept"])
        for xi, mu, sd, w in zip(x, m["scaler_mean"], m["scaler_std"],
                                 m["weights_standardized"]):
            z += w * ((xi - mu) / (sd if sd else 1.0))
        z = max(-30.0, min(30.0, z))
        return round(1.0 / (1.0 + math.exp(-z)), 4), None
    except Exception as exc:
        return None, "score_error:%r" % (exc,)


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
_CELL_HIST = []   # item 2: [(rv_time, slim_cells)] last few RV frames (oldest->newest), cap 3


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


def _annotate_cell_history(cells, rv_time):
    """item 2: attach per-cell motion history -- velocity, acceleration, and
    growth/decay rate -- by chaining nearest-cell matches back through the last
    few RV frames, and write them onto each cell so they land in the snapshot.
    Training data only: the server ETA still uses 700 hPa steering, so there is
    no train/serve parity concern. Keyed on RV FRAME time, so repeated autolog
    cycles on the same (~10 min) radar frame don't fabricate zero-motion samples.
    Cold start fills in gradually: 1 prior frame -> velocity+growth, 2 -> +accel."""
    global _CELL_HIST
    if rv_time is None:
        return
    hist = [(t, cs) for (t, cs) in _CELL_HIST if 0 < (rv_time - t) <= HS_GAP]
    for c in cells:
        track, ref = [], c
        for (t, cs) in reversed(hist):          # most recent prior frame first
            best, bd = None, 1e9
            for q in cs:
                d = math.hypot(ref["x"] - q["x"], ref["y"] - q["y"])
                if d < bd:
                    bd, best = d, q
            if best is None or bd > HS_MATCH_KM:
                break
            track.append((t, best))
            ref = best
        c["track_n"] = len(track)
        if track:
            t1, p1 = track[0]
            dt1_h = (rv_time - t1) / 3600.0
            if dt1_h > 0:
                vx, vy = (c["x"] - p1["x"]) / dt1_h, (c["y"] - p1["y"]) / dt1_h
                c["vx"], c["vy"] = round(vx, 1), round(vy, 1)
                c["speed"] = round(math.hypot(vx, vy), 1)
                c["toward"] = round((math.degrees(math.atan2(vx, vy)) + 360) % 360)
                c["growth_mm_min"] = round((c["mm"] - p1["mm"]) / ((rv_time - t1) / 60.0), 2)
                c["d_dbz"] = c["dbz"] - p1["dbz"]
                if len(track) >= 2:
                    t2, p2 = track[1]
                    dt2_h = (t1 - t2) / 3600.0
                    span_h = ((rv_time - t2) / 2) / 3600.0
                    if dt2_h > 0 and span_h > 0:
                        pvx, pvy = (p1["x"] - p2["x"]) / dt2_h, (p1["y"] - p2["y"]) / dt2_h
                        c["accel_kmh_h"] = round(math.hypot(vx - pvx, vy - pvy) / span_h, 1)
    # advance the buffer only when the RV frame actually changed
    if not _CELL_HIST or _CELL_HIST[-1][0] != rv_time:
        _CELL_HIST.append((rv_time, [{"x": c["x"], "y": c["y"], "mm": c["mm"], "dbz": c["dbz"]} for c in cells]))
        _CELL_HIST = _CELL_HIST[-3:]


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


def _closest_approach(snaps, t0, t1):
    """item 3: closest a radar cell got to the station within [t0, t1] -> (min_km,
    ts), or (None, None) if no cells were logged in the window. Lets a miss be
    split into a near-miss (a cell came close but no rain arrived) vs a far-/no-cell
    miss. Analytical, from the existing event-log cell distances."""
    best_km, best_ts = None, None
    for sn in snaps:
        ts = sn.get("t")
        if ts is None or ts < t0 or ts > t1:
            continue
        for c in (sn.get("cells") or []):
            d = c.get("dist")
            if d is not None and (best_km is None or d < best_km):
                best_km, best_ts = d, ts
    return best_km, best_ts


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
    keep, resolved, snaps = [], [], None
    for p in pend:
        pt = p["t"]
        # did rain start after the prediction time? (item 11: trace is now 1-min
        # granularity -> onset timestamps gain 1-min resolution, and a single
        # bucket-tip must not resolve a hit: confirm like _confirmed_wet)
        onset = None
        trace = [x for x in _RAIN_TRACE if x[0] >= pt]
        for i, (ts, rr) in enumerate(trace):
            if rr >= 0.2 and _confirmed_wet(trace, i):
                onset = ts
                break
        if onset is not None:
            outcome, actual_min, t1 = "hit", round((onset - pt) / 60), onset
        elif now - pt >= 120 * 60:
            outcome, actual_min, t1 = "miss", None, now
        else:
            keep.append(p)
            continue
        # item 3: closest radar-cell approach within the window (load the event
        # log lazily, only when something actually resolves).
        if snaps is None:
            snaps = _load_event_snapshots()
        closest_km, closest_ts = _closest_approach(snaps, pt, t1)
        r = {**p, "outcome": outcome, "actual_min": actual_min, "resolved_t": now,
             "closest_km": closest_km, "closest_ts": closest_ts}
        if outcome == "miss":
            # a cell came within the 10 km intercept radius but no rain arrived
            r["near_miss"] = closest_km is not None and closest_km <= 10
        resolved.append(r)
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


def _auto_log_once(davis=None):
    try:
        # Davis ground-truth: normally handed in by the 60-s poller so the poll
        # cadence is the single WeatherLink consumer; direct fetch is the
        # fallback (e.g. first cycle if the poll was skipped).
        if davis is None:
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
        rv_field = None
        try:
            meta = _fetch_rv_meta()
            host = meta.get("host", "https://tilecache.rainviewer.com")
            past = (meta.get("radar") or {}).get("past") or []
            if past:
                fr = past[-1]
                rv_time = fr["time"]
                field = _fetch_rv_field(host, fr["path"])
                rv_field = field
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
        _push_check_t1(now_ts, rain_rate or 0)   # item 25: BEFORE append
        _RAIN_TRACE.append((now_ts, rain_rate or 0))
        cutoff = now_ts - 3 * 3600
        while _RAIN_TRACE and _RAIN_TRACE[0][0] < cutoff:
            _RAIN_TRACE.pop(0)
        _save_rain_trace()          # persist so a restart/deploy can't blank the trace

        _annotate_cell_history(cells, rv_time)   # item 2: per-cell velocity/accel/growth

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

        # item 4b Phase 1: WH57 interference flags (training-data integrity).
        # Evaluate on a recent strike only. Stuck-distance comes from the fetch; the
        # environment check (a strike with no radar cell within R_echo AND dry low
        # levels) uses the cells + RH already in hand. corrob_status stays
        # 'unavailable' until a server-side Blitz source exists (Phase 2), and
        # 'unavailable' never implies interference. FLAG, never delete.
        _li_reasons = []
        if _lt.get("interference_stuck"):
            _li_reasons.append("stuck_distance")
        if _lt.get("corrob_status") == "uncorroborated":
            _li_reasons.append("no_blitz_corroboration")
        if _lt.get("age_min") is not None and _lt["age_min"] <= 30:
            _near_cell = any((c.get("dist") or 1e9) <= WH57_R_ECHO_KM for c in cells)
            _rh = davis.get("humidity_pct")
            if (not _near_cell) and (_rh is not None and _rh < WH57_RH_DRY_PCT):
                _li_reasons.append("no_echo_dry_env")

        # SHADOW MODE: log-only in-situ v1 probability. Fail-safe by construction
        # (never raises); UI and the ETA heuristic are untouched.
        _ins_prob, _ins_reason = _insitu_score(now_ts, cape, davis.get("humidity_pct"),
                                               davis.get("temperature_c"), davis.get("dew_point_c"),
                                               steer_profile, steer, _pt15)

        # sub-threshold echo block (v2 runway): add-only, fail-safe like the scorer.
        try:
            _sub, _sub_reason = _subthresh_block(rv_field, rv_time)
        except Exception as exc:
            _sub, _sub_reason = None, "subthresh_error:%r" % (exc,)

        # GOES-19 cloud-top block (item 17, v2 runway): add-only, fail-safe.
        try:
            _goes, _goes_reason = _goes_block(now_ts)
        except Exception as exc:
            _goes, _goes_reason = None, "goes_error:%r" % (exc,)

        # item 24 (blend6h) STAGE-0 instrumentation: log the Open-Meteo 6-hour
        # CLAIM as it stood this cycle. The 26-jul audit found the season log
        # carries NO per-cycle hourly forecasts, so the pre-registered per-lead
        # replay (the blend's ship gate) cannot run until this accrues.
        # Add-only, fail-safe: reason instead of hours, never raises.
        try:
            _fxd = _fx_get()
            if _fxd:
                _fxh = _fxd.get("hourly") or {}
                _fx_rows = []
                import calendar as _cal
                for _i, _ts in enumerate(_fxh.get("time") or []):
                    # hourly.time is America/Mexico_City local (UTC-6, no DST)
                    _tu = _cal.timegm(time.strptime(_ts, "%Y-%m-%dT%H:%M")) + 6 * 3600
                    if _tu <= now_ts:
                        continue
                    _fx_rows.append({"t": _tu,
                                     "prob": (_fxh.get("precipitation_probability") or [None]*(_i+1))[_i],
                                     "mm": (_fxh.get("precipitation") or [None]*(_i+1))[_i]})
                    if len(_fx_rows) >= 6:
                        break
                _fx6h = {"age_s": int(time.time() - _FX_CACHE["t"]), "hours": _fx_rows}
            else:
                _fx6h = {"reason": "no_forecast_data"}
        except Exception as _fx_exc:
            _fx6h = {"reason": "fx6h_error:%r" % (_fx_exc,)}

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
            # item 4b: interference / corroboration gate (Phase 1; Phase 2 pending)
            "lightning_suspected_interference": bool(_li_reasons),
            "interference_reason": ("+".join(_li_reasons) if _li_reasons else None),
            "lightning_corrob_status": _lt.get("corrob_status"),
            "lightning_corroborated": _lt.get("corroborated"),
            "lightning_corrob_n": _lt.get("corrob_n"),
            "lightning_corrob_min_km": _lt.get("corrob_min_km"),
            # in-situ v1 SHADOW scoring (log-only; None + reason on any failure)
            "insitu_prob_v1": _ins_prob,
            "insitu_model_v": ((_INSITU.get("model") or {}).get("version") if _ins_prob is not None else None),
            "insitu_prob_reason": _ins_reason,
            # sub-threshold echo bands (v2 runway; add-only, null + reason on failure)
            "subthresh": _sub,
            "subthresh_reason": _sub_reason,
            # GOES-19 band-13 cloud tops (item 17; negative cool_15m = growth)
            "goes": _goes,
            "goes_reason": _goes_reason,
            # "+30 min" nowcast bookkeeping (B4, add-only). The compute runs
            # AFTER this row is written (cycle-safety), so these reflect the
            # nowcast that was CURRENT at row time -- exactly what later skill
            # scoring needs (was an extrapolation available, and from when).
            "nowcast_computed": bool(_NC_LAST.get("ok") and _NC_LAST.get("computed_at")
                                     and now_ts - _NC_LAST["computed_at"] <= NC_FRESH_S),
            "nowcast_compute_s": _NC_LAST.get("compute_s"),
            "nowcast_base_time": (_NC_LAST.get("base_time") if _NC_LAST.get("ok") else None),
            # item 24: the logged 6-hour Open-Meteo claim (blend6h replay baseline)
            "fx6h": _fx6h,
        }
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False)
        with open(EVENTLOG_FILE, "a") as fh:
            fh.write(line + "\n")
        _push_check_cycle(rec)   # item 25: T2/T3 from the just-written row

        # beta-gauge read-through (item 19): persist the just-scored values so
        # /api/current can serve them from any process. Fail-safe: a write
        # error can never touch the cycle.
        try:
            _bl = {"ts": now_ts,
                   "insitu_prob_v1": _ins_prob,
                   "insitu_reason": _ins_reason,
                   "model_v": (_INSITU.get("model") or {}).get("version"),
                   "threshold": (_INSITU.get("model") or {}).get("threshold"),
                   "subthresh": _sub, "subthresh_reason": _sub_reason}
            with open(BETA_LAST_FILE + ".tmp", "w") as fh:
                json.dump(_bl, fh)
            os.replace(BETA_LAST_FILE + ".tmp", BETA_LAST_FILE)
        except Exception:
            pass

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


_AUTOLOG_LOCK = os.path.join(DATA_DIR, "autolog.lock")

# ── single-writer lease (dual-writer fix) ────────────────────────────────
# Root cause of the dual writer: the previous guard was an IN-PROCESS flag (the
# on-disk lock was advisory and unconditionally re-claimed on boot, because
# container PIDs get reused). That assumed one process globally; with two
# processes sharing /data (replicas / deploy overlap), each claimed ownership.
# This lease is identity-based (random writer_id, no PID semantics), lives on
# /data, heartbeats via atomic content writes, and is claimed with a
# jitter-then-verify race breaker. Non-owners idle as hot standby and take over
# only after the lease goes STALE (owner died). Guard errors -> DON'T write
# (one writer or none — never two).
WRITER_ID = os.urandom(4).hex()
_LEASE_STALE_S = 15 * 60          # takeover only after this silence


def _lease_read():
    try:
        with open(_AUTOLOG_LOCK) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None    # legacy bare-PID lock -> None
    except Exception:
        return None                                   # missing/unreadable/legacy


def _lease_mtime():
    try:
        return os.path.getmtime(_AUTOLOG_LOCK)
    except OSError:
        return 0.0


def _lease_write():
    tmp = _AUTOLOG_LOCK + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"writer_id": WRITER_ID, "pid": os.getpid(), "ts": int(time.time())}, fh)
    os.replace(tmp, _AUTOLOG_LOCK)


def _lease_ensure():
    """True iff THIS process holds the lease after this call. Freshness is
    judged by file mtime (also covers legacy utime-heartbeat locks written by
    old code during a deploy overlap, so we never steal from a live old
    writer)."""
    d = _lease_read()
    holder = (d or {}).get("writer_id")
    fresh = (time.time() - _lease_mtime()) < _LEASE_STALE_S and _lease_mtime() > 0
    if holder == WRITER_ID:
        _lease_write()                    # refresh heartbeat
        return True
    if fresh:
        return False                      # live lease held by someone else
    # stale or missing -> claim, jitter, verify (loser of a race stands down)
    _lease_write()
    time.sleep(2 + (os.getpid() % 5))
    d2 = _lease_read()
    return (d2 or {}).get("writer_id") == WRITER_ID


# ─────────────────────────────────────────────────────────────────────────
# PUSH NOTIFICATIONS (item 25) — VERIFIED EVIDENCE ONLY (post-Rule-A classes):
#   T1 rain-onset: the debounced station onset on the 1-min lattice
#   T2 tracked-cell approach: server pred_eta_min <= 30 (>=30 dBZ by floor)
#   T3 corroborated lightning <= 25 km (corroborated only, NEVER raw WH57)
# Nothing else pushes; suspicion stays in-app. Copy states the MEASUREMENT,
# never a duration/forecast claim. Fail-safe doctrine: a push failure can
# never touch the cycle; every attempt logs to push_log.jsonl (/pushes).
# All webpush imports are LAZY (boot rule: module level must stay inert).
PUSH_SUBS_FILE = os.path.join(DATA_DIR, "push_subs.json")
PUSH_STATE_FILE = os.path.join(DATA_DIR, "push_state.json")
PUSH_LOG_FILE = os.path.join(DATA_DIR, "push_log.jsonl")
VAPID_PEM_FILE = os.path.join(DATA_DIR, "vapid_private.pem")
PUSH_COOLDOWN_S = {"t1": 1800, "t2": 3600, "t3": 3600}  # one storm = one push/tier
PUSH_T1_CONFIRM_S = 6 * 60   # mirrors ONSET_CONFIRM_S
PUSH_T1_DRY_S = 30 * 60      # mirrors the onset 30-min dry rule
_PUSH_STATE = {"loaded": False}


def _cdmx_hhmm(ts):
    return time.strftime("%H:%M", time.gmtime(ts - 6 * 3600))


def _push_state():
    if not _PUSH_STATE["loaded"]:
        try:
            with open(PUSH_STATE_FILE) as fh:
                _PUSH_STATE.update(json.load(fh))
        except Exception:
            pass
        _PUSH_STATE["loaded"] = True
    return _PUSH_STATE


def _push_state_save():
    try:
        d = {k: v for k, v in _PUSH_STATE.items() if k != "loaded"}
        with open(PUSH_STATE_FILE + ".tmp", "w") as fh:
            json.dump(d, fh)
        os.replace(PUSH_STATE_FILE + ".tmp", PUSH_STATE_FILE)
    except Exception:
        pass


def _push_subs_load():
    try:
        with open(PUSH_SUBS_FILE) as fh:
            return json.load(fh)
    except Exception:
        return []


def _push_subs_save(subs):
    try:
        with open(PUSH_SUBS_FILE + ".tmp", "w") as fh:
            json.dump(subs[-50:], fh)
        os.replace(PUSH_SUBS_FILE + ".tmp", PUSH_SUBS_FILE)
    except Exception:
        pass


def _push_log(entry):
    try:
        with open(PUSH_LOG_FILE, "a") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _vapid_ensure():
    """Load-or-create the VAPID keypair on /data (single-owner app: the key
    lives with the rest of the state, never in the repo)."""
    from py_vapid import Vapid
    if not os.path.exists(VAPID_PEM_FILE):
        v = Vapid()
        v.generate_keys()
        v.save_key(VAPID_PEM_FILE)
    return Vapid.from_file(VAPID_PEM_FILE)


def _vapid_public_b64():
    import base64
    from cryptography.hazmat.primitives import serialization
    v = _vapid_ensure()
    raw = v.public_key.public_bytes(serialization.Encoding.X962,
                                    serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _push_send_all(tier, title, body):
    """Deliver to every subscription in a daemon thread — the cycle never
    waits on push services. Dead endpoints (404/410) are pruned."""
    def run():
        try:
            from pywebpush import webpush, WebPushException
            subs = _push_subs_load()
            keep, sent, fail = [], 0, 0
            for s in subs:
                try:
                    webpush(subscription_info=s,
                            data=json.dumps({"title": title, "body": body, "tag": tier}),
                            vapid_private_key=VAPID_PEM_FILE,
                            vapid_claims={"sub": "mailto:urreakriisa@gmail.com"},
                            timeout=10)
                    keep.append(s)
                    sent += 1
                except WebPushException as exc:
                    code = getattr(getattr(exc, "response", None), "status_code", None)
                    fail += 1
                    if code not in (404, 410):     # 404/410 = gone -> prune
                        keep.append(s)
                except Exception:
                    keep.append(s)
                    fail += 1
            if len(keep) != len(subs):
                _push_subs_save(keep)
            _push_log({"ts": int(time.time()), "tier": tier, "title": title,
                       "body": body, "sent": sent, "failed": fail, "subs": len(subs)})
        except Exception as exc:
            _push_log({"ts": int(time.time()), "tier": tier, "error": repr(exc)})
    threading.Thread(target=run, daemon=True).start()


def _push_fire(tier, title, body):
    """Cooldown-gated send. State persists to /data so a restart mid-storm
    cannot re-fire the same event."""
    st = _push_state()
    now = time.time()
    if now - (st.get(tier + "_last_push") or 0) < PUSH_COOLDOWN_S[tier]:
        return False
    st[tier + "_last_push"] = int(now)
    _push_state_save()
    _push_send_all(tier, title, body)
    return True


def _push_check_t1(now_ts, rr):
    """T1 rain-onset. Runs on EVERY owner Davis read, BEFORE the trace append
    (prev_wet must not see the current reading). Debounce mirrors
    _confirmed_wet: instant at >=1.0 mm/h, else a second wet read <=6 min."""
    try:
        rr = float(rr or 0)
        if rr < 0.2:
            return
        st = _push_state()
        prev_wet = None
        for (t, r) in reversed(_RAIN_TRACE):
            if t < now_ts and (r or 0) >= 0.2:
                prev_wet = t
                break
        fresh = prev_wet is None or now_ts - prev_wet > PUSH_T1_DRY_S
        pend = st.get("t1_pending_ts")
        if fresh:
            if rr >= 1.0:
                _push_fire("t1", "Lluvia iniciando en Virreyes",
                           "%.1f mm/h (pluviometro, %s)" % (rr, _cdmx_hhmm(now_ts)))
                st["t1_pending_ts"] = None
            else:
                st["t1_pending_ts"] = now_ts   # light first tip: wait for confirmation
            _push_state_save()
        elif pend and now_ts - pend <= PUSH_T1_CONFIRM_S:
            _push_fire("t1", "Lluvia iniciando en Virreyes",
                       "%.1f mm/h confirmada (pluviometro, %s)" % (rr, _cdmx_hhmm(now_ts)))
            st["t1_pending_ts"] = None
            _push_state_save()
    except Exception:
        pass


def _push_check_cycle(rec):
    """T2/T3, evaluated once per full cycle from the just-written row."""
    try:
        eta = rec.get("pred_eta_min")
        if eta is not None and eta <= 30:
            _push_fire("t2", "Celda aproximandose a Virreyes",
                       "Celda >=30 dBZ - ETA ~%d min (radar)" % round(eta))
        km = rec.get("lightning_corrob_min_km")
        if (rec.get("lightning_corroborated") and km is not None and km <= 25
                and rec.get("lightning_last_ts") != _push_state().get("t3_last_strike_ts")):
            if _push_fire("t3", "Rayo cerca de Virreyes",
                          "Rayo corroborado a %.0f km (WH57 + Blitzortung)" % km):
                _push_state()["t3_last_strike_ts"] = rec.get("lightning_last_ts")
                _push_state_save()
    except Exception:
        pass


@app.route("/api/push/pubkey")
def push_pubkey():
    try:
        return jsonify({"ok": True, "key": _vapid_public_b64()})
    except Exception as exc:
        return jsonify({"ok": False, "error": repr(exc)}), 500


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    try:
        sub = request.get_json(force=True) or {}
        if not sub.get("endpoint"):
            return jsonify({"ok": False, "error": "no_endpoint"}), 400
        subs = [s for s in _push_subs_load() if s.get("endpoint") != sub["endpoint"]]
        subs.append(sub)
        _push_subs_save(subs)
        _push_log({"ts": int(time.time()), "event": "subscribe", "n_subs": len(subs)})
        return jsonify({"ok": True, "n": len(subs)})
    except Exception as exc:
        return jsonify({"ok": False, "error": repr(exc)}), 500


@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    try:
        body = request.get_json(force=True) or {}
        subs = [s for s in _push_subs_load() if s.get("endpoint") != body.get("endpoint")]
        _push_subs_save(subs)
        _push_log({"ts": int(time.time()), "event": "unsubscribe", "n_subs": len(subs)})
        return jsonify({"ok": True, "n": len(subs)})
    except Exception as exc:
        return jsonify({"ok": False, "error": repr(exc)}), 500


@app.route("/api/pushes")
def pushes_json():
    """Audit trail (same doctrine as /weakgate): every push attempt, counts by
    tier. Subscription endpoints are capability URLs -> only counts exposed."""
    tail = []
    try:
        with open(PUSH_LOG_FILE) as fh:
            tail = [json.loads(l) for l in fh.read().splitlines()[-200:] if l.strip()]
    except Exception:
        pass
    counts = {}
    for e in tail:
        k = e.get("tier") or e.get("event") or "?"
        counts[k] = counts.get(k, 0) + 1
    return jsonify({"ok": True, "n_subs": len(_push_subs_load()),
                    "cooldowns_s": PUSH_COOLDOWN_S, "counts_last200": counts,
                    "log": tail})


@app.route("/pushes")
def pushes_page():
    return ("<html><head><meta charset='utf-8'><title>Pushes</title></head><body "
            "style='font-family:monospace;background:#0d1117;color:#c9d1d9;padding:20px'>"
            "<h3>Notificaciones enviadas (audit)</h3><pre id='o'>cargando...</pre>"
            "<script>fetch('/api/pushes').then(r=>r.json()).then(d=>{"
            "document.getElementById('o').textContent=JSON.stringify(d,null,2);});"
            "</script></body></html>")


DAVIS_POLL_S = 60          # item 11: WeatherLink hard floor -- never below 60 s
_DAVIS_NEXT = {"t": 0.0}


def _davis_poll():
    """Single WeatherLink consumer at ~60 s cadence (owner only). Hard floor
    DAVIS_POLL_S between calls; backoff on failure (180 s) and 429 (300 s).
    Returns the normalized dict, or None when skipped/failed."""
    now = time.time()
    if now < _DAVIS_NEXT["t"]:
        return None
    try:
        status, raw = weatherlink_current_raw()
    except Exception:
        status, raw = 599, None
    if status == 200:
        _DAVIS_NEXT["t"] = now + DAVIS_POLL_S
        _AUTOLOG_STATUS["last_davis_ts"] = int(now)
        return normalize_weatherlink(raw)
    _DAVIS_NEXT["t"] = now + (300 if status == 429 else 180)
    _AUTOLOG_STATUS["last_error"] = "davis_poll status %s" % status
    return None


def _station_row(davis):
    """item 11 schema decision: 1-min reads between full cycles are LIGHTWEIGHT
    station-only rows (t, station block, src marker) -- never full snapshots
    with stale radar/env copied in. Readers either use them (onset timing at
    1-min resolution) or filter by src=="station" (precursor/hotspots/episode
    readers keep full-snapshot semantics)."""
    try:
        now_ts = int(time.time())
        p = davis.get("pressure_hpa")
        rec = {"t": now_ts, "src": "station",
               "station": {"rain_rate": davis.get("rain_rate_mm_h") or 0,
                           "rain_day": davis.get("rain_day_mm") or 0,
                           "temp_c": davis.get("temperature_c"),
                           "humidity_pct": davis.get("humidity_pct"),
                           "dew_point_c": davis.get("dew_point_c"),
                           "press": p,
                           "press_trend": record_pressure(p),
                           "press_trend_15m": pressure_trend_15m(p)}}
        with open(EVENTLOG_FILE, "a") as fh:
            fh.write(json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n")
        _push_check_t1(now_ts, rec["station"]["rain_rate"])   # item 25: BEFORE append
        _RAIN_TRACE.append((now_ts, rec["station"]["rain_rate"]))
        cutoff = now_ts - 3 * 3600
        while _RAIN_TRACE and _RAIN_TRACE[0][0] < cutoff:
            _RAIN_TRACE.pop(0)
        _save_rain_trace()
    except Exception as exc:
        _AUTOLOG_STATUS["last_error"] = "station_row: %r" % (exc,)


def _auto_log_loop():
    # item 11: split cadence. Every ~60 s (owner only): one Davis read -> a
    # lightweight station row. Full snapshots (radar + model + scoring) stay on
    # the adaptive 3/20-min cycle and CONSUME the same Davis read, so the
    # 60 s WeatherLink floor holds by construction.
    _AUTOLOG_STATUS["owner_pid"] = os.getpid()
    _AUTOLOG_STATUS["writer_id"] = WRITER_ID
    _AUTOLOG_STATUS["davis_cadence_s"] = DAVIS_POLL_S
    _AUTOLOG_STATUS["main_cycle_s"] = "180 active / 1200 quiet (adaptive)"
    next_log = 0
    while True:
        try:
            owner = _lease_ensure()
            _AUTOLOG_STATUS["lease_holder"] = (_lease_read() or {}).get("writer_id")
        except Exception as exc:
            owner = False                 # fail-safe: unknown ownership -> no write
            _AUTOLOG_STATUS["state"] = "lease-error (not writing)"
            _AUTOLOG_STATUS["last_error"] = "lease: %r" % (exc,)
        if owner:
            _AUTOLOG_STATUS["state"] = "running (lease owner)"
            now = time.time()
            davis = _davis_poll()
            if now >= next_log:
                try:
                    n_cells, rain_rate = _auto_log_once(davis)
                    _AUTOLOG_STATUS["runs"] += 1
                    _AUTOLOG_STATUS["last_run"] = int(now)
                    _AUTOLOG_STATUS["last_error"] = None
                    pending = os.path.exists(PENDING_FILE) and os.path.getsize(PENDING_FILE) > 0
                    active = (n_cells > 0) or (rain_rate and rain_rate > 0) or pending
                except Exception as exc:
                    _AUTOLOG_STATUS["last_error"] = repr(exc)
                    active = False
                next_log = now + (3 * 60 if active else 20 * 60)
                # "+30" nowcast rides the full cycle, strictly AFTER all
                # existing work (row already written); it has its own guard so
                # a failure can never touch the cycle or the lease.
                _nowcast_cycle()
            elif davis:
                _station_row(davis)
        elif not _AUTOLOG_STATUS.get("state", "").startswith("lease-error"):
            _AUTOLOG_STATUS["state"] = "standby (lease held elsewhere)"
        time.sleep(60)


_AUTOLOG_STARTED = False   # in-process guard: one loop thread per process


def start_autologger():
    if os.environ.get("AUTOLOG", "1") != "1":
        _AUTOLOG_STATUS["state"] = "disabled (AUTOLOG!=1)"
        return
    global _AUTOLOG_STARTED
    if _AUTOLOG_STARTED:
        return
    _AUTOLOG_STARTED = True
    _AUTOLOG_STATUS["state"] = "starting (lease pending)"

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
threading.Thread(target=_blitz_listener, daemon=True).start()   # item 14 Phase 2
_insitu_load()   # in-situ v1 shadow model (loads from /data if installed)
_wh57_gate_load()   # WH57 gate state (stuck ring + last verdict) survives restarts


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


# ─────────────────────────────────────────────────────────────────────────
# Item 23: weak-echo gate backtest. Post-hoc analysis over storm_events.jsonl.
# The 23-jul bust (NARANJA "<15 min" off a 1.5 mm/h echo, CAPE 10 J/kg,
# ensemble 0.1 mm/6h) came from the client's weak-echo branches: the only way
# a logged snapshot reaches ops>=2 with no rain at the station, no echo over
# Virreyes, no cell with ETA<=45 and no close lightning is the ETA-consistency
# guard firing on a danger-class headline from sub-cell evidence. This replay
# counts those escalations, checks whether rain actually arrived, and asks
# whether the gate would have suppressed a real onset (must be ZERO).
# Historical rows predate the ens6h/weak_gate fields, so the gate is evaluated
# as its SUPERSET (CAPE floor alone, drizzle + ensemble assumed to agree):
# if even the superset misses zero onsets, the stricter live gate does too.
# ─────────────────────────────────────────────────────────────────────────
WG_CAPE_FLOOR = 100        # J/kg — MUST match weakEchoVerdict in index.html
WG_EPISODE_GAP_S = 30 * 60
WG_VERIFY_S = 60 * 60
# candidate rule B (counterfactual only): AMARILLO iff BOTH hold, else VERDE
WG_RULEB_CAPE = 500        # J/kg, CAPE max12 threshold
WG_RULEB_PERSIST = 2       # consecutive cycles (rows) in the episode


def _wg_backtest():
    rows, rain = [], []
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
                ts = int(ts)
                st = o.get("station") or {}
                rr = float(st.get("rain_rate") or 0)
                rain.append((ts, rr))
                if o.get("src") == "station":
                    continue          # 1-min station row: no radar/ops context
                rows.append((ts, rr, o))
    except FileNotFoundError:
        pass
    rain.sort()
    rows.sort(key=lambda r: r[0])

    def _wet_within(t0, horizon_s):
        for (t, rr) in rain:
            if t <= t0:
                continue
            if t - t0 > horizon_s:
                break
            if rr >= 0.2:
                return True
        return False

    weak = []
    for (ts, rr, o) in rows:
        if (o.get("ops") or 0) < 2 or rr > 0:
            continue
        if float(o.get("echo_now") or 0) >= 0.3:
            continue
        cells = o.get("cells") or []
        if any(c.get("eta") is not None and c["eta"] <= 45 for c in cells):
            continue
        lt = o.get("lightning") or {}
        closest = lt.get("closest")
        if (lt.get("n60") or 0) > 0 and closest is not None and closest <= 25:
            continue
        env = o.get("env") or {}
        wg = o.get("weak_gate")
        if wg is not None:
            fired = bool(wg.get("fired"))
        else:
            cape12 = env.get("cape_max12")
            fired = cape12 is not None and cape12 < WG_CAPE_FLOOR
        weak.append({"t": ts, "ops": o.get("ops"), "cape_max12": env.get("cape_max12"),
                     "li": env.get("li"), "ens6h": env.get("ens6h"),
                     "gate": fired, "wg": wg})

    episodes = []
    for w in weak:
        if episodes and w["t"] - episodes[-1]["rows"][-1]["t"] <= WG_EPISODE_GAP_S:
            episodes[-1]["rows"].append(w)
        else:
            episodes.append({"rows": [w]})
    for ep in episodes:
        r0, rN = ep["rows"][0], ep["rows"][-1]
        ep["start"] = r0["t"]
        ep["day"] = _storm_day(r0["t"])
        ep["dur_min"] = (rN["t"] - r0["t"]) // 60
        ep["n_rows"] = len(ep["rows"])
        ep["cape_max12"] = max((r["cape_max12"] for r in ep["rows"]
                                if r["cape_max12"] is not None), default=None)
        # verified if rain arrived within 60 min of ANY escalated row (lenient
        # toward the alert: a late-episode verification still counts as real)
        ep["verified"] = any(_wet_within(r["t"], WG_VERIFY_S) for r in ep["rows"])
        # the gate silences the episode only if it fires on EVERY row of it
        ep["gate_suppressed"] = all(r["gate"] for r in ep["rows"])
        ep["missed_onset"] = ep["gate_suppressed"] and ep["verified"]
        # ── candidate de-escalation rules (COUNTERFACTUAL columns only —
        # the live level logic is untouched until one is chosen).
        # A) PURE GREEN: weak-echo evidence never escalates the level.
        # B) CONDITIONAL AMARILLO: AMARILLO only if CAPE max12 >= 500 AND the
        #    episode persists >= 2 consecutive cycles; else VERDE. Weak
        #    evidence never goes above AMARILLO under either rule.
        ep["rule_a"] = "VERDE"
        ep["rule_b"] = ("AMARILLO" if (ep["cape_max12"] is not None
                                       and ep["cape_max12"] >= WG_RULEB_CAPE
                                       and ep["n_rows"] >= WG_RULEB_PERSIST)
                        else "VERDE")
        del ep["rows"]

    # 24-jul decision: RULE A live as of BUILD 130 — weak-echo/model-field
    # evidence never escalates the ops level. Rule B kept as counterfactual
    # reference (its AMARILLO would have been false 12 of 13 times).
    WG_DECISION = {"rule": "A", "date": "2026-07-24", "live_build": 130,
                   "accepted_miss": "16-jun-class episodes (1 of 28) read VERDE; "
                                    "echo stays visible in panel/map lines"}
    n = len(episodes)
    nv = sum(1 for e in episodes if e["verified"])
    ns = sum(1 for e in episodes if e["gate_suppressed"])
    nm = sum(1 for e in episodes if e["missed_onset"])
    return {
        "ok": True,
        "params": {"cape_floor": WG_CAPE_FLOOR, "episode_gap_min": WG_EPISODE_GAP_S // 60,
                   "verify_min": WG_VERIFY_S // 60},
        "summary": {
            "weak_escalation_episodes": n,
            "verified_within_60min": nv,
            "false_alarms": n - nv,
            "gate_suppressed": ns,
            "gate_suppressed_false_alarms": sum(1 for e in episodes
                                                if e["gate_suppressed"] and not e["verified"]),
            "gate_missed_onsets": nm,   # ship criterion: MUST be 0
            "false_alarm_rate_before": round((n - nv) / n, 3) if n else None,
            "false_alarm_rate_after": (round((n - nv - sum(1 for e in episodes
                                       if e["gate_suppressed"] and not e["verified"]))
                                       / max(1, n - ns), 3) if n else None),
            # counterfactual de-escalation candidates (decision pending):
            "rule_a": {"verde": n, "amarillo": 0,
                       "verified_reading_verde": nv},
            "rule_b": {"verde": sum(1 for e in episodes if e["rule_b"] == "VERDE"),
                       "amarillo": sum(1 for e in episodes if e["rule_b"] == "AMARILLO"),
                       "verified_reading_verde": sum(1 for e in episodes
                                                     if e["verified"] and e["rule_b"] == "VERDE"),
                       "verified_reading_amarillo": sum(1 for e in episodes
                                                        if e["verified"] and e["rule_b"] == "AMARILLO"),
                       "params": {"cape_min": WG_RULEB_CAPE, "persist_rows": WG_RULEB_PERSIST}},
            "decision": WG_DECISION,
        },
        "episodes": episodes,
        "note": ("Episodios: filas consecutivas (gap <=30 min) con ops>=2 sin lluvia en "
                 "estacion, sin eco sobre Virreyes, sin celda con ETA<=45 y sin rayos a "
                 "<=25 km — solo la rama de eco debil / modelo puede producirlas. Filas "
                 "historicas sin campo weak_gate se evaluan con el SUPERSET del gate "
                 "(solo piso de CAPE); si ni el superset pierde onsets reales, el gate "
                 "en vivo (mas estricto: ademas requiere eco llovizna y ensamble seco) "
                 "tampoco. gate_missed_onsets debe ser CERO para mantener el gate. "
                 "DECIDIDO 24-jul: REGLA A en vivo desde BUILD 130 — evidencia "
                 "debil nunca escala el nivel (la linea de eco sigue visible en "
                 "panel y mapa). Regla B se conserva como referencia: su AMARILLO "
                 "habria sido falso 12 de 13 veces (8% precision). Costo aceptado "
                 "conscientemente: episodios clase 16-jun (1 de 28) leen VERDE."),
    }


@app.route("/api/weakgate")
def weakgate_json():
    try:
        return jsonify(_wg_backtest())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


WG_PAGE = """<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Virreyes · Backtest gate eco-debil</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:18px;max-width:900px;margin-left:auto;margin-right:auto}
h1{font-size:1.05rem;letter-spacing:.03em}
.muted{color:#9aa3b2}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:16px;margin:12px 0}
.row{display:flex;gap:18px;flex-wrap:wrap;font-family:ui-monospace,monospace;font-size:.85rem}
.k{color:#9aa3b2}
table{width:100%;border-collapse:collapse;font-family:ui-monospace,monospace;font-size:.75rem}
th,td{padding:6px 8px;text-align:left;border-bottom:1px solid #21262d}
th{color:#9aa3b2;font-weight:600;font-size:.65rem;letter-spacing:.06em;text-transform:uppercase}
.ok{color:#3fb950}.bad{color:#f78166}.warn{color:#d29922}
a{color:#58a6ff}
</style></head><body>
<h1>&#x1F6E1; Backtest &mdash; gate de plausibilidad para ecos debiles (item 23)</h1>
<div class="muted" style="font-size:.8rem;margin-bottom:6px">Escalaciones NARANJA/inminente nacidas de la rama de eco debil, contra la lluvia real del pluviometro Davis.</div>
<div class="card"><div class="row" id="summary"><span class="muted">Cargando...</span></div></div>
<div class="card"><div class="row" id="rules"></div></div>
<div class="card" style="overflow-x:auto"><table id="tbl"><thead><tr>
<th>Inicio (CDMX)</th><th>Dur</th><th>Filas</th><th>CAPE max12</th><th>Lluvia &le;60 min</th><th>Gate</th><th>Veredicto</th><th>Regla A &#9733; en vivo</th><th>Regla B (ref)</th>
</tr></thead><tbody></tbody></table></div>
<div class="card muted" style="font-size:.78rem;line-height:1.6" id="note"></div>
<p class="muted" style="font-size:.72rem"><a href="/api/weakgate">JSON</a> &middot; <a href="/readiness">readiness</a> &middot; <a href="/">dashboard</a></p>
<script>
fetch('/api/weakgate').then(r=>r.json()).then(d=>{
  const s=d.summary;
  const miss = s.gate_missed_onsets;
  document.getElementById('summary').innerHTML=
    '<span><span class="k">Episodios eco-debil</span> '+s.weak_escalation_episodes+'</span>'
    +'<span><span class="k">Verificados &le;60 min</span> '+s.verified_within_60min+'</span>'
    +'<span><span class="k">Falsas alarmas</span> <span class="bad">'+s.false_alarms+'</span></span>'
    +'<span><span class="k">Suprimidos por gate</span> '+s.gate_suppressed+'</span>'
    +'<span><span class="k">Onsets reales perdidos</span> <span class="'+(miss===0?'ok':'bad')+'">'+miss+(miss===0?' ✓':' ✗ RE-TUNE')+'</span></span>'
    +'<span><span class="k">FAR antes → despues</span> '+(s.false_alarm_rate_before==null?'--':s.false_alarm_rate_before+' → '+s.false_alarm_rate_after)+'</span>';
  const tz=new Intl.DateTimeFormat('es-MX',{timeZone:'America/Mexico_City',month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
  document.querySelector('#tbl tbody').innerHTML=(d.episodes||[]).map(e=>{
    const v = e.verified?'<span class="ok">SI</span>':'<span class="bad">no</span>';
    const g = e.gate_suppressed?'suprime':'pasa';
    const verdict = e.missed_onset?'<span class="bad">ONSET PERDIDO</span>'
      : e.gate_suppressed && !e.verified ? '<span class="ok">falsa alarma eliminada</span>'
      : !e.gate_suppressed && !e.verified ? '<span class="warn">falsa alarma restante</span>'
      : '<span class="muted">alerta legitima intacta</span>';
    const lvl = x => x==='AMARILLO' ? '<span class="warn">AMARILLO</span>' : '<span class="ok">VERDE</span>';
    return '<tr><td>'+tz.format(new Date(e.start*1000))+'</td><td>'+e.dur_min+' min</td><td>'+e.n_rows
      +'</td><td>'+(e.cape_max12==null?'--':e.cape_max12+' J/kg')+'</td><td>'+v+'</td><td>'+g+'</td><td>'+verdict
      +'</td><td>'+lvl(e.rule_a)+'</td><td>'+lvl(e.rule_b)+'</td></tr>';
  }).join('') || '<tr><td colspan="9" class="muted">Sin episodios de escalacion eco-debil en el registro.</td></tr>';
  const ra=s.rule_a, rb=s.rule_b;
  if(ra && rb) document.getElementById('rules').innerHTML=
    (s.decision?'<span><span class="k">Decision '+s.decision.date+'</span> <span class="ok">REGLA '+s.decision.rule+' EN VIVO</span> (BUILD '+s.decision.live_build+')</span>':'')
    +'<span><span class="k">Regla A (verde puro)</span> '+ra.verde+' VERDE &middot; verificados leyendo VERDE: '+ra.verified_reading_verde+'</span>'
    +'<span><span class="k">Regla B (CAPE&ge;'+rb.params.cape_min+' + &ge;'+rb.params.persist_rows+' ciclos)</span> '
    +rb.amarillo+' AMARILLO / '+rb.verde+' VERDE &middot; verificados: '+rb.verified_reading_amarillo+' AMARILLO, '+rb.verified_reading_verde+' VERDE</span>';
  document.getElementById('note').textContent=d.note;
}).catch(e=>{ document.getElementById('summary').textContent='Error cargando datos: '+e; });
</script>
</body></html>"""


@app.route("/weakgate")
def weakgate_page():
    r = make_response(WG_PAGE)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    r.headers["Cache-Control"] = "no-store"
    return r


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
