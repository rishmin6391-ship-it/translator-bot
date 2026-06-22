# app.py — LINE v3 + OpenAI + Persistent Disk 저장(그룹별 언어 설정)

import os
import re
import sys
import json
import threading
from typing import Optional, Tuple
from flask import Flask, request, abort

# ===== LINE v3 SDK =====
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage,
)

# ===== OpenAI =====
from openai import OpenAI

# -----------------------------------------------------------
# 환경 변수
# -----------------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY            = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL              = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and OPENAI_API_KEY):
    print("[FATAL] Missing env: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET / OPENAI_API_KEY", file=sys.stderr)
    sys.exit(1)

# -----------------------------------------------------------
# Flask & Clients
# -----------------------------------------------------------
app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["JSONIFY_PRETTYPRINT_REGULAR"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler     = WebhookHandler(LINE_CHANNEL_SECRET)
oai         = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------------------------------------
# Persistent Disk 경로 (/var/data). 실패 시 /tmp 로 폴백(비영구)
# -----------------------------------------------------------
DATA_DIR_ENV = os.getenv("DATA_DIR", "/var/data")
GROUP_LANG_PATH = None
SETTINGS_LOCK = threading.RLock()

def _ensure_data_dir() -> str:
    base = DATA_DIR_ENV
    try:
        os.makedirs(base, exist_ok=True)
        # 쓰기 가능 여부 간단 체크
        test_file = os.path.join(base, ".writetest")
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        return base
    except Exception as e:
        fallback = "/tmp/botdata"
        try:
            os.makedirs(fallback, exist_ok=True)
        except Exception:
            pass
        print(f"[WARN] Cannot write to {base}: {e}. Fallback to {fallback} (NOT persistent).", file=sys.stderr)
        return fallback

DATA_DIR = _ensure_data_dir()
GROUP_LANG_PATH = os.path.join(DATA_DIR, "group_lang.json")

# -----------------------------------------------------------
# 언어 코드/별칭 매핑 (주요 20개 언어)
# 사용자가 ko, 한국어, korean 등으로 설정해도 코드 'ko'로 매핑
# -----------------------------------------------------------
LANG_ALIASES = {
    "ko": "ko", "한국어": "ko", "korean": "ko",
    "th": "th", "태국어": "th", "thai": "th",
    "en": "en", "영어": "en", "english": "en",
    "ja": "ja", "일본어": "ja", "japanese": "ja",
    "zh": "zh", "중국어": "zh", "chinese": "zh", "zh-cn": "zh", "zh-tw": "zh",
    "es": "es", "스페인어": "es", "spanish": "es",
    "fr": "fr", "프랑스어": "fr", "french": "fr",
    "de": "de", "독일어": "de", "german": "de",
    "it": "it", "이탈리아어": "it", "italian": "it",
    "ru": "ru", "러시아어": "ru", "russian": "ru",
    "vi": "vi", "베트남어": "vi", "vietnamese": "vi",
    "id": "id", "인도네시아어": "id", "indonesian": "id",
    "ms": "ms", "말레이어": "ms", "malay": "ms",
    "ar": "ar", "아랍어": "ar", "arabic": "ar",
    "hi": "hi", "힌디어": "hi", "hindi": "hi",
    "pt": "pt", "포르투갈어": "pt", "portuguese": "pt",
    "tr": "tr", "터키어": "tr", "turkish": "tr",
    "fa": "fa", "페르시아어": "fa", "persian": "fa", "farsi": "fa",
    "he": "he", "히브리어": "he", "hebrew": "he",
    "fil": "fil", "tl": "fil", "타갈로그어": "fil", "tagalog": "fil",
}

# -----------------------------------------------------------
# 간단한 문자 범위 기반 언어 감지 (ko/th 우선)
# -----------------------------------------------------------
RE_THAI   = re.compile(r"[\u0E00-\u0E7F]")
RE_HANGUL = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]")

def detect_lang(text: str) -> Optional[str]:
    if RE_THAI.search(text):
        return "th"
    if RE_HANGUL.search(text):
        return "ko"
    # 기타는 None (모델 감지에 맡겨도 되지만 latency 고려해 여기선 None)
    return None

# -----------------------------------------------------------
# 설정 파일 I/O
# 구조 예) {"pairs": {"group:xxxxx": ["ko","th"], "user:yyyy":"en","ja"]}}
# -----------------------------------------------------------
def _load_pairs() -> dict:
    try:
        if os.path.exists(GROUP_LANG_PATH):
            with open(GROUP_LANG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], dict):
                    return data["pairs"]
    except Exception as e:
        print(f"[WARN] load_pairs error: {e}", file=sys.stderr)
    return {}

def _save_pairs(pairs: dict):
    try:
        tmp = GROUP_LANG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"pairs": pairs}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, GROUP_LANG_PATH)
    except Exception as e:
        print(f"[ERROR] save_pairs error: {e}", file=sys.stderr)

PAIRS = _load_pairs()

def get_chat_key(event: MessageEvent) -> str:
    s = event.source
    stype = getattr(s, "type", None)
    if stype == "group":
        gid = getattr(s, "group_id", None) or getattr(s, "groupId", None)
        return f"group:{gid}"
    elif stype == "room":
        rid = getattr(s, "room_id", None) or getattr(s, "roomId", None)
        return f"room:{rid}"
    else:
        uid = getattr(s, "user_id", None) or getattr(s, "userId", None)
        return f"user:{uid}"

