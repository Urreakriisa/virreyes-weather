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
        errs = [abs(r["actual_min"] - r["pred_eta_min"])
                for r in hits if r.get("actual_min") is not None and r.get("pred_eta_min") is not None]
        mae = round(sum(errs) / len(errs), 1) if errs else None
        # precision: of all predictions, how many actually produced rain
        precision = round(100 * len(hits) / n) if n else None
        return jsonify({
            "ok": True,
            "labeled_pairs": n,
            "hits": len(hits),
            "misses": len(misses),
            "precision_pct": precision,
            "eta_mae_min": mae,
            "note": "MAE = mean absolute error of heuristic ETA on hits; "
                    "baseline for the future learned model to beat.",
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
    return jsonify(
        {
            "ok": True,
            "station_id": WL_STATION_ID,
            "has_api_key": bool(WL_API_KEY),
            "has_api_secret": bool(WL_API_SECRET),
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


def terrain_elev(lat, lon):
    num = den = 0.0
    for a_lat, a_lon, e in TERRAIN_ANCHORS:
        dx = (lon - a_lon) * KM_LON
        dy = (lat - a_lat) * KM_LAT
        d2 = dx * dx + dy * dy + 4.0
        w = 1.0 / (d2 * d2)
        num += w * e
        den += w
    return num / den


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
    THR = 2.0
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


def _fetch_steering():
    try:
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
               "&hourly=wind_speed_700hPa,wind_direction_700hPa,cape,lifted_index"
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
        return steer, cape, li
    except Exception:
        return None, None, None


PENDING_FILE = os.path.join(DATA_DIR, "pending_pred.jsonl")
OUTCOME_FILE = os.path.join(DATA_DIR, "labeled_outcomes.jsonl")
_RAIN_TRACE = []   # (ts, rain_rate) ring buffer for outcome detection


def _terrain_grad(xkm, ykm):
    h = 1.5
    e_x1 = terrain_elev(LAT + ykm / KM_LAT, LON + (xkm + h) / KM_LON)
    e_x0 = terrain_elev(LAT + ykm / KM_LAT, LON + (xkm - h) / KM_LON)
    e_y1 = terrain_elev(LAT + (ykm + h) / KM_LAT, LON + xkm / KM_LON)
    e_y0 = terrain_elev(LAT + (ykm - h) / KM_LAT, LON + xkm / KM_LON)
    return (e_x1 - e_x0) / (2 * h), (e_y1 - e_y0) / (2 * h)


def _terrain_correct(xkm, ykm, vx, vy):
    speed = math.hypot(vx, vy)
    if speed < 3:
        return vx, vy
    gx, gy = _terrain_grad(xkm, ykm)
    gmag = math.hypot(gx, gy)
    if gmag < 4:
        return vx, vy
    ux, uy = vx / speed, vy / speed
    nx, ny = gx / gmag, gy / gmag
    uphill = ux * nx + uy * ny
    steep = min(1.0, gmag / 25.0)
    cvx, cvy = vx, vy
    if uphill > 0.15:
        damp = 0.6 * steep * uphill
        cvx = vx - damp * nx * speed
        cvy = vy - damp * ny * speed
        tx, ty = -ny, nx
        sense = 1 if (ux * tx + uy * ty) >= 0 else -1
        deflect = 0.4 * steep * uphill
        cvx += sense * deflect * tx * speed
        cvy += sense * deflect * ty * speed
    elif uphill < -0.3:
        accel = 0.12 * steep * (-uphill)
        cvx, cvy = vx * (1 + accel), vy * (1 + accel)
    cs = math.hypot(cvx, cvy)
    if cs > 0:
        clamped = max(0.6 * speed, min(1.3 * speed, cs))
        cvx, cvy = cvx / cs * clamped, cvy / cs * clamped
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

        steer, cape, li = _fetch_steering()
        now_ts = int(time.time())

        # maintain a rain trace (last 3h) for outcome resolution
        _RAIN_TRACE.append((now_ts, rain_rate or 0))
        cutoff = now_ts - 3 * 3600
        while _RAIN_TRACE and _RAIN_TRACE[0][0] < cutoff:
            _RAIN_TRACE.pop(0)

        # terrain-aware ETA prediction for the nearest approaching cell
        eta_min, eta_cell = _predict_eta(cells, steer)

        rec = {
            "t": now_ts,
            "src": "server",
            "station": {"rain_rate": rain_rate, "rain_day": rain_day,
                        "press": davis.get("pressure_hpa"),
                        "press_trend": davis.get("pressure_trend_3h")},
            "env": {"cape": round(cape) if cape is not None else None,
                    "li": li},
            "steering": steer,
            "rv_time": rv_time,
            "cells": cells,
            "n_cells": len(cells),
            "pred_eta_min": eta_min,
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


def _auto_log_loop():
    # adaptive cadence: 3 min when cells/rain present or predictions pending
    # (so rain onset is caught for outcome labeling), else 20 min.
    while True:
        try:
            n_cells, rain_rate = _auto_log_once()
            pending = os.path.exists(PENDING_FILE) and os.path.getsize(PENDING_FILE) > 0
            active = (n_cells > 0) or (rain_rate and rain_rate > 0) or pending
        except Exception:
            active = False
        try:
            os.utime(_AUTOLOG_LOCK, None)   # keep ownership fresh
        except Exception:
            pass
        time.sleep(3 * 60 if active else 20 * 60)


_AUTOLOG_LOCK = os.path.join(DATA_DIR, "autolog.lock")


def _claim_autolog_owner():
    """Only one worker should run the logger. Use an exclusive-create lock file
    holding the PID; refreshed each cycle. Stale locks (>3h) are reclaimable."""
    try:
        if os.path.exists(_AUTOLOG_LOCK):
            age = time.time() - os.path.getmtime(_AUTOLOG_LOCK)
            if age < 3 * 3600:
                return False
        fd = os.open(_AUTOLOG_LOCK, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except Exception:
        return False


def start_autologger():
    if os.environ.get("AUTOLOG", "1") != "1":
        return
    if not _claim_autolog_owner():
        return
    t = threading.Thread(target=_auto_log_loop, daemon=True)
    t.start()


# Start the background logger as soon as the module is imported (covers
# gunicorn, which imports the app rather than running __main__).
start_autologger()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
