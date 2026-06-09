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


# -----------------------------
# Generic helpers
# -----------------------------

def fetch_bytes(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "virreyes-weather/2.0"})
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
    return dirs[int((deg + 11.25) / 22.5) % 16]


def safe_round(x, n=1):
    return None if x is None else round(float(x), n)


def first_number(*values):
    for value in values:
        if isinstance(value, (int, float)):
            return float(value)
    return None


# -----------------------------
# WeatherLink
# -----------------------------

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


def f_to_c(v): return None if v is None else (v - 32.0) * 5.0 / 9.0
def mph_to_kmh(v): return None if v is None else v * 1.609344
def inhg_to_hpa(v): return None if v is None else v * 33.8638866667
def inch_to_mm(v): return None if v is None else v * 25.4


def normalize_weatherlink(raw: dict) -> dict:
    records = []
    for sensor in raw.get("sensors", []):
        for datum in sensor.get("data", []):
            if isinstance(datum, dict):
                records.append(datum)

    temp = hum = dew = wind = wind_dir = pressure = rain_day = rain_rate = ts = None
    for d in records:
        temp = first_number(temp, d.get("temp"), d.get("temperature"))
        hum = first_number(hum, d.get("hum"), d.get("humidity"))
        dew = first_number(dew, d.get("dew_point"), d.get("dewpoint"))
        wind = first_number(wind, d.get("wind_speed_last"), d.get("wind_speed_avg_last_1_min"), d.get("wind_speed_avg_last_2_min"))
        wind_dir = first_number(wind_dir, d.get("wind_dir_last"), d.get("wind_dir_scalar_avg_last_1_min"))
        pressure = first_number(pressure, d.get("bar_sea_level"), d.get("bar_absolute"), d.get("bar"))
        rain_day = first_number(rain_day, d.get("rain_day_in"), d.get("rain_day_mm"), d.get("rainfall_daily"))
        rain_rate = first_number(rain_rate, d.get("rain_rate_last_in"), d.get("rain_rate_hi_in"), d.get("rain_rate_mm"))
        ts = first_number(ts, d.get("ts"), d.get("timestamp"), d.get("time"))

    temp_c = f_to_c(temp) if temp is not None and temp > 45 else temp
    dew_c = f_to_c(dew) if dew is not None and dew > 45 else dew
    pressure_hpa = inhg_to_hpa(pressure) if pressure is not None and pressure < 100 else pressure
    rain_day_mm = inch_to_mm(rain_day) if rain_day is not None and rain_day < 20 else rain_day
    rain_rate_mm = inch_to_mm(rain_rate) if rain_rate is not None and rain_rate < 20 else rain_rate

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
    }


# -----------------------------
# Open-Meteo
# -----------------------------

def get_openmeteo():
    params = {
        "latitude": LAT,
        "longitude": LON,
        "timezone": TZ,
        "forecast_days": 5,
        "past_days": 1,
        "hourly": ",".join([
            "temperature_2m",
            "relative_humidity_2m",
            "dew_point_2m",
            "precipitation",
            "rain",
            "showers",
            "precipitation_probability",
            "pressure_msl",
            "cloud_cover",
            "cape",
            "wind_speed_10m",
            "wind_direction_10m",
            "wind_gusts_10m",
        ]),
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "precipitation_probability_max",
            "wind_speed_10m_max",
            "wind_gusts_10m_max",
        ]),
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    return fetch_json(url, timeout=20)


# -----------------------------
# RainViewer radar nowcast
# -----------------------------

def lonlat_to_global_pixel(lat, lon, z, tile_size=512):
    siny = min(max(math.sin(math.radians(lat)), -0.9999), 0.9999)
    scale = tile_size * (2 ** z)
    x = (lon + 180.0) / 360.0 * scale
    y = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * scale
    return x, y


def global_pixel_to_lonlat(px, py, z, tile_size=512):
    scale = tile_size * (2 ** z)
    lon = px / scale * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * py / scale
    lat = math.degrees(math.atan(math.sinh(n)))
    return lat, lon


