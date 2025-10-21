# PATCH NOTE
- Fix: line-bot-sdk v2 expects `http_client` **class**, not instance.
  So, use:
      line_bot_api = LineBotApi(
          LINE_CHANNEL_ACCESS_TOKEN,
          http_client=RequestsHttpClient,  # pass CLASS
          timeout=(15, 60),
      )
- Ensure Render Start Command uses gunicorn (NOT `python app.py`).