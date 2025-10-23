# app.py — v3 LINE SDK + 자연스러운 구어체 번역 + langdetect + 디스크영구저장 + 속도최적화
import os
import re
import sys
import json
import time
from pathlib import Path
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

# ===== Lang detect =====
try:
    from langdetect import detect
    _HAS_LANGDETECT = True
except Exception:
    _HAS_LANGDETECT = False

app = Flask(__name__)

# ===== ENV =====
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")  # 품질 향상을 위해 기본 gpt-4o
DATA_DIR = os.getenv("DATA_DIR", "/opt/render/project/src/data")  # Render 퍼시스턴트 디스크 권장 경로

if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and OPENAI_API_KEY):
    print("[FATAL] Missing environment variables.", file=sys.stderr)
    sys.exit(1)

# ===== 디스크 저장소(언어설정, 캐시 등) =====
DATA_PATH = Path(DATA_DIR)
DATA_PATH.mkdir(parents=True, exist_ok=True)

SETTINGS_PATH = DATA_PATH / "settings.json"      # 방별 설정 (예: 고정 번역 방향/사용자 프리셋)
CACHE_PATH = DATA_PATH / "cache.json"            # 간단 번역 캐시(선택적)

def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print("[WARN] load_json:", e, file=sys.stderr)
    return default

def _save_json(path: Path, obj):
    try:
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print("[WARN] save_json:", e, file=sys.stderr)

SETTINGS = _load_json(SETTINGS_PATH, default={})   # {roomId: {"mode": "auto|ko->th|th->ko"}}
CACHE = _load_json(CACHE_PATH, default={})         # {"text|src->tgt": {"out": "...", "ts": 123456}}

def persist_settings():
    _save_json(SETTINGS_PATH, SETTINGS)

def persist_cache():
    # 캐시는 너무 커지지 않도록 최근 것만 유지
    try:
        if len(CACHE) > 2000:
            # 7일 이전 항목 정리
            cutoff = time.time() - 7 * 24 * 3600
            for k in list(CACHE.keys()):
                if CACHE[k].get("ts", 0) < cutoff:
                    del CACHE[k]
        _save_json(CACHE_PATH, CACHE)
    except Exception as e:
        print("[WARN] persist_cache:", e, file=sys.stderr)

# ===== Clients =====
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
oai = OpenAI(api_key=OPENAI_API_KEY)

# ===== Helpers =====
RE_THAI   = re.compile(r"[\u0E00-\u0E7F]")
RE_HANGUL = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]")

def detect_lang_fast(text: str):
    """우선 정규식으로 빠르게, 실패 시 langdetect 보조."""
    if RE_THAI.search(text):
        return "th"
    if RE_HANGUL.search(text):
        return "ko"
    if _HAS_LANGDETECT:
        try:
            lang = detect(text)
            if lang.startswith("ko"):
                return "ko"
            if lang.startswith("th"):
                return "th"
        except Exception:
            pass
    return None

def build_system_prompt(src: str, tgt: str, short_hint: bool):
    """
    자연스러운 구어체/현지화 지시.
    짧은 한줄(감탄/슬랭/이모지)인 경우 힌트를 추가해 문맥 추론을 유도.
    """
    base_hint = (
        "번역문만 출력해. 따옴표, 라벨, 설명은 금지. "
        "원문의 공손/반말·감정/톤을 가능한 한 유지하되, 목표 언어에서 어색하지 않게 자연스럽게 다듬어."
    )
    if src == "ko" and tgt == "th":
        sys_prompt = (
            "너는 전문 한→태 통역사야. 직역을 피하고 뜻을 자연스럽게 옮겨. "
            "한국식 표현(존댓말/반말, 구어체, 인터넷 슬랭/이모지)을 태국 현지인이 쓰는 자연스러운 표현으로 바꿔. "
            + base_hint
        )
    elif src == "th" and tgt == "ko":
        sys_prompt = (
            "너는 전문 태→한 통역사야. 직역을 피하고 뜻을 자연스럽게 옮겨. "
            "태국식 표현(경어, 구어체, 감탄사/이모지)은 한국어로 어색하지 않게 바꿔. "
            + base_hint
        )
    else:
        sys_prompt = "정확하고 자연스럽게 번역해. 번역문만 출력해."

    if short_hint:
        sys_prompt += "\n짧거나 구어체만 있는 문장일 수 있어. 문맥을 추론하여 자연스럽게 다듬어."

    return sys_prompt

