import hashlib, hmac, json, os, time, urllib.error, urllib.request
from flask import Flask, jsonify, send_from_directory, Response
from flask_cors import CORS

app = Flask(__name__, static_folder='.')
CORS(app)

# Credentials — set as Railway environment variables for security
API_KEY    = os.environ.get('WL_API_KEY',    'nm48qdmixsk1uv4myi9ok3jdtvojjwg5')
API_SECRET = os.environ.get('WL_API_SECRET', 'xqkzyjhthbtwlnhbdqsr1jb4qlyervoy')
STATION_ID = int(os.environ.get('WL_STATION_ID', '238059'))


def sign(params: dict) -> str:
    s = ''.join(k + str(params[k]) for k in sorted(params))
    return hmac.new(API_SECRET.encode(), s.encode(), hashlib.sha256).hexdigest()


def wl_current():
    t      = int(time.time())
    params = {'api-key': API_KEY, 'station-id': str(STATION_ID), 't': t}
    sig    = sign(params)
    url    = (f'https://api.weatherlink.com/v2/current/{STATION_ID}'
              f'?api-key={API_KEY}&t={t}&api-signature={sig}')
    try:
        req = urllib.request.Request(url, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {'error': e.read().decode()}
    except Exception as ex:
        return 502, {'error': str(ex)}


@app.route('/')
def index():
    return send_from_directory('.', 'index.html')


@app.route('/current')
def current():
    status, data = wl_current()
    return jsonify(data), status


@app.route('/manifest.json')
def manifest():
    return send_from_directory('.', 'manifest.json', mimetype='application/manifest+json')


@app.route('/sw.js')
def sw():
    return send_from_directory('.', 'sw.js', mimetype='application/javascript')


@app.route('/icon-<size>.png')
def icon(size):
    return send_from_directory('.', f'icon-{size}.png', mimetype='image/png')


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'station': STATION_ID})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5050))
    app.run(host='0.0.0.0', port=port, debug=False)
