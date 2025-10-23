# app.py — LINE v3 SDK + OpenAI 번역봇 (한국어/태국어 기본 + 다국어 확장), PD/로컬 저장 지원
import os
import re
import sys
import json
from typing import Dict, Any, Optional
from flask import Flask, request, abort

# LINE v3 SDK
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent, JoinEvent
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)

# OpenAI
from openai import OpenAI

# 파일 잠금
from filelock import FileLock

app = Flask(__name__)

# ===== ENV =====
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# 영구 저장 경로: Render PD 사용 시 '/var/data' 로 환경변수 지정. 기본은 './data'
PERSIST_DIR = os.getenv("PERSIST_DIR", "./data")
SETTINGS_PATH = os.path.join(PERSIST_DIR, "settings.json")
LOCK_PATH = SETTINGS_PATH + ".lock"

if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and OPENAI_API_KEY):
    print("[FATAL] Missing environment variables. Check LINE_* and OPENAI_*", file=sys.stderr)
    sys.exit(1)

# ===== Clients =====
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
oai = OpenAI(api_key=OPENAI_API_KEY)

# ===== Storage helpers =====
def _ensure_dirs():
    os.makedirs(PERSIST_DIR, exist_ok=True)

def _load_settings() -> Dict[str, Any]:
    _ensure_dirs()
    if not os.path.exists(SETTINGS_PATH):
        return {}
    with FileLock(LOCK_PATH, timeout=5):
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                return {}

def _save_settings(data: Dict[str, Any]):
    _ensure_dirs()
    with FileLock(LOCK_PATH, timeout=5):
        tmp = SETTINGS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)

def get_room_key(event: MessageEvent) -> str:
    # group / room / user 별로 독립 설정
    if event.source.type == "group":
        return f"group:{event.source.group_id}"
    if event.source.type == "room":
        return f"room:{event.source.room_id}"
    return f"user:{event.source.user_id}"

# 기본 설정
DEFAULT_CONF = {
    "mode": "auto",           # auto | ko-th | th-ko | off
    "formal": "auto",         # 문체: auto | casual | formal
    "nativeTone": True,       # 현지인처럼 자연스럽게
    "tag": True,              # 🇰🇷→🇹🇭 같은 방향 라벨 붙이기
}

# ===== Lang detection =====
RE_THAI   = re.compile(r"[\u0E00-\u0E7F]")
RE_HANGUL = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]")

def detect_lang(text: str) -> Optional[str]:
    if RE_THAI.search(text):
        return "th"
    if RE_HANGUL.search(text):
        return "ko"
    return None

# ===== Prompt builders =====
def build_system_prompt(src: str, tgt: str, formal: str, native_tone: bool) -> str:
    tone_line = "현지인이 쓰는 자연스러운 구어체" if native_tone else "자연스럽고 명확한 표준어"
    style_line = ""
    if formal == "casual":
        style_line = " 반말/친근한 구어체를 사용하되 무례하지 않게."
    elif formal == "formal":
        style_line = " 존댓말/격식을 유지하되 딱딱하지 않게."

    if src == "ko" and tgt == "th":
        return (
            f"역할: 한국어→태국어 통역사.\n"
            f"목표: 의미를 정확히 전달하되 {tone_line}로 번역.{style_line}\n"
            "사투리/은어/이모지는 태국에서 통용되는 자연스러운 표현으로 변환.\n"
            "불필요한 설명/따옴표/접두사 금지. 번역문만 출력."
        )
    if src == "th" and tgt == "ko":
        return (
            f"역할: 태국어→한국어 통역사.\n"
            f"목표: 의미를 정확히 전달하되 {tone_line}로 번역.{style_line}\n"
            "태국식 관용구는 한국어에서 어색하지 않게 변환.\n"
            "불필요한 설명/따옴표/접두사 금지. 번역문만 출력."
        )
    # 다국어 확장 대비
    return "역할: 고품질 번역가. 의미 정확, 자연스러운 문장. 번역문만 출력."