def analyze_radar_frame(host, path, z=7, radius_km=110):
    center_x, center_y = lonlat_to_global_pixel(LAT, LON, z)
    tile_size = 512
    tx, ty = int(center_x // tile_size), int(center_y // tile_size)

    # 3x3 tile mosaic centered on the site.
    mosaic = Image.new("RGBA", (tile_size * 3, tile_size * 3), (0, 0, 0, 0))
    origin_tx, origin_ty = tx - 1, ty - 1

    for dx in range(3):
        for dy in range(3):
            x, y = origin_tx + dx, origin_ty + dy
            url = f"{host}{path}/512/{z}/{x}/{y}/2/1_1.png"
            try:
                img = Image.open(BytesIO(fetch_bytes(url, timeout=10))).convert("RGBA")
                mosaic.paste(img, (dx * tile_size, dy * tile_size))
            except Exception:
                pass

    site_px = center_x - origin_tx * tile_size
    site_py = center_y - origin_ty * tile_size

    weighted_x = weighted_y = total_w = 0.0
    wet_pixels = strong_pixels = local_pixels = local_wet = 0
    nearest_km = None
    max_intensity = 0

    # Sample every 4 px to stay fast.
    for py in range(0, mosaic.height, 4):
        for px in range(0, mosaic.width, 4):
            r, g, b, a = mosaic.getpixel((px, py))
            if a < 20:
                continue

            global_x = origin_tx * tile_size + px
            global_y = origin_ty * tile_size + py
            plat, plon = global_pixel_to_lonlat(global_x, global_y, z)
            d = haversine_km(LAT, LON, plat, plon)
            if d > radius_km:
                continue

            # RainViewer palette: colored/opaque pixels represent radar echoes.
            # Weight stronger echoes higher using alpha and color brightness/saturation.
            intensity = max(r, g, b, a)
            if intensity < 25:
                continue

            wet_pixels += 1
            if intensity > 145:
                strong_pixels += 1
            max_intensity = max(max_intensity, intensity)
            nearest_km = d if nearest_km is None else min(nearest_km, d)

            if d <= 8:
                local_pixels += 1
                local_wet += 1

            w = 1 + intensity / 255.0
            weighted_x += px * w
            weighted_y += py * w
            total_w += w

    if total_w == 0:
        return {
            "has_echo": False,
            "coverage_score": 0,
            "strong_score": 0,
            "local_rain": False,
            "nearest_echo_km": None,
            "centroid": None,
            "max_intensity": 0,
        }

    cx = weighted_x / total_w
    cy = weighted_y / total_w
    c_lat, c_lon = global_pixel_to_lonlat(origin_tx * tile_size + cx, origin_ty * tile_size + cy, z)
    coverage = wet_pixels / 1200.0  # approximate normalized local/regional coverage
    strong = strong_pixels / max(1, wet_pixels)

    return {
        "has_echo": True,
        "coverage_score": min(1, coverage),
        "strong_score": safe_round(strong, 2),
        "local_rain": bool(local_wet > 3),
        "nearest_echo_km": safe_round(nearest_km, 1),
        "centroid": {"lat": safe_round(c_lat, 4), "lon": safe_round(c_lon, 4)},
        "centroid_distance_km": safe_round(haversine_km(LAT, LON, c_lat, c_lon), 1),
        "centroid_bearing_deg": safe_round(bearing_deg(LAT, LON, c_lat, c_lon), 0),
        "centroid_compass_from_site": compass(bearing_deg(LAT, LON, c_lat, c_lon)),
        "max_intensity": int(max_intensity),
    }


def radar_nowcast():
    maps = fetch_json("https://api.rainviewer.com/public/weather-maps.json", timeout=15)
    host = maps.get("host", "https://tilecache.rainviewer.com")
    frames = maps.get("radar", {}).get("past", [])[-6:]

    analyzed = []
    for frame in frames[-4:]:
        item = analyze_radar_frame(host, frame["path"])
        item["time"] = frame.get("time")
        item["time_iso"] = datetime.fromtimestamp(frame["time"], tz=timezone.utc).isoformat() if frame.get("time") else None
        analyzed.append(item)

    current = analyzed[-1] if analyzed else {"has_echo": False}
    previous = next((x for x in reversed(analyzed[:-1]) if x.get("has_echo") and current.get("has_echo")), None)

    motion = None
    eta_minutes = None
    confidence = "low"
    headline = "Sin eco de lluvia cercano"
    meteorologist_text = "El radar público no muestra un eco claro dentro del radio de análisis. No se debe publicar una hora estimada de llegada."

    if current.get("local_rain"):
        eta_minutes = 0
        confidence = "high"
        headline = "Lluvia sobre Virreyes o muy cerca"
        meteorologist_text = "El radar detecta reflectividad sobre el área inmediata. La llegada estimada es ahora."

    elif current.get("has_echo"):
        nearest = current.get("nearest_echo_km")
        headline = f"Eco de lluvia a ~{nearest} km" if nearest is not None else "Eco de lluvia regional"

        if previous and previous.get("centroid") and current.get("centroid"):
            p, c = previous["centroid"], current["centroid"]
            dt_h = max(1 / 60, (current["time"] - previous["time"]) / 3600)
            moved_km = haversine_km(p["lat"], p["lon"], c["lat"], c["lon"])
            speed_kmh = moved_km / dt_h
            direction = bearing_deg(p["lat"], p["lon"], c["lat"], c["lon"])

            # Does the motion vector point generally toward the station?
            to_site = bearing_deg(c["lat"], c["lon"], LAT, LON)
            angle_diff = abs((direction - to_site + 180) % 360 - 180)
            dist_to_site = haversine_km(c["lat"], c["lon"], LAT, LON)
            closing = speed_kmh * math.cos(math.radians(angle_diff))

            motion = {
                "speed_kmh": safe_round(speed_kmh, 1),
                "direction_deg": safe_round(direction, 0),
                "direction_compass": compass(direction),
                "angle_to_site_deg": safe_round(angle_diff, 0),
                "closing_speed_kmh": safe_round(closing, 1),
            }

            if closing > 8 and dist_to_site < 120:
                eta_minutes = int(max(5, min(180, (dist_to_site / closing) * 60)))
                confidence = "medium" if current.get("coverage_score", 0) > 0.05 else "low"
                headline = f"Eco moviéndose hacia Virreyes desde el {current.get('centroid_compass_from_site')}"
                meteorologist_text = (
                    f"El centro del eco se mueve hacia la estación con velocidad de cierre aproximada de "
                    f"{round(closing)} km/h. ETA radar estimada: {eta_minutes} minutos."
                )
            else:
                meteorologist_text = (
                    "Hay lluvia regional, pero el desplazamiento del eco no apunta claramente hacia Virreyes. "
                    "No publico ETA porque sería engañosa."
                )
        else:
            meteorologist_text = (
                "Hay ecos regionales, pero no hay suficientes fotogramas útiles para calcular movimiento. "
                "No publico ETA confiable."
            )

    intensity = "ninguna"
    if current.get("local_rain") or current.get("strong_score", 0) > 0.25:
        intensity = "fuerte"
    elif current.get("coverage_score", 0) > 0.10:
        intensity = "moderada"
    elif current.get("has_echo"):
        intensity = "ligera/aislada"

    return {
        "ok": True,
        "site": {"lat": LAT, "lon": LON, "tz": TZ},
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "rainviewer": {
            "host": host,
            "frames": frames,
            "note": "Radar frames are analyzed from public RainViewer tiles; ETA is intentionally suppressed when confidence is low.",
        },
        "analysis": {
            "headline": headline,
            "eta_minutes": eta_minutes,
            "confidence": confidence,
            "expected_intensity": intensity,
            "meteorologist_text": meteorologist_text,
            "current_frame": current,
            "motion": motion,
            "frames_analyzed": analyzed,
        },
    }


# -----------------------------
# Flask routes
# -----------------------------

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
        return jsonify({
            "ok": False,
            "error": str(exc),
            "analysis": {
                "headline": "Radar no disponible",
                "eta_minutes": None,
                "confidence": "low",
                "meteorologist_text": "No fue posible leer RainViewer. Usa el radar SACMEX como respaldo visual.",
            },
        }), 502


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
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
