# Estación Virreyes - Nowcast Meteorológico

Weather dashboard for Virreyes, Miguel Hidalgo, CDMX.

## What changed

This version separates three different products:

1. **Observed local weather** from your Davis WeatherLink station.
2. **Observed radar** from RainViewer public radar tiles, animated on a map.
3. **Forecast context** from Open-Meteo.

The rain ETA is deliberately conservative. It is only shown when radar echoes are detected and the recent radar motion is moving toward Virreyes. Otherwise, the app says **"Sin ETA confiable"**.

## Railway variables

Set these in Railway -> Project -> Variables:

- `WL_API_KEY`
- `WL_API_SECRET`
- `WL_STATION_ID`
- Optional: `SITE_LAT`, `SITE_LON`, `SITE_TZ`

Do not commit secrets to GitHub.
