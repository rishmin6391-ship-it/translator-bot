from flask import Flask, request, abort
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from openai import OpenAI
import os, sys, re

app = Flask(__name__)

# --- 환경 변수 ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

# --- 클라이언트 ---
config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
oai = OpenAI(api_key=OPENAI_API_KEY)

# --- 언어 감지 ---
RE_THAI = re.compile(r"[\u0E00-\u0E7F]")
RE_KO   = re.compile(r"[\uAC00-\uD7A3]")

def detect_lang(t):
    if RE_THAI.search(t): return "th"
    if RE_KO.search(t): return "ko"
    return None

def system_prompt(src, tgt):
    return f"""
너는 전문 번역가야. {src} 언어를 {tgt} 언어로 번역하라.
규칙:
1. 의미는 변경하지 않는다.
2. 단어를 생략하거나 추가하지 않는다.
3. 고유명사, 감정, 문체를 유지한다.
4. 결과는 번역문만 출력하라.
"""

@app.route("/", methods=["GET"])
def home(): return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except Exception as e:
        print("[ERROR]", e, file=sys.stderr)
        abort(400)
    return "OK", 200

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    text = event.message.text.strip()
    lang = detect_lang(text)

    if lang == "ko":
        src, tgt, label = "한국어", "태국어", "🇰🇷→🇹🇭"
    elif lang == "th":
        src, tgt, label = "태국어", "한국어", "🇹🇭→🇰🇷"
    else:
        reply(event.reply_token, "지원 언어는 한국어/태국어입니다.")
        return

    try:
        resp = oai.chat.completions.create(
            model=MODEL,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt(src, tgt)},
                {"role": "user", "content": text}
            ],
        )
        out = resp.choices[0].message.content.strip()
        reply(event.reply_token, f"{label}\n{out}")
    except Exception as e:
        print("[OpenAI ERROR]", e, file=sys.stderr)
        reply(event.reply_token, "번역 중 문제가 발생했어요.")

def reply(token, text):
    with ApiClient(config) as client:
        MessagingApi(client).reply_message(
            ReplyMessageRequest(reply_token=token, messages=[TextMessage(text=text)])
        )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
