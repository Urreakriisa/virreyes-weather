# Estación Virreyes - Weather App

Personal weather dashboard for Virreyes, Miguel Hidalgo, CDMX.

## Railway variables

Set these in Railway -> Project -> Variables:

- WL_API_KEY
- WL_API_SECRET
- WL_STATION_ID

Do not commit API keys or API secrets to GitHub.

## Local development

```bash
pip install -r requirements.txt
export WL_API_KEY="your_key"
export WL_API_SECRET="your_secret"
export WL_STATION_ID="238059"
python server.py
```

Open http://localhost:5050
