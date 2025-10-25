import os
import re
import sys
import json
import time
from typing import Optional, Dict, Any, List
from collections import defaultdict, deque

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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")  # 품질 우선 (필요시 gpt-4o-mini 등으로 조정)

if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and OPENAI_API_KEY):
    print("[FATAL] Missing environment variables.", file=sys.stderr)
    sys.exit(1)

# ===== Persistent state path =====
STATE_DIR = os.getenv("TRANSLATOR_STATE_DIR", "/opt/render/persistent/translator_state")
STATE_FILE = "state.json"
STATE_PATH = os.path.join(STATE_DIR, STATE_FILE)

def _ensure_state_dir() -> str:
    """권한 문제 없는 쓰기 가능 디렉토리 확보."""
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
    return "./translator_state"

STATE_DIR = _ensure_state_dir()
STATE_PATH = os.path.join(STATE_DIR, STATE_FILE)
print(f"[STATE] Using state dir: {STATE_DIR}")

# ===== Clients =====
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
oai = OpenAI(api_key=OPENAI_API_KEY)

# ===== In-memory state (디스크 + 메모리) =====
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
            print(f"[STATE] Loaded entries: {len(_state_mem) if isinstance(_state_mem, dict) else '?'}")
        else:
            _state_mem = {}
        _state_loaded = True
        _state_last_flush = time.time()
    except Exception as e:
        print("[STATE] Load failed:", repr(e), file=sys.stderr)
        _state_mem = {}
        _state_loaded = True

def _flush_state(force: bool = False):
    """디스크 쓰기는 5초에 한 번 (burst 보호)."""
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

def _room_key(evt: MessageEvent) -> str:
    """그룹/룸/1:1 별로 고유키 생성."""
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

# ===== Language/emoji detectors =====
RE_THAI   = re.compile(r"[\u0E00-\u0E7F]")  # Thai block
RE_HANGUL = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]")  # Hangul blocks
EMOJI_REGEX = re.compile(
    r"["
    r"\U0001F600-\U0001F64F"  # emoticons
    r"\U0001F300-\U0001F5FF"  # symbols & pictographs
    r"\U0001F680-\U0001F6FF"  # transport & map symbols
    r"\U0001F1E0-\U0001F1FF"  # flags
    r"]+", flags=re.UNICODE
)
# 간단 반응(한국/태국 커뮤니티에서 흔함)
KOREAN_REACTIONS = re.compile(r"^(ㅋ+|ㅎ+|ㅠ+|ㅜ+|ㄷㄷ|ㅇㅇ|ㄴㄴ|\^\^|넵|넹|ㅇㅋ)$")
THAI_REACTIONS   = re.compile(r"^(5{2,}|555+|คริ+|คิคิ+|ฮ่า+)$")

def _looks_like_only_emoji_or_reaction(text: str) -> bool:
    s = text.strip()
    if not s:
        return True
    if EMOJI_REGEX.fullmatch(s):
        return True
    if KOREAN_REACTIONS.fullmatch(s) or THAI_REACTIONS.fullmatch(s):
        return True
    return False

def detect_lang(text: str, last_lang: Optional[str]) -> Optional[str]:
    """문자 범위 + 반응사 + 이모지. 미검출 시 최근 언어 유지."""
    if RE_THAI.search(text):
        return "th"
    if RE_HANGUL.search(text):
        return "ko"
    # 반응사/이모지 → 최근 언어 유지(없으면 None)
    if KOREAN_REACTIONS.search(text):
        return last_lang or "ko"
    if THAI_REACTIONS.search(text):
        return last_lang or "th"
    if EMOJI_REGEX.search(text):
        return last_lang
    return None

# ===== System prompt (자연스러운 현지 톤) =====
def build_system_prompt(src: str, tgt: str) -> str:
    if src == "ko" and tgt == "th":
        return (
            "너는 태국 현지인 통역사야.\n"
            "한국어를 태국어로 번역할 때 번역투를 피하고, 자연스럽고 부드러운 구어체로 표현해.\n"
            "문장의 어감, 존댓말·반말 톤을 유지하되, 상황에 맞는 자연스러운 태국어로 다듬어.\n"
            "친근한 대화는 친근하게, 격식 있는 말은 공손하게.\n"
            "원문의 의미는 바꾸지 말고, 추가 설명/따옴표/접두사 없이 번역문만 출력해."
        )
    if src == "th" and tgt == "ko":
        return (
            "너는 한국인 통역사야.\n"
            "태국어를 한국어로 번역할 때 번역투를 피하고, 실제 한국인이 쓰는 자연스러운 구어체로 표현해.\n"
            "태국식 직역은 피하고, 한국어 맥락에 맞게 어투·어감을 다듬어.\n"
            "친근한 대화는 친근하게, 예의가 필요한 상황은 공손하게.\n"
            "추가 설명/따옴표/접두사 없이 번역문만 출력해."
        )
    return "입력 문장을 자연스럽고 정확하게 번역해. 번역문만 출력해."

def build_reply_label(src: str, tgt: str) -> str:
    if src == "ko" and tgt == "th":
        return "🇰🇷→🇹🇭"
    if src == "th" and tgt == "ko":
        return "🇹🇭→🇰🇷"
    return ""

# ===== 간단 문맥 메모리(최근 3문장) =====
_context_mem: Dict[str, deque] = defaultdict(lambda: deque(maxlen=3))

def _context_key(room_key: str, src: str, tgt: str) -> str:
    return f"{room_key}:{src}->{tgt}"

def _compose_messages(system_prompt: str, context_list: List[str], current: str) -> List[Dict[str, str]]:
    msgs: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    # 이전 맥락(최대 3개)을 사용자 발화로 붙여서, 연속 대화를 반영
    for prev in context_list:
        msgs.append({"role": "user", "content": prev})
    msgs.append({"role": "user", "content": current})
    return msgs

def _chat_with_retry(messages: List[Dict[str, str]], max_retries: int = 2, timeout: int = 20) -> str:
    """429/서버 일시 오류 시 짧게 재시도."""
    delay = 0.6
    for attempt in range(max_retries + 1):
        try:
            resp = oai.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                timeout=timeout,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            if attempt < max_retries:
                time.sleep(delay)
                delay *= 1.5
                continue
            print("[OpenAI ERROR]", repr(e), file=sys.stderr)
            return "번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요."

def translate_native(room_key: str, text: str, src: str, tgt: str) -> str:
    sys_prompt = build_system_prompt(src, tgt)
    ckey = _context_key(room_key, src, tgt)
    context_list = list(_context_mem[ckey])
    _context_mem[ckey].append(text)
    msgs = _compose_messages(sys_prompt, context_list, text)
    return _chat_with_retry(msgs)

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

    # 이모지/반응만 온 경우: 안내문 출력하지 않고 원문 그대로 되돌려주기
    if _looks_like_only_emoji_or_reaction(user_text):
        _reply(event.reply_token, user_text)
        return

    # 언어 결정
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
            _reply(
                event.reply_token,
                "지원 언어는 한국어/태국어입니다.\n한국어↔태국어 문장을 보내주세요."
            )
            return

    # 번역
    out = translate_native(key, user_text, src, tgt)
    tag = build_reply_label(src, tgt)
    reply_text = f"{tag}\n{out}" if tag else out
    _reply(event.reply_token, reply_text)

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