def translate_text(user_text: str, cfg: Dict[str, Any]) -> (Optional[str], str):
    mode = cfg.get("mode", "auto")
    formal = cfg.get("formal", "auto")
    native = bool(cfg.get("nativeTone", True))
    tag_on = bool(cfg.get("tag", True))

    if mode == "off":
        return None, "번역 모드가 꺼져 있습니다. /mode auto 로 다시 켜주세요."

    # 결정 src/tgt
    src = tgt = None
    if mode == "ko-th":
        src, tgt = "ko", "th"
    elif mode == "th-ko":
        src, tgt = "th", "ko"
    else:
        # auto
        d = detect_lang(user_text)
        if d == "ko":
            src, tgt = "ko", "th"
        elif d == "th":
            src, tgt = "th", "ko"
        else:
            return None, "지원 언어는 한국어/태국어입니다.\n한국어↔태국어 문장을 보내주세요."

    label = "🇰🇷→🇹🇭" if (src, tgt) == ("ko","th") else "🇹🇭→🇰🇷"
    system_prompt = build_system_prompt(src, tgt, formal, native)

    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            timeout=18,
        )
        out = (resp.choices[0].message.content or "").strip()
        if tag_on:
            return label, out
        return None, out
    except Exception as e:
        print("[OpenAI ERROR]", repr(e), file=sys.stderr)
        return None, "번역 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

# ===== Command parsing =====
HELP_TEXT = (
    "🛠 번역봇 설정 명령어\n"
    "• /mode auto | ko-th | th-ko | off\n"
    "• /formal auto | casual | formal\n"
    "• /tag on|off  (방향 라벨)\n"
    "• /native on|off (현지인 톤)\n"
    "• /show (현재 설정 보기)\n"
    "• /help (이 도움말)\n"
)

def handle_command(txt: str, settings: Dict[str, Any]) -> str:
    parts = txt.strip().split()
    if not parts:
        return HELP_TEXT
    cmd = parts[0].lower()

    if cmd == "/help":
        return HELP_TEXT

    if cmd == "/show":
        return (
            "현재 설정:\n"
            f"- mode: {settings.get('mode')}\n"
            f"- formal: {settings.get('formal')}\n"
            f"- nativeTone: {settings.get('nativeTone')}\n"
            f"- tag: {settings.get('tag')}\n"
        )

    if cmd == "/mode" and len(parts) >= 2:
        v = parts[1].lower()
        if v in ("auto", "ko-th", "th-ko", "off"):
            settings["mode"] = v
            return f"모드가 '{v}' 로 변경되었습니다."
        return "사용법: /mode auto | ko-th | th-ko | off"

    if cmd == "/formal" and len(parts) >= 2:
        v = parts[1].lower()
        if v in ("auto","casual","formal"):
            settings["formal"] = v
            return f"문체가 '{v}' 로 변경되었습니다."
        return "사용법: /formal auto | casual | formal"

    if cmd == "/tag" and len(parts) >= 2:
        v = parts[1].lower()
        if v in ("on","off"):
            settings["tag"] = (v == "on")
            return f"방향 라벨(tag)이 '{v}' 로 변경되었습니다."
        return "사용법: /tag on | off"

    if cmd == "/native" and len(parts) >= 2:
        v = parts[1].lower()
        if v in ("on","off"):
            settings["nativeTone"] = (v == "on")
            return f"현지인 톤(native)이 '{v}' 로 변경되었습니다."
        return "사용법: /native on | off"

    return HELP_TEXT

# ===== Routes =====
@app.route("/", methods=["GET"])
def home():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        app.logger.info("[EVENT IN] %s", body)
        handler.handle(body, signature)
    except Exception as e:
        print("[Webhook ERROR]", repr(e), file=sys.stderr)
        abort(400)
    return "OK", 200

# ===== Handlers =====
@handler.add(JoinEvent)
def on_join(event: JoinEvent):
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="안녕하세요! 한국어↔태국어 통역봇입니다. /help 로 설정을 확인하세요.")]
            )
        )

@handler.add(FollowEvent)
def on_follow(event: FollowEvent):
    with ApiClient(line_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="친구 추가 감사합니다! /help 를 보내 설정을 확인하세요.")]
            )
        )

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()

    # 로드 설정
    store = _load_settings()
    key = get_room_key(event)
    cfg = {**DEFAULT_CONF, **store.get(key, {})}

    # 명령어?
    if user_text.startswith("/"):
        answer = handle_command(user_text, cfg)
        store[key] = cfg
        _save_settings(store)
        reply(answer, event.reply_token)
        return

    # 번역
    tag, out = translate_text(user_text, cfg)
    if tag:
        reply_text = f"{tag}\n{out}"
    else:
        reply_text = out
    reply(reply_text, event.reply_token)

def reply(text: str, reply_token: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text)]
                )
            )
    except Exception as e:
        print("[LINE Reply ERROR]", repr(e), file=sys.stderr)

# ===== Main =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
