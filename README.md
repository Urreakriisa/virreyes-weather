# Estacion Virreyes - Weather App

Personal weather station dashboard for Virreyes, Miguel Hidalgo, CDMX.
Davis WeatherLink station ID 238059 + Open-Meteo forecast + RainViewer radar.

## Deploy to Railway (free)

### Step 1 - Create a GitHub account
Go to https://github.com and sign up (free). Skip if you already have one.

### Step 2 - Create a new GitHub repository
1. Click the + icon (top right) -> New repository
2. Name it: virreyes-weather
3. Set to Public
4. Click "Create repository"

### Step 3 - Upload the files
1. On the new repo page click "uploading an existing file"
2. Drag and drop ALL files from this folder:
   - server.py
   - index.html
   - manifest.json
   - sw.js
   - icon-192.png
   - icon-512.png
   - requirements.txt
   - Procfile
3. Click "Commit changes"

### Step 4 - Deploy on Railway
1. Go to https://railway.app and click "Start a New Project"
2. Sign in with GitHub
3. Click "Deploy from GitHub repo"
4. Select your virreyes-weather repository
5. Railway detects Python automatically and starts deploying
6. Wait ~2 minutes for the build to finish
7. Click "Generate Domain" to get your public URL
   (looks like: virreyes-weather-production.up.railway.app)

### Step 5 - Add to iPhone home screen
1. On your iPhone, open Safari (must be Safari, not Chrome)
2. Go to your Railway URL
3. Tap the Share button (box with arrow pointing up)
4. Scroll down and tap "Add to Home Screen"
5. Name it "Virreyes" and tap Add
6. The app icon appears on your home screen!

### Step 6 (optional) - Secure your API keys
In Railway dashboard -> your project -> Variables, add:
  WL_API_KEY    = nm48qdmixsk1uv4myi9ok3jdtvojjwg5
  WL_API_SECRET = xqkzyjhthbtwlnhbdqsr1jb4qlyervoy
  WL_STATION_ID = 238059

Then remove the hardcoded values from server.py (replace with empty strings).

## Local development
Run the server locally:
  pip install flask flask-cors gunicorn
  python server.py
Then open http://localhost:5050

## Notes
- Railway free tier: 500 hours/month (enough for 24/7 use)
- The app works offline showing last cached data
- Auto-refreshes live Davis station data on every load
