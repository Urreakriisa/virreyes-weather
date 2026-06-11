
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

SITE_LAT = float(os.environ.get("SITE_LAT", "19.4169"))
SITE_LON = float(os.environ.get("SITE_LON", "-99.2176"))
SITE_TZ = os.environ.get("SITE_TZ", "America/Mexico_City")

WL_API_KEY = os.environ.get("WL_API_KEY", "").strip()
WL_API_SECRET = os.environ.get("WL_API_SECRET", "").strip()
WL_STATION_ID = os.environ.get("WL_STATION_ID", "238059").strip()


def fetch_bytes(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "virreyes-weather/4.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url, timeout=15):
    return json.loads(fetch_bytes(url, timeout).decode("utf-8"))


def safe_round(v, n=1):
    return None if v is None else round(float(v), n)


def f_to_c(v):
    return None if v is None else (v - 32.0) * 5.0 / 9.0


def mph_to_kmh(v):
    return None if v is None else v * 1.609344


def inhg_to_hpa(v):
    return None if v is None else v * 33.8638866667


def inch_to_mm(v):
    return None if v is None else v * 25.4


def compass(deg):
    if deg is None:
        return "--"
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((float(deg) + 11.25) / 22.5) % 16]


def first_number(*vals):
    for v in vals:
        if isinstance(v, (int, float)):
            return float(v)
    return None


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def destination_point(lat, lon, bearing, distance_km):
    r = 6371.0
    brng = math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    d = distance_km / r
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(brng))
    l2 = l1 + math.atan2(math.sin(brng) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), math.degrees(l2)


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


