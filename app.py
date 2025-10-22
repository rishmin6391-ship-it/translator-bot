# app.py — v3 SDK, 지속 설정 + 최적화 + 방향 라벨
import os
import re
import sys
import json
import time
import threading
from typing import Optional, Tuple

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

# 채팅방 설정 파일 경로(영구 저장 위치). Render의 Persistent Disk를 /data 로 마운트하면 재배포/재시작 뒤에도 유지됩니다.
SETTINGS_PATH = os.getenv("SETTINGS_PATH", "/data/settings.json")

if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and OPENAI_API_KEY):
    print("[FATAL] Missing environment variables.", file=sys.stderr)
    sys.exit(1)

# ===== Clients (재사용) =====
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
# ApiClient / MessagingApi를 전역으로 재사용(keep-alive)
_line_api_client = ApiClient(line_config)
_line_api = MessagingApi(_line_api_client)

handler = WebhookHandler(LINE_CHANNEL_SECRET)
oai = OpenAI(api_key=OPENAI_API_KEY)

# ===== Regex for Language Detection =====
RE_THAI   = re.compile(r"[\u0E00-\u0E7F]")  # 태국어
RE_HANGUL = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]")  # 한글(자모+완성형)

# ===== Settings Store (Thread-safe) =====
_settings_lock = threading.Lock()
_chat_settings = {}  # {chat_id: {"mode": "auto"|"ko->th"|"th->ko"}}

def _load_settings():
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    if not os.path.exists(SETTINGS_PATH):
        return
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _chat_settings.update(data)
    except Exception as e:
        print("[WARN] Failed to load settings:", e, file=sys.stderr)

def _save_settings():
    tmp = SETTINGS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_chat_settings, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except Exception as e:
        print("[WARN] Failed to save settings:", e, file=sys.stderr)

# 초기 로드
_load_settings()

def _get_chat_id(event: MessageEvent) -> str:
    """그룹/룸/1:1 구분하여 채팅방 ID를 반환."""
    src = event.source
    # v3 모델에서 속성명 스네이크/카멜 혼용 대응
    group_id = getattr(src, "group_id", getattr(src, "groupId", None))
    room_id  = getattr(src, "room_id", getattr(src, "roomId", None))
    user_id  = getattr(src, "user_id", getattr(src, "userId", None))
    if group_id:
        return f"group:{group_id}"
    if room_id:
        return f"room:{room_id}"
    return f"user:{user_id}"

def get_mode(chat_id: str) -> str:
    with _settings_lock:
        return _chat_settings.get(chat_id, {}).get("mode", "auto")

def set_mode(chat_id: str, mode: str) -> None:
    with _settings_lock:
        _chat_settings.setdefault(chat_id, {})["mode"] = mode
        _save_settings()

# ===== Language + Prompt =====
def detect_lang(text: str) -> Optional[str]:
    if RE_THAI.search(text):
        return "th"
    if RE_HANGUL.search(text):
        return "ko"
    return None

def build_system_prompt(src: str, tgt: str) -> str:
    if src == "ko" and tgt == "th":
        return (
            "역할: 한→태 통역사.\n"
            "원문의 말투(존댓말/반말·감정·유머)를 유지하되, 태국 현지인이 쓰는 자연스러운 구어체로 번역해.\n"
            "불필요한 설명/따옴표/접두사 금지. 번역문만."
        )
    if src == "th" and tgt == "ko":
        return (
            "역할: 태→한 통역사.\n"
            "원문의 말투(존댓말/반말·감정·유머)를 유지하되, 한국인이 쓰는 자연스러운 구어체로 번역해.\n"
            "불필요한 설명/따옴표/접두사 금지. 번역문만."
        )
    return "입력 문장을 자연스럽고 정확하게 번역해. 번역문만."

# ===== OpenAI 호출(지연 최소 + 재시도) =====
def chat_translate(system_prompt: str, user_text: str, timeout_s: float = 8.0) -> str:
    # 가벼운 재시도: 3회, 지수 백오프(0.4s, 0.8s)
    delays = [0.0, 0.4, 0.8]
    last_err = None
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            resp = oai.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ],
                timeout=timeout_s,
            )
            out = (resp.choices[0].message.content or "").strip()
            return out
        except Exception as e:
            last_err = e
    print("[OpenAI ERROR]", repr(last_err), file=sys.stderr)
    return "번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요."

