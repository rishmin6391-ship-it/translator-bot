import os
import re
import sys
import json
import time
from typing import Optional, Tuple, Dict, Any
from flask import Flask, request, abort

# ===== LINE v3 SDK =====
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)

# ===== OpenAI =====
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

# ===== Persistent state path =====
STATE_DIR = os.getenv("TRANSLATOR_STATE_DIR", "/opt/render/persistent/translator_state")
STATE_FILE = "state.json"
STATE_PATH = os.path.join(STATE_DIR, STATE_FILE)

# 안전한 디렉토리 만들기 (권한 오류 시 폴백)
def _ensure_state_dir() -> str:
    path_order = [STATE_DIR, "/opt/render/persistent/translator_state", "./translator_state"]
    for p in path_order:
        try:
            os.makedirs(p, exist_ok=True)
            test_file = os.path.join(p, ".touch")
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test_file)
            return p
        except Exception as e:
            print(f"[WARN] state dir '{p}' not usable: {e}", file=sys.stderr)
            continue
    # 마지막 폴백
    return "./translator_state"

STATE_DIR = _ensure_state_dir()
STATE_PATH = os.path.join(STATE_DIR, STATE_FILE)
print(f"[STATE] Using state dir: {STATE_DIR}")

# ===== Clients =====
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
oai = OpenAI(api_key=OPENAI_API_KEY)

# ===== In-memory cache (디스크 + 메모리) =====
_state_mem: Dict[str, Any] = {}
_state_loaded = False
_state_last_flush = 0.0

def _load_state():
    global _state_mem, _state_loaded, _state_last_flush
    if _state_loaded:
        return
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                _state_mem = json.load(f)
            print(f"[STATE] Loaded {_safe_size(_state_mem)} entries")
        else:
            _state_mem = {}
        _state_loaded = True
        _state_last_flush = time.time()
    except Exception as e:
        print("[STATE] Load failed:", repr(e), file=sys.stderr)
        _state_mem = {}
        _state_loaded = True

def _safe_size(d):
    try:
        return len(d)
    except Exception:
        return "?"

def _flush_state(force: bool = False):
    """디스크 쓰기는 5초에 한 번만 (burst 보호)."""
    global _state_last_flush
    now = time.time()
    if not force and (now - _state_last_flush) < 5.0:
        return
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state_mem, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
        _state_last_flush = now
    except Exception as e:
        print("[STATE] Flush failed:", repr(e), file=sys.stderr)

# ===== Simple helpers =====
RE_THAI   = re.compile(r"[\u0E00-\u0E7F]")  # Thai
RE_HANGUL = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]")  # Hangul (Jamo+Syllables)

EMOJI_REGEX = re.compile(
    r"["
    r"\U0001F600-\U0001F64F"  # emoticons
    r"\U0001F300-\U0001F5FF"  # symbols & pictographs
    r"\U0001F680-\U0001F6FF"  # transport & map
    r"\U0001F1E0-\U0001F1FF"  # flags (iOS)
    r"]+", flags=re.UNICODE
)

KOREAN_REACTIONS = re.compile(r"(ㅋ+|ㅎ+|^ㅠ+|^ㅜ+|^ㄷㄷ|^ㅇㅇ|^ㄴㄴ|^ㅅㅂ|^ㅈㅅ|^넵|^넹|^ㅇㅋ|^\^\^)$")
THAI_REACTIONS   = re.compile(r"^(5{2,}|555+|คริ|คิคิ|ฮ่า+)$")  # 555=웃음

def _room_key(evt: MessageEvent) -> str:
    """그룹/룸/1:1 각각을 고유키로 식별."""
    src = evt.source
    if src.type == "group":
        return f"group:{src.group_id}"
    if src.type == "room":
        return f"room:{src.room_id}"
    return f"user:{src.user_id}"

def _get_last_lang(key: str) -> Optional[str]:
    try:
        return _state_mem.get(key, {}).get("last_lang")
    except Exception:
        return None

