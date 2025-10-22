# app.py (LINE v3 + Flask + OpenAI / KO<->TH auto-translate stable)

import os
import sys
import json
import re
from typing import Optional

from flask import Flask, request, abort

# === LINE v3 SDK ===
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
)

# === OpenAI ===
from openai import OpenAI
import openai  # 예외 타입 참조용 (openai.RateLimitError 등)

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 환경 변수
# ─────────────────────────────────────────────────────────────────────────────
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("[BOOT] LINE env missing. Check LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET", file=sys.stderr)
if not OPENAI_API_KEY:
    print("[BOOT] OPENAI_API_KEY is missing.", file=sys.stderr)

# ─────────────────────────────────────────────────────────────────────────────
# LINE SDK v3 초기화
# ─────────────────────────────────────────────────────────────────────────────
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration)
messaging_api = MessagingApi(api_client)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ─────────────────────────────────────────────────────────────────────────────
# OpenAI 초기화
# ─────────────────────────────────────────────────────────────────────────────
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# 언어 감지 & 번역 유틸
# ─────────────────────────────────────────────────────────────────────────────
THAI_RE   = re.compile(r'[\u0E00-\u0E7F]')  # 태국어
HANGUL_RE = re.compile(r'[\uAC00-\uD7A3\u1100-\u11FF\u3130-\u318F]')  # 한글(완성형/자모/호환)

def detect_lang(text: str) -> str:
    """
    견고한 문자 범위 기반 판별: 'ko' | 'th' | 'unknown'
    - 태국어 범위 문자가 있으면 'th'
    - 한글 범위 문자가 있으면 'ko'
    - 둘 다 있거나 둘 다 없으면 'unknown'
    """
    has_th = bool(THAI_RE.search(text))
    has_ko = bool(HANGUL_RE.search(text))
    if has_th and not has_ko:
        return "th"
    if has_ko and not has_th:
        return "ko"
    return "unknown"

def translate_exact(text: str, source: str, target: str, timeout: int = 30) -> str:
    """
    OpenAI에 소스/타깃을 명시해 번역만 출력하도록 강제.
    source/target: 'ko' 또는 'th'
    """
    lang_name = {"ko": "Korean", "th": "Thai"}
    sys_prompt = (
        f"You are a professional translator. Translate strictly from {lang_name[source]} "
        f"to {lang_name[target]}. Output ONLY the translation (no quotes, no language labels, "
        f"no explanations). Preserve emojis, URLs, and proper nouns."
    )

    resp = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user",   "content": text},
        ],
        temperature=0.0,
        top_p=1.0,
        timeout=timeout,
    )
    out = (resp.choices[0].message.content or "").strip()
    return out.strip("`").strip()

def strip_bot_mention(text: str) -> str:
    """
    라인 그룹에서 '@봇이름 ' 형태로 멘션될 수 있으므로,
    맨 앞의 멘션 패턴을 간단히 제거.
    """
    # 예: "@Kira Translator2 안녕" -> "안녕"
    return re.sub(r"^@\S+(?:\s+|：|:)\s*", "", text).strip()

def reply_text(reply_token: str, text: str) -> None:
    try:
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text[:5000])]  # LINE 메시지 길이 방지
            )
        )
    except Exception as e:
        print("[ReplyError]", type(e).__name__, str(e), file=sys.stderr)

# ─────────────────────────────────────────────────────────────────────────────
# 라우트
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # 관찰용 로그
    try:
        print("[EVENT IN]", body, file=sys.stdout)
    except Exception:
        pass

    try:
        handler.handle(body, signature)
    except Exception as e:
        # 서명 오류/파싱 오류 등은 400으로 응답
        print("[WebhookError]", type(e).__name__, str(e), file=sys.stderr)
        abort(400)
    return "OK", 200

# ─────────────────────────────────────────────────────────────────────────────
# 이벤트 핸들러 (v3 스타일)
# ─────────────────────────────────────────────────────────────────────────────
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    """
    한국어만 오면 태국어로, 태국어만 오면 한국어로 번역.
    둘 다 없거나 섞이면 안내 문구.
    """
    try:
        user_text = (event.message.text or "").strip()
        print("[MESSAGE]", user_text, file=sys.stdout)

        # 멘션 제거
        clean_text = strip_bot_mention(user_text)
        if not clean_text:
            return

        src = detect_lang(clean_text)
        if src == "ko":
            tgt = "th"
        elif src == "th":
            tgt = "ko"
        else:
            reply_text(
                event.reply_token,
                "지원 언어는 한국어/태국어입니다. 한국어는 태국어로, 태국어는 한국어로 번역해 드려요."
            )
            return

        translated = translate_exact(clean_text, src, tgt)
        # 혹시 모델이 설명을 붙였을 가능성 대비: 한 줄 요약만
        translated = translated.splitlines()[0].strip() if translated else ""
        if not translated:
            translated = "번역 결과가 비어 있습니다. 다시 시도해주세요."
        reply_text(event.reply_token, translated)

    except openai.RateLimitError:
        reply_text(event.reply_token, "번역 사용량이 초과되었습니다. 잠시 후 다시 시도해주세요.")
    except Exception as e:
        print("[ERROR]", type(e).__name__, str(e), file=sys.stderr)
        reply_text(event.reply_token, "번역 중 오류가 발생했어요. 잠시 후 다시 시도해주세요.")

# ─────────────────────────────────────────────────────────────────────────────
# 로컬 실행 (Render에서는 Gunicorn으로 구동)
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