def pick_direction(room_id: str, text: str):
    """방 설정(고정 방향)이 있으면 우선, 없으면 자동 감지."""
    mode = SETTINGS.get(room_id, {}).get("mode", "auto")
    if mode == "ko->th":
        return "ko", "th", "🇰🇷→🇹🇭"
    if mode == "th->ko":
        return "th", "ko", "🇹🇭→🇰🇷"

    # auto
    src = detect_lang_fast(text)
    if src == "ko":
        return "ko", "th", "🇰🇷→🇹🇭"
    if src == "th":
        return "th", "ko", "🇹🇭→🇰🇷"
    return None, None, None

def translate_native(user_text: str, src: str, tgt: str, model: str):
    # 캐시(짧은 문장 위주) — 빠른 응답
    key = f"{user_text.strip()}|{src}->{tgt}"
    if len(user_text) <= 40 and key in CACHE:
        return CACHE[key]["out"]

    short_hint = len(user_text.strip()) < 6
    system_prompt = build_system_prompt(src, tgt, short_hint=short_hint)

    try:
        resp = oai.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            timeout=25,          # 5초 목표를 위해 전체 타임아웃 25초 (네트워크 여유)
            temperature=0.3,     # 오역 줄이기 위해 낮춤
            max_tokens=256,
        )
        out = (resp.choices[0].message.content or "").strip()

        if len(user_text) <= 40 and out:
            CACHE[key] = {"out": out, "ts": time.time()}
            persist_cache()
        return out
    except Exception as e:
        print("[OpenAI ERROR]", repr(e), file=sys.stderr)
        return None

def set_room_mode(room_id: str, mode: str):
    # mode: auto | ko->th | th->ko
    SETTINGS.setdefault(room_id, {})
    SETTINGS[room_id]["mode"] = mode
    persist_settings()

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
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()
    app.logger.info("[MESSAGE] %s", user_text)

    # --- 방 식별: 그룹/룸/1:1 모두 대응
    src_type = getattr(event.source, "type", "user")
    if src_type == "group":
        room_id = event.source.group_id
    elif src_type == "room":
        room_id = event.source.room_id
    else:
        room_id = event.source.user_id

    # --- 명령어(사용자 직접 설정)
    lowered = user_text.lower()
    if lowered in ("설정", "help", "도움", "명령", "명령어"):
        reply = (
            "번역봇 설정:\n"
            "• auto — 자동(한국어↔태국어)\n"
            "• ko->th — 한국어만 태국어로\n"
            "• th->ko — 태국어만 한국어로\n"
            "예) `auto`, `ko->th`, `th->ko`"
        )
        _reply(event.reply_token, reply)
        return
    if lowered in ("auto", "ko->th", "th->ko"):
        set_room_mode(room_id, lowered)
        tag = "자동" if lowered == "auto" else ("한→태" if lowered == "ko->th" else "태→한")
        _reply(event.reply_token, f"설정이 저장되었습니다: {tag}")
        return

    # --- 번역 방향 결정
    src, tgt, tag = pick_direction(room_id, user_text)
    if not (src and tgt):
        _reply(event.reply_token,
               "지원 언어는 한국어/태국어입니다.\n"
               "• 한국어 → 태국어\n• 태국어 → 한국어\n"
               "필요하면 `설정`을 입력해 모드를 바꾸세요.")
        return

    # --- 번역 수행
    out = translate_native(user_text, src, tgt, OPENAI_MODEL)
    if not out:
        _reply(event.reply_token, "번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요.")
        return

    # --- 라벨로 방향 명확화
    label = "🇰🇷→🇹🇭" if (src, tgt) == ("ko", "th") else "🇹🇭→🇰🇷"
    _reply(event.reply_token, f"{label}\n{out}")

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

# ===== Main (로컬 테스트용) =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
