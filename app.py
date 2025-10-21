import os
import re
import sys
import traceback
from typing import List
from dotenv import load_dotenv
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, JoinEvent
from linebot.http_client import RequestsHttpClient  # timeout control
from requests.exceptions import ReadTimeout           # retry on timeout

from openai import OpenAI

# Load .env if running locally; on Render, set Environment variables
load_dotenv()

# --- LINE setup ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise RuntimeError("Missing LINE credentials. Set LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET.")

# Use custom http_client to increase timeout (default ~5s)
http_client = RequestsHttpClient(timeout=15)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN, http_client=http_client)  # v2 API (deprecation warning is OK)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- OpenAI setup ---
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY.")
client = OpenAI(api_key=OPENAI_API_KEY)

# --- Script detectors ---
HANGUL_RE = re.compile(r"[\u3131-\uD79D]+")   # Korean script
THAI_RE   = re.compile(r"[\u0E00-\u0E7F]+")   # Thai script

app = Flask(__name__)

def decide_target_lang(text: str) -> str:
    """Return 'THAI' if source contains Korean only; 'KOREAN' if Thai only; default 'KOREAN'."""
    has_ko = bool(HANGUL_RE.search(text))
    has_th = bool(THAI_RE.search(text))
    if has_ko and not has_th:
        return "THAI"
    if has_th and not has_ko:
        return "KOREAN"
    return "KOREAN"

SYSTEM_PROMPT = (
    "You are a precise, friendly translator for casual LINE chats between Korean and Thai speakers.\n"
    "- Detect the source language.\n"
    "- If the message contains Korean, translate it into NATURAL THAI suitable for friendly chat (no stiff business tone).\n"
    "- If the message contains Thai, translate it into NATURAL KOREAN in a friendly, casual tone (banmal; not overly formal).\n"
    "- If neither script is clearly present, choose the opposite among Korean/Thai based on context, default to Korean.\n"
    "- Preserve emojis, names, and intent; adapt idioms to sound native.\n"
    "- Return ONLY the translation text. Do NOT add quotes, language tags, or explanations."
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
    )
    return resp.choices[0].message.content.strip()

def chunk_text(s: str, limit: int = 4500) -> List[str]:
    return [s[i:i+limit] for i in range(0, len(s), limit)]

@app.route("/", methods=["GET", "HEAD"])
def health():
    return "OK", 200

# Accept GET for Verify (200) and POST for real webhooks
@app.route("/callback", methods=["GET", "POST"])
def callback():
    if request.method == "GET":
        return "OK", 200

    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, "Invalid signature")
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

    # Optional command overrides
    forced = None
    if user_text.startswith("/ko "):
        forced = "KOREAN"
        user_text = user_text[4:]
    elif user_text.startswith("/th "):
        forced = "THAI"
        user_text = user_text[4:]

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
        # First try with 15s timeout
        line_bot_api.reply_message(event.reply_token, messages, timeout=15)
    except ReadTimeout:
        print("[LINE REPLY TIMEOUT] retry with 25s", file=sys.stderr)
        try:
            # Second attempt within replyToken validity
            line_bot_api.reply_message(event.reply_token, messages, timeout=25)
        except Exception as e2:
            print("[LINE REPLY ERROR/RETRY]", type(e2).__name__, str(e2), file=sys.stderr)
            traceback.print_exc()
    except Exception as e:
        print("[LINE REPLY ERROR]", type(e).__name__, str(e), file=sys.stderr)
        traceback.print_exc()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