# ===== 번역 라우팅 =====
def translate_with_mode(user_text: str, mode: str) -> Tuple[str, str]:
    """
    mode: "auto" | "ko->th" | "th->ko"
    return: (tag, translated_text)  tag는 방향 라벨(없으면 "")
    """
    if mode == "ko->th":
        src, tgt, tag = "ko", "th", "🇰🇷→🇹🇭"
    elif mode == "th->ko":
        src, tgt, tag = "th", "ko", "🇹🇭→🇰🇷"
    else:
        # auto
        det = detect_lang(user_text)
        if det == "ko":
            src, tgt, tag = "ko", "th", "🇰🇷→🇹🇭"
        elif det == "th":
            src, tgt, tag = "th", "ko", "🇹🇭→🇰🇷"
        else:
            help_msg = (
                "지원 언어는 한국어/태국어입니다.\n"
                "• 한국어 → 태국어\n• 태국어 → 한국어\n"
                "또는 채팅방에서 ‘설정 한국어→태국어’, ‘설정 태국어→한국어’, ‘자동감지’, ‘상태’ 를 사용할 수 있어요."
            )
            return "", help_msg

    system_prompt = build_system_prompt(src, tgt)
    out = chat_translate(system_prompt, user_text)
    return tag, out

# ===== 명령어 처리 =====
def maybe_handle_command(chat_id: str, text: str) -> Optional[str]:
    t = text.strip().replace(" ", "")
    if t in ("상태", "/상태", "상태보기"):
        mode = get_mode(chat_id)
        if mode == "ko->th":
            return "현재 모드: 🇰🇷→🇹🇭 (한국어를 태국어로)"
        if mode == "th->ko":
            return "현재 모드: 🇹🇭→🇰🇷 (태국어를 한국어로)"
        return "현재 모드: 자동감지 (한국어↔태국어 자동 번역)"

    if t in ("자동감지", "/자동", "기본모드"):
        set_mode(chat_id, "auto")
        return "이제 자동감지 모드입니다. (한국어↔태국어 자동 번역)"

    patterns = ("설정한국어→태국어", "설정한국어->태국어", "설정한→태", "설정ko→th", "설정ko->th")
    if any(t == p for p in patterns):
        set_mode(chat_id, "ko->th")
        return "이 채팅방은 이제 🇰🇷→🇹🇭 모드입니다. (한국어를 태국어로 번역)"

    patterns = ("설정태국어→한국어", "설정태국어->한국어", "설정태→한", "설정th→ko", "설정th->ko")
    if any(t == p for p in patterns):
        set_mode(chat_id, "th->ko")
        return "이 채팅방은 이제 🇹🇭→🇰🇷 모드입니다. (태국어를 한국어로 번역)"

    return None

# ===== Routes =====
@app.route("/", methods=["GET"])
def home():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        app.logger.info("[EVENT IN] %s", body[:2000])  # 과한 로그 방지
        handler.handle(body, signature)
    except Exception as e:
        print("[Webhook ERROR]", repr(e), file=sys.stderr)
        abort(400)
    return "OK", 200

# ===== Handler =====
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()
    chat_id = _get_chat_id(event)
    app.logger.info("[MESSAGE] chat=%s text=%s", chat_id, user_text)

    # 1) 명령어 먼저 처리
    cmd_resp = maybe_handle_command(chat_id, user_text)
    if cmd_resp is not None:
        _reply(event.reply_token, cmd_resp)
        return

    # 2) 현재 모드로 번역
    mode = get_mode(chat_id)
    tag, result = translate_with_mode(user_text, mode)
    reply_text = f"{tag}\n{result}" if tag else result
    _reply(event.reply_token, reply_text)

def _reply(reply_token: str, text: str):
    try:
        _line_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )
    except Exception as e:
        print("[LINE Reply ERROR]", repr(e), file=sys.stderr)

# ===== Main (로컬 테스트용) =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    # 로컬에서 빠른 응답 확인용
    app.run(host="0.0.0.0", port=port)
