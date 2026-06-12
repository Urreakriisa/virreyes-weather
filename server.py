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
    ts = None

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
        rain_day = first_number(rain_day, d.get("rain_day_in"), d.get("rain_day_mm"), d.get("rainfall_daily"))
        rain_rate = first_number(rain_rate, d.get("rain_rate_last_in"), d.get("rain_rate_hi_in"), d.get("rain_rate_mm"))
        ts = first_number(ts, d.get("ts"), d.get("timestamp"), d.get("time"))

    # Davis WeatherLink often returns US units. Convert conservatively.
    temp_c = f_to_c(temp) if temp is not None and temp > 45 else temp
    dew_c = f_to_c(dew_point) if dew_point is not None and dew_point > 45 else dew_point
    wind_kmh = mph_to_kmh(wind_speed) if wind_speed is not None else None
    pressure_hpa = inhg_to_hpa(pressure) if pressure is not None and pressure < 100 else pressure
    rain_day_mm = inch_to_mm(rain_day) if rain_day is not None and rain_day < 20 else rain_day
    rain_rate_mm = inch_to_mm(rain_rate) if rain_rate is not None and rain_rate < 20 else rain_rate

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
    }


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
    return jsonify({"ok": True, "source": "weatherlink", "parsed": parsed, "raw": raw})


@app.route("/api/tile/<int:z>/<int:x>/<int:y>")
def tile_proxy(z, x, y):
    """Proxy CARTO dark basemap tiles (free for personal use, attribution shown
    in the app) so the canvas can compose them without CORS taint."""
    try:
        if not (0 <= z <= 19):
            raise ValueError("bad zoom")
        sub = "abcd"[(x + y) % 4]
        url = f"https://{sub}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
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


@app.route("/api/rvtile/<int:ts>/<int:z>/<int:x>/<int:y>")
def rv_tile(ts, z, x, y):
    """RainViewer radar tile, black-and-white dBZ scheme for client-side decode."""
    try:
        if not (0 <= z <= 15):
            raise ValueError("bad zoom")
        url = f"https://tilecache.rainviewer.com/v2/radar/{ts}/256/{z}/{x}/{y}/0/0_0.png"
        req = urllib.request.Request(url, headers={"User-Agent": "virreyes-weather/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        r = make_response(data)
        r.headers["Content-Type"] = "image/png"
        r.headers["Cache-Control"] = "public, max-age=300"
        return r
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