def sign_weatherlink(params):
    msg = "".join(k + str(params[k]) for k in sorted(params))
    return hmac.new(WL_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


def weatherlink_current_raw():
    if not WL_API_KEY or not WL_API_SECRET or not WL_STATION_ID:
        return 500, {"error": "Missing WL_API_KEY, WL_API_SECRET, or WL_STATION_ID."}

    t = int(time.time())
    params = {"api-key": WL_API_KEY, "station-id": WL_STATION_ID, "t": t}
    sig = sign_weatherlink(params)
    q = urllib.parse.urlencode({"api-key": WL_API_KEY, "t": t, "api-signature": sig})
    url = f"https://api.weatherlink.com/v2/current/{WL_STATION_ID}?{q}"

    try:
        return 200, fetch_json(url)
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", errors="replace")}
    except Exception as exc:
        return 502, {"error": str(exc)}


def extract_rain_day_mm(raw, records):
    candidates = []
    for d in records:
        for name in ["rainfall_daily_mm", "rain_day_mm", "rain_today_mm", "rain_daily_mm"]:
            if isinstance(d.get(name), (int, float)):
                candidates.append((name, float(d[name]), "mm"))
        for name in ["rainfall_daily_in", "rain_day_in", "rain_today_in", "rain_daily_in"]:
            if isinstance(d.get(name), (int, float)):
                candidates.append((name, inch_to_mm(float(d[name])), "in"))

    for key, val in flatten(raw).items():
        if not isinstance(val, (int, float)):
            continue
        leaf = key.lower().split(".")[-1]
        if "clicks" in leaf:
            continue
        if ("rain" in leaf or "rainfall" in leaf) and any(x in leaf for x in ["daily", "day", "today"]) and "rate" not in leaf:
            if "mm" in leaf:
                candidates.append((key, float(val), "mm"))
            elif "_in" in leaf or leaf.endswith("in"):
                candidates.append((key, inch_to_mm(float(val)), "in"))

    candidates = [c for c in candidates if c[1] is not None and c[1] >= 0]
    candidates.sort(
        key=lambda c: (100 if any(x in c[0].lower() for x in ["daily", "today", "day"]) else 0) + (10 if c[2] == "mm" else 0),
        reverse=True,
    )
    return candidates[0][1] if candidates else None


def extract_rain_rate_mm(records):
    for d in records:
        for name in ["rain_rate_last_mm", "rain_rate_mm", "rain_rate_hi_mm"]:
            if isinstance(d.get(name), (int, float)):
                return float(d[name])
        for name in ["rain_rate_last_in", "rain_rate_in", "rain_rate_hi_in"]:
            if isinstance(d.get(name), (int, float)):
                return inch_to_mm(float(d[name]))
    return None


def normalize_weatherlink(raw):
    records = []
    for sensor in raw.get("sensors", []):
        for datum in sensor.get("data", []):
            if isinstance(datum, dict):
                records.append(datum)

    temp = hum = dew = wind = wind_dir = pressure = ts = None
    rain_24 = rain_60 = None

    for d in records:
        temp = first_number(temp, d.get("temp"))
        hum = first_number(hum, d.get("hum"))
        dew = first_number(dew, d.get("dew_point"))
        wind = first_number(wind, d.get("wind_speed_last"), d.get("wind_speed_avg_last_1_min"))
        wind_dir = first_number(wind_dir, d.get("wind_dir_last"), d.get("wind_dir_scalar_avg_last_1_min"))
        pressure = first_number(pressure, d.get("bar_sea_level"), d.get("bar_absolute"))
        ts = first_number(ts, d.get("ts"))
        rain_24 = first_number(
            rain_24,
            d.get("rainfall_last_24_hr_mm"),
            inch_to_mm(d.get("rainfall_last_24_hr_in")) if isinstance(d.get("rainfall_last_24_hr_in"), (int, float)) else None,
        )
        rain_60 = first_number(
            rain_60,
            d.get("rainfall_last_60_min_mm"),
            inch_to_mm(d.get("rainfall_last_60_min_in")) if isinstance(d.get("rainfall_last_60_min_in"), (int, float)) else None,
        )

    temp_c = f_to_c(temp) if temp is not None and temp > 45 else temp
    dew_c = f_to_c(dew) if dew is not None and dew > 45 else dew
    pressure_hpa = inhg_to_hpa(pressure) if pressure is not None and pressure < 100 else pressure

    return {
        "station_id": WL_STATION_ID,
        "updated_utc": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else datetime.now(timezone.utc).isoformat(),
        "temperature_c": safe_round(temp_c, 1),
        "humidity_pct": safe_round(hum, 0),
        "dew_point_c": safe_round(dew_c, 1),
        "wind_speed_kmh": safe_round(mph_to_kmh(wind), 1) if wind is not None else None,
        "wind_direction_deg": safe_round(wind_dir, 0),
        "wind_direction_compass": compass(wind_dir),
        "pressure_hpa": safe_round(pressure_hpa, 1),
        "rain_day_mm": safe_round(extract_rain_day_mm(raw, records), 1),
        "rain_rate_mm_h": safe_round(extract_rain_rate_mm(records), 1),
        "rain_last_60_min_mm": safe_round(rain_60, 1),
        "rain_last_24_hr_mm": safe_round(rain_24, 1),
    }


def get_openmeteo():
    params = {
        "latitude": SITE_LAT,
        "longitude": SITE_LON,
        "timezone": SITE_TZ,
        "forecast_days": 3,
        "past_days": 1,
        "hourly": "temperature_2m,precipitation,precipitation_probability,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
    }
    return fetch_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params), 20)


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
    if sat < 35 or (r > 220 and g > 220 and b > 220) or (r < 25 and g < 25 and b < 25):
        return 0
    return min(255, int(0.65 * mx + 0.35 * sat))


