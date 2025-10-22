# -*- coding: utf-8 -*-
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
import httpx

app = Flask(__name__)

# ===== ENV =====
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and OPENAI_API_KEY):
    print("[FATAL] Missing env: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET / OPENAI_API_KEY", file=sys.stderr)
    sys.exit(1)

# ===== Filesystem: Render에서 쓰기 가능 경로로 설정 (권한 오류 방지) =====
BASE_DIR = os.path.dirname(os.path.abspath(__file__))               # /opt/render/project/src
DATA_DIR = os.path.join(BASE_DIR, "data")                           # 프로젝트 폴더 안쪽
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")             # 방별 설정 저장
os.makedirs(DATA_DIR, exist_ok=True)

# ===== LINE/OpenAI Clients =====
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# OpenAI: 저지연 httpx 설정 (Keep-Alive, 짧은 통신 타임아웃)
oai_http = httpx.Client(
    timeout=httpx.Timeout(connect=2.0, read=6.0, write=3.0, pool=6.0),  # 5초 넘지 않도록 타이트하게
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
)
oai = OpenAI(api_key=OPENAI_API_KEY, http_client=oai_http)

# ===== Language detection (정확·가벼움) =====
RE_THAI   = re.compile(r"[\u0E00-\u0E7F]")  # 태국어
RE_HANGUL = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]")  # 한글

def detect_lang(text: str) -> Optional[str]:
    if RE_THAI.search(text):
        return "th"
    if RE_HANGUL.search(text):
        return "ko"
    return None

# ======= 방별 설정(언어페어/호출 트리거 등) 저장/로드 =======
_lock = threading.Lock()
_default_room_cfg = {
    "mode": "auto",        # "auto": ko<->th 자동, 또는 "ko2th"/"th2ko" 강제
    "prefix": "",          # 특정 접두사(@봇, !tr 등) 요구 시 설정. 빈 문자열이면 무조건 번역
    "native_tone": True    # 현지 구어체 톤 사용
}
_settings_cache = {"rooms": {}}  # { roomId(or userId): cfg }

def _atomic_write_json(path: str, data: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _load_settings():
    global _settings_cache
    if not os.path.exists(SETTINGS_PATH):
        _settings_cache = {"rooms": {}}
        _atomic_write_json(SETTINGS_PATH, _settings_cache)
        return
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            _settings_cache = json.load(f)
            if "rooms" not in _settings_cache:
                _settings_cache = {"rooms": {}}
    except Exception as e:
        print("[WARN] settings load failed:", repr(e), file=sys.stderr)
        _settings_cache = {"rooms": {}}

def _save_settings():
    try:
        _atomic_write_json(SETTINGS_PATH, _settings_cache)
    except Exception as e:
        print("[WARN] settings save failed:", repr(e), file=sys.stderr)

def get_room_cfg(room_id: str) -> dict:
    with _lock:
        room = _settings_cache["rooms"].get(room_id)
        if not room:
            room = dict(_default_room_cfg)
            _settings_cache["rooms"][room_id] = room
            _save_settings()
        return room

def update_room_cfg(room_id: str, **fields):
    with _lock:
        room = _settings_cache["rooms"].get(room_id) or dict(_default_room_cfg)
        room.update({k: v for k, v in fields.items() if v is not None})
        _settings_cache["rooms"][room_id] = room
        _save_settings()

# 처음 기동 시 로드
_load_settings()

# ===== 프롬프트 =====
def build_system_prompt(src: str, tgt: str, native_tone: bool) -> str:
    if native_tone:
        if src == "ko" and tgt == "th":
            return (
                "역할: 한→태 통역사.\n"
                "원문의 뉘앙스/존댓말/반말을 유지하되, 태국 현지인이 쓰는 자연스러운 구어체로 번역해.\n"
                "불필요한 설명·따옴표 금지. 번역문만."
            )
        if src == "th" and tgt == "ko":
            return (
                "역할: 태→한 통역사.\n"
                "원문의 뉘앙스/존댓말/반말을 유지하되, 한국인이 쓰는 자연스러운 구어체로 번역해.\n"
                "불필요한 설명·따옴표 금지. 번역문만."
            )
    return "입력 문장을 자연스럽고 정확하게 번역해. 번역문만."

def choose_direction(text: str, mode: str) -> Optional[Tuple[str, str, str]]:
    """
    반환: (src, tgt, tag) 또는 None
    """
    if mode == "ko2th":
        return ("ko", "th", "🇰🇷→🇹🇭")
    if mode == "th2ko":
        return ("th", "ko", "🇹🇭→🇰🇷")
    # auto
    src = detect_lang(text)
    if src == "ko":
        return ("ko", "th", "🇰🇷→🇹🇭")
    if src == "th":
        return ("th", "ko", "🇹🇭→🇰🇷")
    return None

# ===== 번역 (저지연/내결함성) =====
def translate_text(user_text: str, src: str, tgt: str, native_tone: bool) -> str:
    sys_prompt = build_system_prompt(src, tgt, native_tone)
    try:
        # 짧은 답변 유도 -> latency 절감
        resp = oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_text},
            ],
            max_tokens=120,        # 과도한 토큰 방지
            temperature=0.3,       # 일관성↑, 속도↑
            timeout=8,             # 5초 목표 내 타임아웃 타이트
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print("[OpenAI ERROR]", repr(e), file=sys.stderr)
        return "지금은 번역 서버가 혼잡합니다. 잠시 후 다시 시도해주세요."

