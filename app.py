# app.py — Production-tuned (gunicorn) for Render
import os, re, sys, traceback
from typing import List
from dotenv import load_dotenv
from flask import Flask, request, abort

# LINE SDK v2 (안정 버전)
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, JoinEvent
from linebot.http_client import RequestsHttpClient
from requests.exceptions import ReadTimeout

# OpenAI SDK (>=1.51,<2)
from openai import OpenAI

load_dotenv()

# ====== 환경 변수 ======
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise RuntimeError("Missing LINE credentials (LINE_CHANNEL_ACCESS_TOKEN/LINE_CHANNEL_SECRET).")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY.")

# 네트워크 타임아웃 (connect, read)
HTTP_TIMEOUT = (15, 60)  # 여유 있게 증가
# LINE API 클라이언트 (Requests 기반, timeout 주입)
http_client = RequestsHttpClient(timeout=HTTP_TIMEOUT)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN, http_client=http_client)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# OpenAI 클라이언트
client = OpenAI(api_key=OPENAI_API_KEY)

# ====== 간단한 스크립트 기반 언어 판별 ======
HANGUL_RE = re.compile(r"[\u3131-\uD79D]+")
THAI_RE   = re.compile(r"[\u0E00-\u0E7F]+")

def decide_target_lang(text: str) -> str:
    has_ko = bool(HANGUL_RE.search(text))
    has_th = bool(THAI_RE.search(text))
    if has_ko and not has_th:
        return "THAI"
    if has_th and not has_ko:
        return "KOREAN"
    return "KOREAN"  # 모호하면 한국어

SYSTEM_PROMPT = (
    "You are a precise, friendly translator for casual LINE chats between Korean and Thai speakers.\n"
    "- Detect the source language.\n"
    "- If the message contains Korean, translate it into NATURAL THAI suitable for friendly chat.\n"
    "- If the message contains Thai, translate it into NATURAL KOREAN (friendly tone).\n"
    "- If neither script is clear, default target to Korean.\n"
    "- Preserve emojis, names, and intent; adapt idioms to sound native.\n"
    "- Return ONLY the translation text."
)

def translate_ko_th(text: str) -> str:
    target = decide_target_lang(text)
    hint = f"Target language: {target}."
    print("[TRANSLATE] target:", target, file=sys.stderr)
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{hint}\n{text}"},
        ],
        temperature=0.2,
        timeout=60,  # OpenAI 요청 타임아웃 (느린 시간대 대비)
    )
    return resp.choices[0].message.content.strip()

def chunk_text(s: str, limit: int = 4500) -> List[str]:
    return [s[i:i+limit] for i in range(0, len(s), limit)]

# ====== Flask ======
app = Flask(__name__)

@app.route("/", methods=["GET", "HEAD"])
def health():
    # 헬스체크/핑용
    print("[BOOT] Python:", sys.version, file=sys.stderr)
    print("[BOOT] HTTP_TIMEOUT:", HTTP_TIMEOUT, file=sys.stderr)
    return "OK", 200

@app.route("/callback", methods=["GET", "POST"])
def callback():
    if request.method == "GET":
        # 일부 Verify/모니터링이 GET으로 접근할 수 있으므로 200 제공
        return "OK", 200

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid signature")
    except Exception as e:
        print("[WEBHOOK ERROR]", type(e).__name__, str(e), file=sys.stderr)
        traceback.print_exc()
    return "OK"

@handler.add(JoinEvent)
def handle_join(event: JoinEvent):
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="안녕하세요! 한국어↔태국어 자동 번역 봇이에요. 편하게 말해 보세요 😊")
        )
    except Exception as e:
        print("[JOIN ERROR]", type(e).__name__, str(e), file=sys.stderr)
        traceback.print_exc()

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event: MessageEvent):
    user_text = event.message.text.strip()

    # 강제 명령 지원 (/ko, /th)
    forced = None
    if user_text.startswith("/ko "):
        forced = "KOREAN"; user_text = user_text[4:]
    elif user_text.startswith("/th "):
        forced = "THAI"; user_text = user_text[4:]

    try:
        if forced:
            forced_prompt = SYSTEM_PROMPT + f" Translate STRICTLY into {forced}."
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": forced_prompt},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.2,
                timeout=60,
            )
            translated = resp.choices[0].message.content.strip()
        else:
            translated = translate_ko_th(user_text)
    except Exception as e:
        print("[OpenAI ERROR]", type(e).__name__, str(e), file=sys.stderr)
        traceback.print_exc()
        translated = "번역 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요."

    parts = chunk_text(translated)
    messages = [TextSendMessage(text=p) for p in parts]

    try:
        # 1차 시도
        line_bot_api.reply_message(event.reply_token, messages, timeout=HTTP_TIMEOUT)
    except ReadTimeout:
        print("[LINE REPLY TIMEOUT] retry with (15, 75)", file=sys.stderr)
        # 2차 시도 (read timeout 더 확장)
        try:
            line_bot_api.reply_message(event.reply_token, messages, timeout=(15, 75))
        except Exception as e2:
            print("[LINE REPLY ERROR/RETRY]", type(e2).__name__, str(e2), file=sys.stderr)
            traceback.print_exc()
    except Exception as e:
        print("[LINE REPLY ERROR]", type(e).__name__, str(e), file=sys.stderr)
        traceback.print_exc()

# gunicorn이 app:app을 import하여 실행합니다.
