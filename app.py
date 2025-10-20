# L001
import os  # L002
import re  # L003
import sys  # L004
import traceback  # L005
from typing import List  # L006
from dotenv import load_dotenv  # L007
from flask import Flask, request, abort  # L008
# L009
from linebot import LineBotApi, WebhookHandler  # L010
from linebot.exceptions import InvalidSignatureError  # L011
from linebot.models import MessageEvent, TextMessage, TextSendMessage, JoinEvent  # L012
# L013
from openai import OpenAI  # L014
# L015
# Load .env if present (Render uses Environment Variables UI in production)  # L016
load_dotenv()  # L017
# L018
# --- LINE setup ---  # L019
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")  # L020
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")  # L021
if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:  # L022
    raise RuntimeError("Missing LINE credentials. Set LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET.")  # L023
# L024
# v2 API (works; deprecation warning is OK for now)  # L025
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)  # L026
handler = WebhookHandler(LINE_CHANNEL_SECRET)  # L027
# L028
# --- OpenAI setup ---  # L029
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # L030
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")  # L031
if not OPENAI_API_KEY:  # L032
    raise RuntimeError("Missing OPENAI_API_KEY.")  # L033
client = OpenAI(api_key=OPENAI_API_KEY)  # L034
# L035
# --- Script detectors ---  # L036
HANGUL_RE = re.compile(r"[\u3131-\uD79D]+")   # Korean script  # L037
THAI_RE   = re.compile(r"[\u0E00-\u0E7F]+")   # Thai script    # L038
# L039
app = Flask(__name__)  # L040
# L041
def decide_target_lang(text: str) -> str:  # L042
    """Return 'THAI' if source contains Korean only; 'KOREAN' if Thai only; default 'KOREAN'."""  # L043
    has_ko = bool(HANGUL_RE.search(text))  # L044
    has_th = bool(THAI_RE.search(text))  # L045
    if has_ko and not has_th:  # L046
        return "THAI"  # L047
    if has_th and not has_ko:  # L048
        return "KOREAN"  # L049
    return "KOREAN"  # L050
# L051
SYSTEM_PROMPT = (  # L052
    "You are a precise, friendly translator for casual LINE chats between Korean and Thai speakers.\n"  # L053
    "- Detect the source language.\n"  # L054
    "- If the message contains Korean, translate it into NATURAL THAI suitable for friendly chat (no stiff business tone).\n"  # L055
    "- If the message contains Thai, translate it into NATURAL KOREAN in a friendly, casual tone (banmal; not overly formal).\n"  # L056
    "- If neither script is clearly present, choose the opposite among Korean/Thai based on context, default to Korean.\n"  # L057
    "- Preserve emojis, names, and intent; adapt idioms to sound native.\n"  # L058
    "- Return ONLY the translation text. Do NOT add quotes, language tags, or explanations."  # L059
)  # L060
# L061
def translate_ko_th(text: str) -> str:  # L062
    target = decide_target_lang(text)  # L063
    hint = f"Target language: {target}."  # L064
    print("[TRANSLATE] target:", target, file=sys.stderr)  # L065
    resp = client.chat.completions.create(  # L066
        model=OPENAI_MODEL,  # L067
        messages=[  # L068
            {"role": "system", "content": SYSTEM_PROMPT},  # L069
            {"role": "user", "content": f"{hint}\n{text}"},  # L070
        ],  # L071
        temperature=0.2,  # L072
    )  # L073
    return resp.choices[0].message.content.strip()  # L074
# L075
def chunk_text(s: str, limit: int = 4500) -> List[str]:  # L076
    return [s[i:i+limit] for i in range(0, len(s), limit)]  # L077
# L078
@app.route("/", methods=["GET", "HEAD"])  # L079
def health():  # L080
    return "OK", 200  # L081
# L082
@app.route("/callback", methods=["POST"])  # L083
def callback():  # L084
    signature = request.headers.get("X-Line-Signature", "")  # L085
    body = request.get_data(as_text=True)  # L086
    try:  # L087
        handler.handle(body, signature)  # L088
    except InvalidSignatureError:  # L089
        abort(400, "Invalid signature")  # L090
    return "OK"  # L091
# L092
@handler.add(JoinEvent)  # L093
def handle_join(event: JoinEvent):  # L094
    try:  # L095
        line_bot_api.reply_message(  # L096
            event.reply_token,  # L097
            TextSendMessage(text="안녕하세요! 한국어↔태국어 자동 번역 봇이에요. 편하게 말해 보세요 😊")  # L098
        )  # L099
    except Exception as e:  # L100
        print("[JOIN ERROR]", type(e).__name__, str(e), file=sys.stderr)  # L101
        traceback.print_exc()  # L102
# L103
@handler.add(MessageEvent, message=TextMessage)  # L104
def handle_text(event: MessageEvent):  # L105
    user_text = event.message.text.strip()  # L106
    # (Optional) input logging  # L107
    # print("[DEBUG] user_text:", repr(user_text), file=sys.stderr)  # L108
    # L109
    # Optional command overrides  # L110
    forced = None  # L111
    if user_text.startswith("/ko "):  # L112
        forced = "KOREAN"  # L113
        user_text = user_text[4:]  # L114
    elif user_text.startswith("/th "):  # L115
        forced = "THAI"  # L116
        user_text = user_text[4:]  # L117
    # L118
    try:  # L119
        if forced:  # L120
            forced_prompt = SYSTEM_PROMPT + f" Translate STRICTLY into {forced}."  # L121
            resp = client.chat.completions.create(  # L122
                model=OPENAI_MODEL,  # L123
                messages=[  # L124
                    {"role": "system", "content": forced_prompt},  # L125
                    {"role": "user", "content": user_text},  # L126
                ],  # L127
                temperature=0.2,  # L128
            )  # L129
            translated = resp.choices[0].message.content.strip()  # L130
        else:  # L131
            translated = translate_ko_th(user_text)  # L132
    except Exception as e:  # L133
        # ✅ Detailed error logs to Render (indent = 8 spaces)  # L134
        print("[OpenAI ERROR]", type(e).__name__, str(e), file=sys.stderr)  # L135
        traceback.print_exc()  # L136
        translated = "번역 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요."  # L137
    # L138
    parts = chunk_text(translated)  # L139
    messages = [TextSendMessage(text=p) for p in parts]  # L140
    try:  # L141
        line_bot_api.reply_message(event.reply_token, messages)  # L142
    except Exception as e:  # L143
        print("[LINE REPLY ERROR]", type(e).__name__, str(e), file=sys.stderr)  # L144
        traceback.print_exc()  # L145
# L146
if __name__ == "__main__":  # L147
    port = int(os.environ.get("PORT", 3000))  # L148
    app.run(host="0.0.0.0", port=port)  # L149