def _put_last_lang(key: str, lang: str):
    _state_mem.setdefault(key, {})
    _state_mem[key]["last_lang"] = lang
    _flush_state()

def detect_lang(text: str, last_lang: Optional[str]) -> Optional[str]:
    """문자 범위 + 반응 패턴 + 이모지 기반. 미검출 시 최근 언어 유지."""
    if RE_THAI.search(text):
        return "th"
    if RE_HANGUL.search(text):
        return "ko"

    # 반응사/이모지 → 최근 언어 유지
    if KOREAN_REACTIONS.search(text):
        return last_lang or "ko"
    if THAI_REACTIONS.search(text):
        return last_lang or "th"
    if EMOJI_REGEX.search(text) or text.strip() in {"ㅋㅋ", "ㅎㅎ", "^^", "ㅠㅠ", "ㅜㅜ"}:
        return last_lang  # 최근 언어 그대로 (없으면 None 반환)

    return None

def build_system_prompt(src: str, tgt: str) -> str:
    if src == "ko" and tgt == "th":
        return (
            "역할: 한→태 통역사.\n"
            "원문의 뉘앙스·존댓말/반말을 유지하되, 태국 현지인이 쓰는 자연스러운 구어체로 번역해.\n"
            "사투리/은어는 태국에서 통하는 자연스러운 표현으로 옮겨.\n"
            "번역문만 답하고, 따옴표/설명/접두사는 쓰지 마."
        )
    if src == "th" and tgt == "ko":
        return (
            "역할: 태→한 통역사.\n"
            "원문의 뉘앙스·존댓말/반말은 살리되, 한국인이 자연스럽게 쓰는 표현으로 번역해.\n"
            "태국식 직역은 피하고 한국어 문맥에 맞게 다듬어.\n"
            "번역문만 답하고, 따옴표/설명/접두사는 쓰지 마."
        )
    return "입력 문장을 자연스럽고 정확하게 번역해. 번역문만 답해."

def translate_native(text: str, src: str, tgt: str) -> str:
    system_prompt = build_system_prompt(src, tgt)
    try:
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": text},
            ],
            timeout=18,  # 빠른 응답
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print("[OpenAI ERROR]", repr(e), file=sys.stderr)
        return "번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요."

def build_reply_label(src: str, tgt: str) -> str:
    if src == "ko" and tgt == "th":
        return "🇰🇷→🇹🇭"
    if src == "th" and tgt == "ko":
        return "🇹🇭→🇰🇷"
    return ""

# ===== Routes =====
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
        print("[Webhook ERROR]", repr(e), file=sys.stderr)
        abort(400)
    return "OK", 200

# ===== Handlers =====
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    _load_state()

    key = _room_key(event)
    user_text = (event.message.text or "").strip()
    app.logger.info("[MESSAGE] %s | %s", key, user_text)

    last_lang = _get_last_lang(key)
    detected = detect_lang(user_text, last_lang)

    # 언어 결정 로직
    if detected == "ko":
        src, tgt = "ko", "th"
    elif detected == "th":
        src, tgt = "th", "ko"
    else:
        # 마지막 언어가 있으면 그 방향 유지, 없으면 가이드
        if last_lang in {"ko", "th"}:
            src = last_lang
            tgt = "th" if last_lang == "ko" else "ko"
        else:
            _reply(event.reply_token,
                   "지원 언어는 한국어/태국어입니다.\n한국어↔태국어 문장을 보내주세요.")
            return

    # 번역
    out = translate_native(user_text, src, tgt)
    tag = build_reply_label(src, tgt)
    _reply(event.reply_token, f"{tag}\n{out}")

    # 최근 언어 저장
    try:
        _put_last_lang(key, src)
    except Exception as e:
        print("[STATE] save last_lang failed:", repr(e), file=sys.stderr)

def _reply(reply_token: str, text: str):
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

# ===== Main (local only) =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
