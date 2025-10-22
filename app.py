# app.py
import os
import re
import logging
from flask import Flask, request

from dotenv import load_dotenv
load_dotenv()  # Render 환경변수도 우선, 로컬 개발 시 .env 사용

# ===== LINE v3 SDK =====
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    JoinEvent,
    FollowEvent,
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)

# ===== OpenAI SDK =====
from openai import OpenAI

# ===== 기본 설정 =====
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
    app.logger.warning("[WARN] LINE 채널 키가 비어 있습니다.")
if not OPENAI_API_KEY:
    app.logger.warning("[WARN] OPENAI_API_KEY가 비어 있습니다.")

# LINE v3: 핸들러/클라이언트
handler = WebhookHandler(LINE_CHANNEL_SECRET)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
# 타임아웃은 gunicorn과 네트워크에서 보장, 필요 시 urllib3.Timeout 연결 가능

# OpenAI 클라이언트
oai = OpenAI(api_key=OPENAI_API_KEY)

# ===== 유틸: 한/태 간단 감지 =====
# (정교한 감지는 모델에 맡기되, 안내 메시지/포맷용으로 간단 사용)
_RE_THAI = re.compile(r"[\u0E00-\u0E7F]")   # 태국어 범위
_RE_KOREAN = re.compile(r"[\uAC00-\uD7A3]") # 한글 범위

def detect_lang(text: str) -> str:
    if _RE_THAI.search(text):
        return "th"
    if _RE_KOREAN.search(text):
        return "ko"
    # 기본값: ko 로 간주
    return "ko"

def translate_ko_th(text: str) -> str:
    """
    한국어 <-> 태국어 양방향 번역 프롬프트 (간결/자연스럽게).
    """
    src = detect_lang(text)
    if src == "ko":
        tgt = "th"
    else:
        tgt = "ko"

    system = (
        "You are a professional translator specializing in Korean↔Thai. "
        "Translate the user's message as naturally as possible. "
        "Do not add explanations—return only the translation."
    )
    user = f"Source language: {src}\nTarget language: {tgt}\nText: {text}"

    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        app.logger.exception("[OpenAI ERROR] %s", e)
        return None

# ===== 헬스체크 =====
@app.get("/")
def health():
    return "OK", 200

# ===== LINE Webhook 엔드포인트 =====
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # 항상 200 반환 (LINE Verify 502 방지)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError as e:
        app.logger.warning("[VERIFY] InvalidSignatureError: %s", e)
    except Exception as e:
        app.logger.exception("[WEBHOOK ERROR] %s", e)
    return "OK", 200

# ===== 핸들러: 방에 초대/친구추가 =====
@handler.add(JoinEvent)
def on_join(event: JoinEvent):
    welcome = (
        "안녕하세요! 한국어↔태국어 통역봇입니다.\n"
        "한국어로 보내면 태국어로, 태국어로 보내면 한국어로 번역해서 답해요. 🥳"
    )
    with ApiClient(line_config) as api_client:
        messaging = MessagingApi(api_client)
        try:
            messaging.reply_message(
                ReplyMessageRequest(
                    replyToken=event.reply_token,
                    messages=[TextMessage(text=welcome)]
                )
            )
        except Exception as e:
            app.logger.exception("[REPLY ERROR / join] %s", e)

@handler.add(FollowEvent)
def on_follow(event: FollowEvent):
    hello = (
        "추가해주셔서 감사합니다! 한국어↔태국어 자동 통역을 도와드려요.\n"
        "메시지를 보내보세요 👋"
    )
    with ApiClient(line_config) as api_client:
        messaging = MessagingApi(api_client)
        try:
            messaging.reply_message(
                ReplyMessageRequest(
                    replyToken=event.reply_token,
                    messages=[TextMessage(text=hello)]
                )
            )
        except Exception as e:
            app.logger.exception("[REPLY ERROR / follow] %s", e)

# ===== 핸들러: 텍스트 메시지 =====
@handler.add(MessageEvent, message=TextMessageContent)
def on_text(event: MessageEvent):
    incoming = event.message.text.strip()
    app.logger.info("[INCOMING] %s", incoming)

    translated = translate_ko_th(incoming)
    if not translated:
        translated = "번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요."

    with ApiClient(line_config) as api_client:
        messaging = MessagingApi(api_client)
        try:
            messaging.reply_message(
                ReplyMessageRequest(
                    replyToken=event.reply_token,
                    messages=[TextMessage(text=translated)]
                )
            )
        except Exception as e:
            app.logger.exception("[REPLY ERROR] %s", e)

# ---- 로컬 실행용 (Render에선 gunicorn이 실행) ----
if __name__ == "__main__":
    # Render는 PORT를 환경변수로 넘겨줌 (없으면 10000)
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
