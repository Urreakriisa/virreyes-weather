import hashlib
import hmac
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from io import BytesIO

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from PIL import Image


app = Flask(__name__, static_folder=".")
CORS(app)

LAT = float(os.environ.get("SITE_LAT", "19.43"))
LON = float(os.environ.get("SITE_LON", "-99.13"))
TZ = os.environ.get("SITE_TZ", "America/Mexico_City")

WL_API_KEY = os.environ.get("WL_API_KEY", "").strip()
WL_API_SECRET = os.environ.get("WL_API_SECRET", "").strip()
WL_STATION_ID = os.environ.get("WL_STATION_ID", "238059").strip()


def fetch_bytes(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "virreyes-weather/4.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url: str, timeout: int = 15):
    return json.loads(fetch_bytes(url, timeout=timeout).decode("utf-8"))


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg):
    if deg is None:
        return "--"
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((float(deg) + 11.25) / 22.5) % 16]


def safe_round(x, n=1):
    return None if x is None else round(float(x), n)


def first_number(*values):
    for value in values:
        if isinstance(value, (int, float)):
            return float(value)
    return None


def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.update(flatten(v, key))
            else:
                out[key] = v
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            out.update(flatten(item, f"{prefix}.{i}" if prefix else str(i)))
    return out


def f_to_c(v):
    return None if v is None else (v - 32.0) * 5.0 / 9.0


def mph_to_kmh(v):
    return None if v is None else v * 1.609344


def inhg_to_hpa(v):
    return None if v is None else v * 33.8638866667


def inch_to_mm(v):
    return None if v is None else v * 25.4