def analyze_radar_frame(host, path, z=7, radius_km=90):
    cx, cy = lonlat_to_global_pixel(SITE_LAT, SITE_LON, z)
    tile_size = 512
    tx, ty = int(cx // tile_size), int(cy // tile_size)
    ox, oy = tx - 1, ty - 1
    mosaic = Image.new("RGBA", (tile_size * 3, tile_size * 3), (0, 0, 0, 0))

    for dx in range(3):
        for dy in range(3):
            url = f"{host}{path}/512/{z}/{ox + dx}/{oy + dy}/2/1_1.png"
            try:
                img = Image.open(BytesIO(fetch_bytes(url, 10))).convert("RGBA")
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

            lat, lon = global_pixel_to_lonlat(ox * tile_size + px, oy * tile_size + py, z)
            d = haversine_km(SITE_LAT, SITE_LON, lat, lon)
            if d > radius_km:
                continue

            wet += 1
            strong += 1 if inten > 150 else 0
            local += 1 if d <= 8 else 0
            max_i = max(max_i, inten)

            if nearest is None or d < nearest:
                nearest, nearest_ll = d, (lat, lon)

            w = 1 + inten / 255
            wx += px * w
            wy += py * w
            tw += w

            if len(points) < 240 and inten > 95:
                points.append({"lat": safe_round(lat, 4), "lon": safe_round(lon, 4), "intensity": int(inten), "distance_km": safe_round(d, 1)})

    if tw == 0:
        return {
            "has_echo": False,
            "coverage_score": 0,
            "strong_score": 0,
            "local_rain": False,
            "nearest_echo_km": None,
            "nearest_echo": None,
            "centroid": None,
            "max_intensity": 0,
            "echo_points": [],
        }

    clat, clon = global_pixel_to_lonlat(ox * tile_size + wx / tw, oy * tile_size + wy / tw, z)
    return {
        "has_echo": True,
        "coverage_score": safe_round(min(1, wet / 1200), 3),
        "strong_score": safe_round(strong / max(1, wet), 2),
        "local_rain": bool(local > 2),
        "nearest_echo_km": safe_round(nearest, 1),
        "nearest_echo": None if nearest_ll is None else {"lat": safe_round(nearest_ll[0], 4), "lon": safe_round(nearest_ll[1], 4)},
        "centroid": {"lat": safe_round(clat, 4), "lon": safe_round(clon, 4)},
        "centroid_distance_km": safe_round(haversine_km(SITE_LAT, SITE_LON, clat, clon), 1),
        "centroid_bearing_deg": safe_round(bearing_deg(SITE_LAT, SITE_LON, clat, clon), 0),
        "centroid_compass_from_site": compass(bearing_deg(SITE_LAT, SITE_LON, clat, clon)),
        "max_intensity": int(max_i),
        "echo_points": points,
    }


def build_arrows_from_points(points, direction_deg, max_arrows=12):
    if not points:
        return []

    # Prefer points 8–80 km away so arrows are visible on map, not stacked on the home marker.
    candidates = [p for p in points if 8 <= p.get("distance_km", 0) <= 80]
    candidates.sort(key=lambda p: (-p.get("intensity", 0), p.get("distance_km", 999)))

    selected = []
    min_sep_km = 9.0
    for p in candidates:
        if len(selected) >= max_arrows:
            break
        if all(haversine_km(p["lat"], p["lon"], q["lat"], q["lon"]) >= min_sep_km for q in selected):
            selected.append(p)

    arrows = []
    for p in selected:
        end_lat, end_lon = destination_point(p["lat"], p["lon"], direction_deg, 9)
        arrows.append({
            "start": {"lat": p["lat"], "lon": p["lon"]},
            "end": {"lat": safe_round(end_lat, 4), "lon": safe_round(end_lon, 4)},
            "intensity": p.get("intensity", 120),
        })
    return arrows


def radar_nowcast():
    maps = fetch_json("https://api.rainviewer.com/public/weather-maps.json", 15)
    host = maps.get("host", "https://tilecache.rainviewer.com")
    frames = maps.get("radar", {}).get("past", [])[-6:]
    analyzed = []

    for frame in frames[-4:]:
        item = analyze_radar_frame(host, frame["path"], 7)
        item["time"] = frame.get("time")
        item["time_iso"] = datetime.fromtimestamp(frame["time"], tz=timezone.utc).isoformat() if frame.get("time") else None
        analyzed.append(item)

    current = analyzed[-1] if analyzed else {"has_echo": False, "echo_points": []}
    previous = next((x for x in reversed(analyzed[:-1]) if x.get("has_echo") and current.get("has_echo")), None)

    eta = None
    confidence = "low"
    motion = None
    arrows = []

    # Calculate radar-derived motion when possible.
    if current.get("has_echo") and previous and previous.get("centroid") and current.get("centroid"):
        p, c = previous["centroid"], current["centroid"]
        dt_h = max(1 / 60, (current["time"] - previous["time"]) / 3600)
        speed = haversine_km(p["lat"], p["lon"], c["lat"], c["lon"]) / dt_h
        direction = bearing_deg(p["lat"], p["lon"], c["lat"], c["lon"])
        to_site = bearing_deg(c["lat"], c["lon"], SITE_LAT, SITE_LON)
        angle = abs((direction - to_site + 180) % 360 - 180)
        dist = haversine_km(c["lat"], c["lon"], SITE_LAT, SITE_LON)
        closing = speed * math.cos(math.radians(angle))
        plausible = 5 <= speed <= 95

        # If centroid jump is too slow/fast, still draw a cautious default W→E arrow field.
        arrow_direction = direction if plausible else 105

        motion = {
            "speed_kmh": safe_round(speed, 1),
            "direction_deg": safe_round(arrow_direction, 0),
            "direction_compass": compass(arrow_direction),
            "angle_to_site_deg": safe_round(angle, 0),
            "closing_speed_kmh": safe_round(closing, 1),
            "plausible": plausible,
        }

        if plausible and closing > 8 and dist < 90:
            eta = int(max(5, min(120, (dist / closing) * 60)))
            confidence = "medium"

        arrows = build_arrows_from_points(current.get("echo_points", []), arrow_direction)

    # Fallback: always return visible arrows when there are echoes.
    if current.get("has_echo") and not arrows:
        default_direction = current.get("centroid_bearing_deg")
        # If storm is west/northwest of site, typical movement is often E/SE across CDMX.
        default_direction = 105 if default_direction is None or 240 <= float(default_direction) <= 320 else 135
        arrows = build_arrows_from_points(current.get("echo_points", []), default_direction)
        motion = motion or {
            "speed_kmh": None,
            "direction_deg": safe_round(default_direction, 0),
            "direction_compass": compass(default_direction),
            "angle_to_site_deg": None,
            "closing_speed_kmh": None,
            "plausible": False,
        }

    if current.get("local_rain"):
        headline = "Lluvia sobre Virreyes o muy cerca"
        eta = 0
        confidence = "high"
    elif current.get("has_echo"):
        headline = f"Lluvia y tormentas a {current.get('nearest_echo_km')} km alrededor de Virreyes"
    else:
        headline = "Sin ecos de lluvia cercanos"

    if current.get("local_rain") or current.get("strong_score", 0) > 0.25 or current.get("max_intensity", 0) > 180:
        intensity = "fuerte"
    elif current.get("coverage_score", 0) > 0.10 or current.get("max_intensity", 0) > 120:
        intensity = "moderada"
    elif current.get("has_echo"):
        intensity = "ligera"
    else:
        intensity = "ninguna"

    return {
        "ok": True,
        "site": {"lat": SITE_LAT, "lon": SITE_LON, "label": "Tu ubicación · Lomas Virreyes"},
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "rainviewer": {"host": host, "frames": frames, "native_zoom": 7},
        "analysis": {
            "headline": headline,
            "eta_minutes": eta,
            "confidence": confidence,
            "expected_intensity": intensity,
            "meteorologist_text": "Las flechas indican dirección estimada del desplazamiento usando radar reciente. Si el vector no es confiable, se muestra una guía conservadora de desplazamiento.",
            "current_frame": current,
            "motion": motion,
            "storm_arrows": arrows,
            "frames_analyzed": analyzed,
        },
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
        return jsonify({"ok": False, "status": status, "error": raw}), status
    return jsonify({"ok": True, "parsed": normalize_weatherlink(raw), "raw": raw})


@app.route("/api/forecast")
def forecast():
    return jsonify({"ok": True, "data": get_openmeteo()})


@app.route("/api/radar-nowcast")
def nowcast():
    return jsonify(radar_nowcast())


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
