# LINE Thai ↔ Korean Translator Bot (Render)

This is a ready-to-deploy Flask app that turns your LINE Official Account into a KO↔TH auto-translation bot using OpenAI.

## Quick Start (Render)

1. Fork or upload this repo to GitHub.
2. On Render: **New + → Web Service** → pick this repo.
3. Settings:
   - Environment: **Python**
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python app.py`
4. Add **Environment Variables** in Render:
   - `OPENAI_API_KEY`: your OpenAI API key
   - `LINE_CHANNEL_ACCESS_TOKEN`: from LINE Developers
   - `LINE_CHANNEL_SECRET`: from LINE Developers
   - (Optional) `OPENAI_MODEL`: defaults to `gpt-4o-mini`
5. Deploy. After it's live, note the URL (e.g., `https://translator-bot.onrender.com`).
6. In LINE Developers Console → **Messaging API** tab:
   - Webhook URL: `https://<your-render-url>/callback`
   - Use webhook: **ON**
   - Verify: should return 200 OK

## Local Test (optional)
```
pip install -r requirements.txt
python app.py
```
Use a tunneling tool (e.g., ngrok) for external access during local tests:
```
ngrok http 3000
Webhook URL → https://<ngrok-id>.ngrok.io/callback
```

## Commands (optional)
- `/ko <text>` → force translate to Korean
- `/th <text>` → force translate to Thai

## Notes
- Make sure to enable Messaging API from your LINE Official Account Manager.
- Ensure HTTPS and correct `/callback` path.
- Check Render **Logs** if you see errors.
