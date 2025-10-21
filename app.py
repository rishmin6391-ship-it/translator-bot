# app.py (최종 안정판)
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from openai import OpenAI
import os, sys

app = Flask(__name__)

# 환경 변수
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
client = OpenAI(api_key=OPENAI_API_KEY)

@app.route("/", methods=["GET"])
def home():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    try:
        user_text = event.message.text.strip()
        print("[User]", user_text)

        # OpenAI 번역 호출
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "너는 번역 도우미야. 입력된 문장을 자연스럽게 번역해."},
                {"role": "user", "content": user_text},
            ],
            timeout=30
        )

        translated = completion.choices[0].message.content.strip()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=translated))
    except Exception as e:
        print("[ERROR]", type(e).__name__, str(e), file=sys.stderr)
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요.")
            )
        except Exception as inner:
            print("[ReplyError]", inner, file=sys.stderr)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
