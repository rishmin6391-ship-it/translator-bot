# app.py — v3 SDK, 네이티브 톤 + 번역 방향 라벨
import os
import re
import sys
import json
from flask import Flask, request, abort

# LINE v3 SDK
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)

# OpenAI
from openai import OpenAI

app = Flask(__name__)

# ===== ENV =====
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and OPENAI_API_KEY):
    print("[FATAL] Missing environment variables.", file=sys.stderr)
    sys.exit(1)

# ===== Clients =====
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
oai = OpenAI(api_key=OPENAI_API_KEY)

# ===== Helpers =====
RE_THAI   = re.compile(r"[\u0E00-\u0E7F]")                 # 태국어
RE_HANGUL = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]")  # 한글(자모+완성형)

def detect_lang(text: str):
    """간단/확정적인 문자 범위 기반 감지 (모델에 의존 X)."""
    if RE_THAI.search(text):
        return "th"
    if RE_HANGUL.search(text):
        return "ko"
    return None

def build_system_prompt(src: str, tgt: str):
    """
    현지인 톤 지시.
    - 의미 정확히 전달, 부자연스러운 직역 금지
    - 원문의 존댓말/반말·말투를 최대한 보존
    - 이모지/구어체/인터넷 슬랭은 자연스럽게 대응
    - 불필요한 설명/따옴표/접두사 금지 (번역문만)
    """
    if src == "ko" and tgt == "th":
        return (
            "역할: 한->태 통역사.\n"
            "원문의 뉘앙스·존댓말/반말을 유지하되, 태국 현지인이 쓰는 자연스러운 구어체로 번역해.\n"
            "사투리/은어는 태국에서 통하는 자연스러운 표현으로 옮겨.\n"
            "번역문만 답하고, 추가 설명은 하지 마."
        )
    if src == "th" and tgt == "ko":
        return (
            "역할: 태->한 통역사.\n"
            "원문의 뉘앙스·존댓말/반말을 유지하되, 한국인이 쓰는 자연스러운 구어체로 번역해.\n"
            "태국식 표현은 한국어에서 어색하지 않게 자연스럽게 바꿔.\n"
            "번역문만 답하고, 추가 설명은 하지 마."
        )
    return "입력 문장을 자연스럽고 정확하게 번역해. 번역문만 답해."

def translate_native(user_text: str):
    src = detect_lang(user_text)
    if src == "ko":
        tgt = "th"
        tag = "🇰🇷→🇹🇭"
    elif src == "th":
        tgt = "ko"
        tag = "🇹🇭→🇰🇷"
    else:
        # 지원 외 언어 또는 감지 실패
        return None, (
            "지원 언어는 한국어/태국어입니다.\n"
            "• 한국어 → 태국어\n• 태국어 → 한국어\n"
            "해당 언어로 다시 보내주세요."
        )

    system_prompt = build_system_prompt(src, tgt)

    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            timeout=30,
        )
        out = (resp.choices[0].message.content or "").strip()
        return tag, out
    except Exception as e:
        print("[OpenAI ERROR]", repr(e), file=sys.stderr)
        return None, "번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요."

# ===== Routes =====
@app.route("/", methods=["GET"])
def home():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        # 디버깅용 간단 로그
        app.logger.info("[EVENT IN] %s", body)
        handler.handle(body, signature)
    except Exception as e:
        print("[Webhook ERROR]", repr(e), file=sys.stderr)
        abort(400)
    return "OK", 200

# ===== Handlers =====
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()
    app.logger.info("[MESSAGE] %s", user_text)

    tag, result = translate_native(user_text)

    # 방향 라벨 붙여서 명확히
    if tag:
        reply_text = f"{tag}\n{result}"
    else:
        reply_text = result

    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
    except Exception as e:
        print("[LINE Reply ERROR]", repr(e), file=sys.stderr)

# ===== Main (Render에서는 gunicorn 사용, 로컬 테스트용) =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