def set_pair(chat_key: str, a_code: str, b_code: str):
    with SETTINGS_LOCK:
        PAIRS[chat_key] = [a_code, b_code]
        _save_pairs(PAIRS)

def get_pair(chat_key: str) -> Optional[Tuple[str, str]]:
    with SETTINGS_LOCK:
        p = PAIRS.get(chat_key)
        if isinstance(p, list) and len(p) == 2:
            return p[0], p[1]
    return None

# -----------------------------------------------------------
# 명령 파싱: !lang ko-th / !언어 한국어-태국어 / lang en ja ...
# -----------------------------------------------------------
CMD_REGEX = re.compile(
    r"^\s*[!/]*(?:lang|언어|설정)\s+([A-Za-z가-힣\-]+)(?:\s*[->\u2192~]\s*|\s+)([A-Za-z가-힣\-]+)\s*$",
    re.IGNORECASE,
)

def normalize_lang(token: str) -> Optional[str]:
    token = token.strip().lower()
    return LANG_ALIASES.get(token)

def parse_lang_command(text: str) -> Optional[Tuple[str, str]]:
    m = CMD_REGEX.match(text)
    if not m:
        return None
    a_raw, b_raw = m.group(1), m.group(2)
    a = normalize_lang(a_raw)
    b = normalize_lang(b_raw)
    if a and b and a != b:
        return a, b
    return None

# -----------------------------------------------------------
# 시스템 프롬프트 (네이티브 톤, 라이트)
# -----------------------------------------------------------
def build_system_prompt(src: str, tgt: str) -> str:
    return (
        f"역할: 실시간 통역사. 소스 {src} → 타겟 {tgt}.\n"
        "원문의 말투/존댓말·반말과 뉘앙스를 유지하되, 타겟 언어의 자연스러운 구어체로 번역해.\n"
        "불필요한 설명/따옴표/괄호 없이 '번역문만' 출력."
    )

# -----------------------------------------------------------
# OpenAI 호출
# -----------------------------------------------------------
def translate_text(user_text: str, src: str, tgt: str) -> str:
    system_prompt = build_system_prompt(src, tgt)
    resp = oai.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_text},
        ],
        timeout=20,   # 지연 최소화를 위해 20초
    )
    return (resp.choices[0].message.content or "").strip()

def build_tag(src: str, tgt: str) -> str:
    FLAG = {
        "ko":"🇰🇷", "th":"🇹🇭", "en":"🇺🇸", "ja":"🇯🇵", "zh":"🇨🇳",
        "es":"🇪🇸", "fr":"🇫🇷", "de":"🇩🇪", "it":"🇮🇹", "ru":"🇷🇺",
        "vi":"🇻🇳", "id":"🇮🇩", "ms":"🇲🇾", "ar":"🇸🇦", "hi":"🇮🇳",
        "pt":"🇵🇹", "tr":"🇹🇷", "fa":"🇮🇷", "he":"🇮🇱", "fil":"🇵🇭",
    }
    return f"{FLAG.get(src, src)}→{FLAG.get(tgt, tgt)}"

# -----------------------------------------------------------
# 라우트
# -----------------------------------------------------------
@app.route("/", methods=["GET"])
def home():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        app.logger.info("[EVENT IN] %s", body[:2000])
        handler.handle(body, signature)
    except Exception as e:
        print("[Webhook ERROR]", repr(e), file=sys.stderr)
        abort(400)
    return "OK", 200

# -----------------------------------------------------------
# 메시지 핸들러
# -----------------------------------------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    user_text = (event.message.text or "").strip()
    chat_key  = get_chat_key(event)
    app.logger.info("[MESSAGE] %s (%s)", user_text, chat_key)

    # 1) 언어 설정 명령 처리
    cmd = parse_lang_command(user_text)
    if cmd:
        a, b = cmd
        set_pair(chat_key, a, b)
        msg = f"언어 설정 저장됨: {a} ↔ {b}\n이제 이 채팅방에서는 두 언어 간 자동 번역을 합니다."
        _reply(event.reply_token, msg)
        return

    # 2) 이 방의 언어쌍 가져오기 (없으면 ko↔th로 초기화)
    pair = get_pair(chat_key)
    if not pair:
        # 기본값 저장(한국어↔태국어)
        set_pair(chat_key, "ko", "th")
        pair = ("ko", "th")

    a, b = pair

    # 3) 방향 결정 (간단 감지)
    src_detected = detect_lang(user_text)
    if   src_detected == a: src, tgt = a, b
    elif src_detected == b: src, tgt = b, a
    else:
        # 감지 실패: 기본 a를 소스, b를 타겟으로 가정
        src, tgt = a, b

    # 4) 번역 실행
    try:
        out = translate_text(user_text, src, tgt)
        tag = build_tag(src, tgt)
        reply_text = f"{tag}\n{out}"
    except Exception as e:
        print("[OpenAI ERROR]", repr(e), file=sys.stderr)
        reply_text = "번역 중 오류가 발생했어요. 잠시 후 다시 시도해주세요."

    _reply(event.reply_token, reply_text)

# -----------------------------------------------------------
# LINE Reply Helper
# -----------------------------------------------------------
def _reply(reply_token: str, text: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text[:5000])]
                )
            )
    except Exception as e:
        print("[LINE Reply ERROR]", repr(e), file=sys.stderr)

# -----------------------------------------------------------
# 로컬 실행 (Render는 gunicorn Start Command 사용)
# -----------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
