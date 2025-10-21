# LINE Thai ↔ Korean Translator Bot — Timeout Applied Package

This package includes longer timeouts and retry for LINE Reply API, plus Python 3.11.9 runtime.

## Deploy
1) Upload to GitHub (repo root).
2) Render → New Web Service → connect this repo.
3) Build Command:
   pip install --upgrade pip wheel setuptools && pip install -r requirements.txt
4) Start Command:
   python app.py
5) Env vars: OPENAI_API_KEY, LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, (optional) OPENAI_MODEL
6) Open https://<service>.onrender.com/ → should show 'OK'.
7) Webhook URL: https://<service>.onrender.com/callback → Use webhook ON → Verify.

## Notes
- /callback supports GET 200 (for Verify) and POST (for real events).
- Reply API timeout: 15s + retry 25s if ReadTimeout.