# ===== 라우팅 =====
@app.route("/", methods=["GET"])
def home():
    return "OK", 200

@app.route("/healthz", methods=["GET"])
def health():
    return "ok", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        # 오래된 이벤트(예: 슬립 뒤 깨우기) 무시 -> 불필요 지연 제거
        payload = json.loads(body)
        for ev in payload.get("events", []):
            ts = ev.get("timestamp")
            if ts and (time.time() * 1000 - int(ts) > 60_000):
                app.logger.info("[SKIP old event] %s", ev.get("webhookEventId"))
                return "OK", 200
        app.logger.info("[EVENT IN] %s", body)
        handler.handle(body, signature)
    except Exception as e:
        print("[Webhook ERROR]", repr(e), file=sys.stderr)
        abort(400)
    return "OK", 200

# ===== 명령어(방 설정) 파서 =====
def try_handle_command(room_id: str, text: str) -> Optional[str]:
    t = text.strip().lower()
    if t in ("!mode auto", "!auto"):
        update_room_cfg(room_id, mode="auto")
        return "번역 모드: 자동(한국어↔태국어 인식)으로 설정되었습니다."
    if t in ("!mode ko2th", "!ko2th"):
        update_room_cfg(room_id, mode="ko2th")
        return "번역 모드: 한국어 → 태국어 고정."
    if t in ("!mode th2ko", "!th2ko"):
        update_room_cfg(room_id, mode="th2ko")
        return "번역 모드: 태국어 → 한국어 고정."
    if t.startswith("!prefix "):
        prefix = t.split(" ", 1)[1].strip()
        update_room_cfg(room_id, prefix=prefix)
        return f"번역 트리거 접두사(prefix): '{prefix}' 로 설정되었습니다. (빈 문자열이면 항상 번역)"
    if t == "!native on":
        update_room_cfg(room_id, native_tone=True)
        return "현지 구어체 톤: ON"
    if t == "!native off":
        update_room_cfg(room_id, native_tone=False)
        return "현지 구어체 톤: OFF"
    if t in ("!help", "/help"):
        return (
            "번역봇 설정 명령어:\n"
            "• !mode auto | !mode ko2th | !mode th2ko\n"
            "• !prefix <문자열>  (예: !prefix @tr)\n"
            "• !native on|off    (현지 구어체 톤)\n"
            "• !help"
        )
    return None

# ===== 핸들러 =====
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()
    room_id = None
    if event.source.type == "group":
        room_id = event.source.group_id
    elif event.source.type == "room":
        room_id = event.source.room_id
    else:
        room_id = event.source.user_id

    # 방 설정 로드
    cfg = get_room_cfg(room_id)

    # 접두사(prefix) 요구 설정 시: 접두사 없으면 패스
    if cfg.get("prefix"):
        if not user_text.startswith(cfg["prefix"]):
            # 접두사가 없고, 다만 명령어는 항상 처리
            cmd = try_handle_command(room_id, user_text)
            if cmd:
                _reply(event.reply_token, cmd)
            return
        else:
            # 접두사 제거 후 번역
            user_text = user_text[len(cfg["prefix"]):].lstrip()

    # 먼저 명령어 판별
    cmd = try_handle_command(room_id, user_text)
    if cmd:
        _reply(event.reply_token, cmd)
        return

    # 번역 방향 결정
    choice = choose_direction(user_text, cfg.get("mode", "auto"))
    if not choice:
        _reply(event.reply_token,
               "지원 언어는 한국어/태국어 입니다.\n"
               "• !mode auto (자동) / !mode ko2th / !mode th2ko\n"
               "• !help 로 명령어를 확인하세요.")
        return

    src, tgt, tag = choice
    result = translate_text(user_text, src, tgt, cfg.get("native_tone", True))
    reply_text = f"{tag}\n{result}"
    _reply(event.reply_token, reply_text)

def _reply(reply_token: str, text: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text[:4900])]  # LINE 5,000자 근사 제한
                )
            )
    except Exception as e:
        print("[LINE Reply ERROR]", repr(e), file=sys.stderr)

# ===== Main (로컬 테스트) =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    # Flask의 reloader 비활성화 -> 기동 시간 단축
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
