# app.py (LINE SDK v3 버전)
from flask import Flask, request, abort
import os
import sys

# LINE v3 SDK
from linebot.v3.webhook import WebhookHandler, MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    TextMessage,
    ReplyMessageRequest,
)

# OpenAI
from openai import OpenAI

app = Flask(__name__)

# 환경 변수
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# LINE v3: API 클라이언트 구성 (전역으로 재사용)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(line_config)
messaging_api = MessagingApi(api_client)

# Webhook 핸들러 (시그니처 검증)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# OpenAI 클라이언트
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
    except Exception as e:
        # 시그니처 불일치 등
        print("[Webhook ERROR]", type(e).__name__, str(e), file=sys.stderr)
        abort(400)
    return "OK", 200

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text(event: MessageEvent):
    try:
        user_text = event.message.text.strip()
        print("[User]", user_text)

        # OpenAI 번역 호출
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "너는 번역 도우미야. 입력된 문장을 자연스럽게 번역해.",
                },
                {"role": "user", "content": user_text},
            ],
            timeout=30,
        )
        translated = completion.choices[0].message.content.strip()

        # v3: ReplyMessageRequest 사용
        messaging_api.reply_message(
            ReplyMessageRequest(
                replyToken=event.reply_token,
                messages=[TextMessage(text=translated)],
            )
        )

    except Exception as e:
        print("[ERROR]", type(e).__name__, str(e), file=sys.stderr)
        # 실패 시에도 사용자에게 안내
        try:
            messaging_api.reply_message(
                ReplyMessageRequest(
                    replyToken=event.reply_token,
                    messages=[TextMessage(text="번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요.")],
                )
            )
        except Exception as inner:
            print("[ReplyError]", type(inner).__name__, str(inner), file=sys.stderr)

if __name__ == "__main__":
    # Render 등에서 PORT 환경변수 제공
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
