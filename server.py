
import hashlib, hmac, json, math, os, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__, static_folder=".")
CORS(app)

WL_API_KEY = os.environ.get("WL_API_KEY", "").strip()
WL_API_SECRET = os.environ.get("WL_API_SECRET", "").strip()
WL_STATION_ID = os.environ.get("WL_STATION_ID", "238059").strip()

def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"Accept":"application/json","User-Agent":"virreyes-weather/2.1"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))

def sign_weatherlink(params):
    msg = "".join(k + str(params[k]) for k in sorted(params))
    return hmac.new(WL_API_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()

def weatherlink_current_raw():
    if not WL_API_KEY or not WL_API_SECRET or not WL_STATION_ID:
        return 500, {"error":"Missing WeatherLink environment variables"}
    t = int(time.time())
    params = {"api-key":WL_API_KEY, "station-id":WL_STATION_ID, "t":t}
    sig = sign_weatherlink(params)
    query = urllib.parse.urlencode({"api-key":WL_API_KEY, "t":t, "api-signature":sig})
    url = f"https://api.weatherlink.com/v2/current/{WL_STATION_ID}?{query}"
    try:
        return fetch_json(url)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try: body = json.loads(body)
        except Exception: body = {"error":body}
        return e.code, body
    except Exception as exc:
        return 502, {"error":str(exc)}

def first_number(*values):
    for v in values:
        if isinstance(v, (int, float)):
            return float(v)
    return None

def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)): out.update(flatten(v, key))
            else: out[key] = v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}.{i}" if prefix else str(i)))
    return out

def f_to_c(v): return None if v is None else (v - 32.0) * 5.0 / 9.0
def mph_to_kmh(v): return None if v is None else v * 1.609344
def inhg_to_hpa(v): return None if v is None else v * 33.8638866667
def inch_to_mm(v): return None if v is None else v * 25.4

def rain_score(key, source):
    k = key.lower()
    s = 0
    if any(x in k for x in ["today","day","daily"]): s += 100
    if "storm" in k: s += 40
    if any(x in k for x in ["month","year","season"]): s -= 100
    if "rate" in k: s -= 100
    if source == "record": s += 10
    return s

def extract_rain_day_mm(raw, records):
    candidates = []
    explicit_mm = ["rain_day_mm","rainfall_daily_mm","rainfall_day_mm","rain_daily_mm","rain_today_mm","rainfall_today_mm"]
    explicit_in = ["rain_day_in","rainfall_daily_in","rainfall_day_in","rain_daily_in","rain_today_in","rainfall_today_in"]
    generic = ["rain_day","rainfall_daily","rainfall_day","rain_daily","rain_today","rainfall_today","daily_rain","day_rain"]
    for d in records:
        for name in explicit_mm:
            if isinstance(d.get(name),(int,float)): candidates.append((name, float(d[name]), "mm", "record"))
        for name in explicit_in:
            if isinstance(d.get(name),(int,float)): candidates.append((name, inch_to_mm(float(d[name])), "in->mm", "record"))
        for name in generic:
            if isinstance(d.get(name),(int,float)):
                v = float(d[name])
                candidates.append((name, inch_to_mm(v) if 0 < v <= 2 else v, "generic", "record"))

    for key, val in flatten(raw).items():
        if not isinstance(val, (int,float)): continue
        leaf = key.lower().split(".")[-1]
        is_rain = any(x in leaf for x in ["rain","rainfall","precip"])
        is_daily = any(x in leaf for x in ["today","day","daily"])
        is_rate = "rate" in leaf or "last" in leaf
        if is_rain and is_daily and not is_rate:
            if "mm" in leaf:
                candidates.append((key, float(val), "mm", "flat"))
            elif "_in" in leaf or "inch" in leaf:
                candidates.append((key, inch_to_mm(float(val)), "in->mm", "flat"))
            else:
                v = float(val)
                candidates.append((key, inch_to_mm(v) if 0 < v <= 2 else v, "generic", "flat"))

    candidates = [c for c in candidates if c[1] is not None and c[1] >= 0]
    if not candidates: return None, []
    candidates.sort(key=lambda c: rain_score(c[0], c[3]), reverse=True)
    return candidates[0][1], candidates[:12]

def extract_rain_rate_mm(raw, records):
    candidates = []
    for d in records:
        for name in ["rain_rate_mm","rain_rate_mm_h","rainfall_rate_mm"]:
            if isinstance(d.get(name),(int,float)): candidates.append((name, float(d[name]), "mm", "record"))
        for name in ["rain_rate_in","rain_rate_last_in","rain_rate_hi_in"]:
            if isinstance(d.get(name),(int,float)): candidates.append((name, inch_to_mm(float(d[name])), "in->mm", "record"))
    for key, val in flatten(raw).items():
        if isinstance(val, (int,float)):
            leaf = key.lower().split(".")[-1]
            if any(x in leaf for x in ["rain","rainfall","precip"]) and "rate" in leaf:
                candidates.append((key, float(val) if "mm" in leaf else inch_to_mm(float(val)), "mm" if "mm" in leaf else "in->mm", "flat"))
    candidates = [c for c in candidates if c[1] is not None and c[1] >= 0]
    return (candidates[0][1], candidates[:12]) if candidates else (None, [])

def normalize_weatherlink(raw):
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
        "temperature_c": None if temp_c is None else round(temp_c,1),
        "humidity_pct": None if hum is None else round(hum),
        "dew_point_c": None if dew_c is None else round(dew_c,1),
        "wind_speed_kmh": None if wind is None else round(mph_to_kmh(wind),1),
        "wind_direction_deg": None if wind_dir is None else round(wind_dir),
        "pressure_hpa": None if pressure_hpa is None else round(pressure_hpa,1),
        "rain_day_mm": None if rain_day_mm is None else round(rain_day_mm,1),
        "rain_rate_mm_h": None if rain_rate_mm is None else round(rain_rate_mm,1),
        "debug_rain_day_candidates": rain_day_candidates,
        "debug_rain_rate_candidates": rain_rate_candidates
    }

@app.after_request
def add_no_cache_headers(response):
    if response.content_type and ("application/json" in response.content_type or "text/html" in response.content_type):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response

@app.route("/")
def index(): return send_from_directory(".", "index.html")

@app.route("/api/current")
@app.route("/current")
def current():
    status, raw = weatherlink_current_raw()
    if status != 200:
        return jsonify({"ok":False, "source":"weatherlink", "status":status, "error":raw}), status
    return jsonify({"ok":True, "source":"weatherlink", "parsed":normalize_weatherlink(raw), "raw":raw})

@app.route("/api/health")
@app.route("/health")
def health():
    return jsonify({"ok":True, "station_id":WL_STATION_ID, "has_api_key":bool(WL_API_KEY), "has_api_secret":bool(WL_API_SECRET)})

@app.route("/manifest.json")
def manifest(): return send_from_directory(".", "manifest.json", mimetype="application/manifest+json")

@app.route("/sw.js")
def sw(): return send_from_directory(".", "sw.js", mimetype="application/javascript")

@app.route("/icon-<int:size>.png")
def icon(size): return send_from_directory(".", f"icon-{size}.png", mimetype="image/png")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5050)))