def sign_weatherlink(params: dict) -> str:
    msg = "".join(k + str(params[k]) for k in sorted(params))
    return hmac.new(WL_API_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def weatherlink_current_raw():
    if not WL_API_KEY or not WL_API_SECRET or not WL_STATION_ID:
        return 500, {"error": "Missing WL_API_KEY, WL_API_SECRET, or WL_STATION_ID in Railway variables."}

    t = int(time.time())
    params = {"api-key": WL_API_KEY, "station-id": WL_STATION_ID, "t": t}
    sig = sign_weatherlink(params)
    query = urllib.parse.urlencode({"api-key": WL_API_KEY, "t": t, "api-signature": sig})
    url = f"https://api.weatherlink.com/v2/current/{WL_STATION_ID}?{query}"

    try:
        return 200, fetch_json(url)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, {"error": body}
    except Exception as exc:
        return 502, {"error": str(exc)}


def extract_rain_day_mm(raw, records):
    candidates = []
    for d in records:
        for name in ["rainfall_daily_mm", "rain_day_mm", "rain_today_mm", "rain_daily_mm"]:
            if isinstance(d.get(name), (int, float)):
                candidates.append((name, float(d[name]), "mm", "record"))
        for name in ["rainfall_daily_in", "rain_day_in", "rain_today_in", "rain_daily_in"]:
            if isinstance(d.get(name), (int, float)):
                candidates.append((name, inch_to_mm(float(d[name])), "in->mm", "record"))

    for key, val in flatten(raw).items():
        if not isinstance(val, (int, float)):
            continue
        leaf = key.lower().split(".")[-1]
        if ("rain" in leaf or "rainfall" in leaf) and any(x in leaf for x in ["daily", "day", "today"]) and "rate" not in leaf:
            if "clicks" in leaf:
                continue
            if "mm" in leaf:
                candidates.append((key, float(val), "mm", "flat"))
            elif "_in" in leaf or leaf.endswith("in"):
                candidates.append((key, inch_to_mm(float(val)), "in->mm", "flat"))

    candidates = [c for c in candidates if c[1] is not None and c[1] >= 0]

    def score(c):
        k = c[0].lower()
        s = 0
        if "daily" in k or "today" in k or "day" in k:
            s += 100
        if "mm" in k:
            s += 10
        if c[3] == "record":
            s += 10
        return s

    candidates.sort(key=score, reverse=True)
    return (candidates[0][1], candidates[:10]) if candidates else (None, [])


def extract_rain_rate_mm(raw, records):
    candidates = []
    for d in records:
        for name in ["rain_rate_last_mm", "rain_rate_mm", "rain_rate_hi_mm"]:
            if isinstance(d.get(name), (int, float)):
                candidates.append((name, float(d[name]), "mm", "record"))
        for name in ["rain_rate_last_in", "rain_rate_in", "rain_rate_hi_in"]:
            if isinstance(d.get(name), (int, float)):
                candidates.append((name, inch_to_mm(float(d[name])), "in->mm", "record"))
    candidates = [c for c in candidates if c[1] is not None and c[1] >= 0]
    return (candidates[0][1], candidates[:10]) if candidates else (None, [])


def normalize_weatherlink(raw: dict) -> dict:
    records = []
    for sensor in raw.get("sensors", []):
        for datum in sensor.get("data", []):
            if isinstance(datum, dict):
                records.append(datum)

    temp = hum = dew = wind = wind_dir = pressure = ts = None
    for d in records:
        temp = first_number(temp, d.get("temp"), d.get("temperature"))
        hum = first_number(hum, d.get("hum"), d.get("humidity"))
        dew = first_number(dew, d.get("dew_point"), d.get("dewpoint"))
        wind = first_number(wind, d.get("wind_speed_last"), d.get("wind_speed_avg_last_1_min"), d.get("wind_speed_avg_last_2_min"))
        wind_dir = first_number(wind_dir, d.get("wind_dir_last"), d.get("wind_dir_scalar_avg_last_1_min"))
        pressure = first_number(pressure, d.get("bar_sea_level"), d.get("bar_absolute"), d.get("bar"))
        ts = first_number(ts, d.get("ts"), d.get("timestamp"), d.get("time"))

    rain_day_mm, rain_day_candidates = extract_rain_day_mm(raw, records)
    rain_rate_mm, rain_rate_candidates = extract_rain_rate_mm(raw, records)

    temp_c = f_to_c(temp) if temp is not None and temp > 45 else temp
    dew_c = f_to_c(dew) if dew is not None and dew > 45 else dew
    pressure_hpa = inhg_to_hpa(pressure) if pressure is not None and pressure < 100 else pressure

    updated = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else datetime.now(timezone.utc).isoformat()

    return {
        "station_id": WL_STATION_ID,
        "updated_utc": updated,
        "temperature_c": safe_round(temp_c, 1),
        "humidity_pct": safe_round(hum, 0),
        "dew_point_c": safe_round(dew_c, 1),
        "wind_speed_kmh": safe_round(mph_to_kmh(wind), 1) if wind is not None else None,
        "wind_direction_deg": safe_round(wind_dir, 0),
        "wind_direction_compass": compass(wind_dir),
        "pressure_hpa": safe_round(pressure_hpa, 1),
        "rain_day_mm": safe_round(rain_day_mm, 1),
        "rain_rate_mm_h": safe_round(rain_rate_mm, 1),
        "debug_rain_day_candidates": rain_day_candidates,
        "debug_rain_rate_candidates": rain_rate_candidates,
    }


def get_openmeteo():
    params = {
        "latitude": LAT,
        "longitude": LON,
        "timezone": TZ,
        "forecast_days": 5,
        "past_days": 1,
        "hourly": "temperature_2m,precipitation,precipitation_probability,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    return fetch_json(url, timeout=20)


def lonlat_to_global_pixel(lat, lon, z, tile_size=512):
    siny = min(max(math.sin(math.radians(lat)), -0.9999), 0.9999)
    scale = tile_size * (2 ** z)
    return (lon + 180.0) / 360.0 * scale, (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * scale


def global_pixel_to_lonlat(px, py, z, tile_size=512):
    scale = tile_size * (2 ** z)
    lon = px / scale * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * py / scale
    lat = math.degrees(math.atan(math.sinh(n)))
    return lat, lon


def radar_intensity(r, g, b, a):
    if a < 45:
        return 0
    mx, mn = max(r, g, b), min(r, g, b)
    sat = mx - mn
    if sat < 35:
        return 0
    if r > 210 and g > 210 and b > 210:
        return 0
    if r < 25 and g < 25 and b < 25:
        return 0
    return min(255, int(0.65 * mx + 0.35 * sat))


def analyze_radar_frame(host, path, z=7, radius_km=110):
    center_x, center_y = lonlat_to_global_pixel(LAT, LON, z)
    tile_size = 512
    tx, ty = int(center_x // tile_size), int(center_y // tile_size)
    origin_tx, origin_ty = tx - 1, ty - 1
    mosaic = Image.new("RGBA", (tile_size * 3, tile_size * 3), (0, 0, 0, 0))

    for dx in range(3):
        for dy in range(3):
            url = f"{host}{path}/512/{z}/{origin_tx + dx}/{origin_ty + dy}/2/1_1.png"
            try:
                img = Image.open(BytesIO(fetch_bytes(url, timeout=10))).convert("RGBA")
                mosaic.paste(img, (dx * tile_size, dy * tile_size))
            except Exception:
                pass

    wet = strong = local = 0
    wx = wy = tw = 0.0
    nearest = None
    nearest_ll = None
    max_i = 0
    points = []

    for py in range(0, mosaic.height, 3):
        for px in range(0, mosaic.width, 3):
            r, g, b, a = mosaic.getpixel((px, py))
            inten = radar_intensity(r, g, b, a)
            if inten <= 0:
                continue
            lat, lon = global_pixel_to_lonlat(origin_tx * tile_size + px, origin_ty * tile_size + py, z)
            d = haversine_km(LAT, LON, lat, lon)
            if d > radius_km:
                continue

            wet += 1
            strong += 1 if inten > 150 else 0
            local += 1 if d <= 8 else 0
            max_i = max(max_i, inten)

            if nearest is None or d < nearest:
                nearest = d
                nearest_ll = (lat, lon)

            w = 1 + inten / 255
            wx += px * w
            wy += py * w
            tw += w

            if len(points) < 180 and inten > 95:
                points.append({"lat": safe_round(lat, 4), "lon": safe_round(lon, 4), "intensity": int(inten), "distance_km": safe_round(d, 1)})

    if tw == 0:
        return {"has_echo": False, "coverage_score": 0, "strong_score": 0, "local_rain": False, "nearest_echo_km": None, "nearest_echo": None, "centroid": None, "max_intensity": 0, "echo_points": []}

    c_lat, c_lon = global_pixel_to_lonlat(origin_tx * tile_size + wx / tw, origin_ty * tile_size + wy / tw, z)
    return {
        "has_echo": True,
        "coverage_score": safe_round(min(1, wet / 1200), 3),
        "strong_score": safe_round(strong / max(1, wet), 2),
        "local_rain": bool(local > 2),
        "nearest_echo_km": safe_round(nearest, 1),
        "nearest_echo": None if nearest_ll is None else {"lat": safe_round(nearest_ll[0], 4), "lon": safe_round(nearest_ll[1], 4)},
        "centroid": {"lat": safe_round(c_lat, 4), "lon": safe_round(c_lon, 4)},
        "centroid_distance_km": safe_round(haversine_km(LAT, LON, c_lat, c_lon), 1),
        "centroid_bearing_deg": safe_round(bearing_deg(LAT, LON, c_lat, c_lon), 0),
        "centroid_compass_from_site": compass(bearing_deg(LAT, LON, c_lat, c_lon)),
        "max_intensity": int(max_i),
        "echo_points": points,
    }


def radar_nowcast():
    maps = fetch_json("https://api.rainviewer.com/public/weather-maps.json", timeout=15)
    host = maps.get("host", "https://tilecache.rainviewer.com")
    frames = maps.get("radar", {}).get("past", [])[-6:]

    analyzed = []
    for frame in frames[-4:]:
        item = analyze_radar_frame(host, frame["path"], z=7)
        item["time"] = frame.get("time")
        item["time_iso"] = datetime.fromtimestamp(frame["time"], tz=timezone.utc).isoformat() if frame.get("time") else None
        analyzed.append(item)

    current = analyzed[-1] if analyzed else {"has_echo": False}
    previous = next((x for x in reversed(analyzed[:-1]) if x.get("has_echo") and current.get("has_echo")), None)

    motion = None
    eta_minutes = None
    confidence = "low"
    headline = "Sin eco de lluvia cercano"
    text = "El radar público no muestra un eco claro dentro del radio de análisis."

    if current.get("local_rain"):
        eta_minutes = 0
        confidence = "high"
        headline = "Lluvia sobre Virreyes o muy cerca"
        text = "El radar detecta reflectividad sobre el área inmediata."
    elif current.get("has_echo"):
        headline = f"Eco de lluvia a ~{current.get('nearest_echo_km')} km"
        text = "Hay ecos cercanos, pero solo publico ETA si el movimiento es confiable."

        if previous and previous.get("centroid") and current.get("centroid"):
            p, c = previous["centroid"], current["centroid"]
            dt_h = max(1 / 60, (current["time"] - previous["time"]) / 3600)
            speed = haversine_km(p["lat"], p["lon"], c["lat"], c["lon"]) / dt_h
            direction = bearing_deg(p["lat"], p["lon"], c["lat"], c["lon"])
            to_site = bearing_deg(c["lat"], c["lon"], LAT, LON)
            angle = abs((direction - to_site + 180) % 360 - 180)
            dist = haversine_km(c["lat"], c["lon"], LAT, LON)
            closing = speed * math.cos(math.radians(angle))
            plausible = 5 <= speed <= 95
            motion = {"speed_kmh": safe_round(speed, 1), "direction_deg": safe_round(direction, 0), "direction_compass": compass(direction), "angle_to_site_deg": safe_round(angle, 0), "closing_speed_kmh": safe_round(closing, 1), "plausible": plausible}

            if plausible and closing > 8 and dist < 120:
                eta_minutes = int(max(5, min(180, (dist / closing) * 60)))
                confidence = "medium"
                text = f"El eco parece acercarse. ETA radar aproximada: {eta_minutes} minutos."
            else:
                text = "Hay lluvia regional, pero el movimiento calculado no es meteorológicamente confiable o no apunta claramente hacia Virreyes."

    if current.get("local_rain") or current.get("strong_score", 0) > 0.25 or current.get("max_intensity", 0) > 180:
        intensity = "fuerte"
    elif current.get("coverage_score", 0) > 0.10 or current.get("max_intensity", 0) > 120:
        intensity = "moderada"
    elif current.get("has_echo"):
        intensity = "ligera/aislada"
    else:
        intensity = "ninguna"

    return {
        "ok": True,
        "site": {"lat": LAT, "lon": LON, "tz": TZ},
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "rainviewer": {"host": host, "frames": frames, "native_zoom": 7},
        "analysis": {"headline": headline, "eta_minutes": eta_minutes, "confidence": confidence, "expected_intensity": intensity, "meteorologist_text": text, "current_frame": current, "motion": motion, "frames_analyzed": analyzed},
    }


@app.after_request
def no_cache(response):
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
    return jsonify({"ok": True, "source": "weatherlink", "parsed": normalize_weatherlink(raw), "raw": raw})


@app.route("/api/forecast")
def forecast():
    try:
        return jsonify({"ok": True, "source": "open-meteo", "data": get_openmeteo()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/radar-nowcast")
def nowcast():
    try:
        return jsonify(radar_nowcast())
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/health")
@app.route("/health")
def health():
    return jsonify({"ok": True, "station_id": WL_STATION_ID, "has_api_key": bool(WL_API_KEY), "has_api_secret": bool(WL_API_SECRET)})


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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)))
