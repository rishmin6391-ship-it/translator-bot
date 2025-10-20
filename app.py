import os  # L002
import re  # L003
import sys  # L004
import traceback  # L005
from typing import List  # L006
from dotenv import load_dotenv  # L007
from flask import Flask, request, abort  # L008
from linebot import LineBotApi, WebhookHandler  # L010
from linebot.exceptions import InvalidSignatureError  # L011
from linebot.models import MessageEvent, TextMessage, TextSendMessage, JoinEvent  # L012
from openai import OpenAI  # L014
load_dotenv()  # L017
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")  # L020
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")  # L021
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:  # L022
    raise RuntimeError("Missing LINE credentials. Set LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET.")  # L023
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)  # L026
handler = WebhookHandler(LINE_CHANNEL_SECRET)  # L027
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # L030
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # L031
if not OPENAI_API_KEY:  # L032
    raise RuntimeError("Missing OPENAI_API_KEY.")  # L033
client = OpenAI(api_key=OPENAI_API_KEY)  # L034
HANGUL_RE = re.compile(r"[\u3131-\uD79D]+")  # L037
THAI_RE   = re.compile(r"[\u0E00-\u0E7F]+")  # L038
app = Flask(__name__)  # L040

def decide_target_lang(text: str) -> str:  # L042
    has_ko = bool(HANGUL_RE.search(text))  # L044
    has_th = bool(THAI_RE.search(text))  # L045
    if has_ko and not has_th: return "THAI"  # L046
    if has_th and not has_ko: return "KOREAN"  # L048
    return "KOREAN"  # L050

SYSTEM_PROMPT = (
    "You are a precise, friendly translator for casual LINE chats between Korean and Thai speakers.\n"
    "- Detect the source language.\n"
    "- If the message contains Korean, translate it into NATURAL THAI suitable for friendly chat.\n"
    "- If the message contains Thai, translate it into NATURAL KOREAN in a friendly tone.\n"
    "- Preserve emojis, names, and intent.\n"
    "- Return ONLY the translation text."
)

def translate_ko_th(text: str) -> str:  # L062
    target = decide_target_lang(text)  # L063
    hint = f"Target language: {target}."  # L064
    print("[TRANSLATE] target:", target, file=sys.stderr)  # L065
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{hint}\n{text}"},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()  # L074

def chunk_text(s: str, limit: int = 4500) -> List[str]:
    return [s[i:i+limit] for i in range(0, len(s), limit)]

@app.route("/", methods=["GET", "HEAD"])
def health():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
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
        line_bot_api.reply_message(event.reply_token, messages)
    except Exception as e:
        print("[LINE REPLY ERROR]", type(e).__name__, str(e), file=sys.stderr)
        traceback.print_exc()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
