# app.py (LINE SDK v3, Flask + OpenAI)
import os, sys, logging
from flask import Flask, request, abort

# --- LINE v3 SDK ---
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    JoinEvent,
    FollowEvent,
)

# --- OpenAI ---
from openai import OpenAI

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# 환경 변수
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# LINE v3 객체
handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

# OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

@app.route("/", methods=["GET"])
def home():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info("[EVENT IN] %s", body)

    try:
        handler.handle(body, signature)
    except Exception as e:
        app.logger.exception("Webhook handle error")
        abort(400)

    return "OK", 200

# ====== 핸들러들 (클래스 기반) ======

@handler.add(MessageEvent)
def on_message(event):
    # 텍스트만 처리
    if not isinstance(event.message, TextMessageContent):
        return

    user_text = (event.message.text or "").strip()
    app.logger.info("[MESSAGE] %s", user_text)

    # OpenAI 번역 호출
    try:
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": "너는 번역 도우미야. 입력된 문장을 자연스럽고 간결하게 번역해. 번역문만 답해.",
                },
                {"role": "user", "content": user_text},
            ],
            timeout=25,  # replyToken 만료 전에 끝내기
        )
        translated = (completion.choices[0].message.content or "").strip()
    except Exception:
        app.logger.exception("OpenAI error")
        translated = "번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요."

    # LINE 응답
    try:
        with ApiClient(configuration) as api_client:
            messaging_api = MessagingApi(api_client)
            messaging_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=translated)],
                )
            )
    except Exception:
        app.logger.exception("Reply error")

@handler.add(JoinEvent)
def on_join(event):
    # 그룹에 초대되었을 때 간단 안내 (필요 없으면 제거 가능)
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="초대해 주셔서 감사합니다! 메시지를 보내면 번역해 드릴게요.")]
                )
            )
    except Exception:
        app.logger.exception("Join reply error")

@handler.add(FollowEvent)
def on_follow(event):
    # 1:1 친구추가 인사
    try:
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="추가해 주셔서 감사합니다! 메시지를 보내면 번역해 드릴게요.")]
                )
            )
    except Exception:
        app.logger.exception("Follow reply error")

if __name__ == "__main__":
    # 개발 로컬용; Render에서는 Gunicorn으로 실행
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
