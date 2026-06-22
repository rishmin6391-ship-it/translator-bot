import os
import re
import sys
import json
import time
import hashlib
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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")     # 품질 우선
CONSISTENCY_WINDOW_SEC = int(os.getenv("CONSISTENCY_WINDOW_SEC", "300"))  # 동일입력 캐시 유지(기본 5분)

if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and OPENAI_API_KEY):
    print("[FATAL] Missing environment variables.", file=sys.stderr)
    sys.exit(1)

# ===== Persistent state path =====
STATE_DIR = os.getenv("TRANSLATOR_STATE_DIR", "/opt/render/persistent/translator_state")
STATE_FILE = "state.json"
STATE_PATH = os.path.join(STATE_DIR, STATE_FILE)

def _ensure_state_dir() -> str:
    for p in [STATE_DIR, "/opt/render/persistent/translator_state", "./translator_state"]:
        try:
            os.makedirs(p, exist_ok=True)
            tf = os.path.join(p, ".touch")
            with open(tf, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(tf)
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

# ===== In-memory state =====
_state_mem: Dict[str, Any] = {}
_loaded = False
_last_flush = 0.0

def _load_state():
    global _state_mem, _loaded, _last_flush
    if _loaded:
        return
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                _state_mem = json.load(f)
        else:
            _state_mem = {}
        # 업그레이드 호환: 기본 구조 보장
        _state_mem.setdefault("rooms", {})     # room_key -> { last_lang, context(list), cache(dict) }
        _loaded = True
        _last_flush = time.time()
        print(f"[STATE] Loaded ok")
    except Exception as e:
        print("[STATE] Load failed:", repr(e), file=sys.stderr)
        _state_mem = {"rooms": {}}
        _loaded = True

def _flush_state(force: bool = False):
    global _last_flush
    now = time.time()
    if not force and (now - _last_flush) < 3.0:
        return
    try:
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state_mem, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
        _last_flush = now
    except Exception as e:
        print("[STATE] Flush failed:", repr(e), file=sys.stderr)

def _room_key(evt: MessageEvent) -> str:
    src = evt.source
    if src.type == "group":
        return f"group:{src.group_id}"
    if src.type == "room":
        return f"room:{src.room_id}"
    return f"user:{src.user_id}"

def _room(slot: str) -> Dict[str, Any]:
    r = _state_mem["rooms"].setdefault(slot, {})
    r.setdefault("last_lang", None)
    r.setdefault("context", [])  # 최근 원문 queue
    r.setdefault("cache", {})    # 입력 해시 -> {out, ts}
    return r

def _set_last_lang(slot: str, lang: str):
    _room(slot)["last_lang"] = lang
    _flush_state()

def _get_last_lang(slot: str) -> Optional[str]:
    return _room(slot).get("last_lang")

def _push_context(slot: str, text: str, maxlen: int = 5):
    ctx = _room(slot)["context"]
    ctx.append(text)
    if len(ctx) > maxlen:
        del ctx[0]
    _flush_state()

def _get_context(slot: str) -> List[str]:
    return list(_room(slot)["context"])

def _hash_key(slot: str, src: str, tgt: str, text: str) -> str:
    m = hashlib.sha256()
    m.update((slot + "|" + src + ">" + tgt + "|" + text).encode("utf-8", errors="ignore"))
    return m.hexdigest()

def _cache_get(slot: str, key: str) -> Optional[str]:
    cache: Dict[str, Any] = _room(slot)["cache"]
    item = cache.get(key)
    if not item:
        return None
    # 시간 검사
    if time.time() - item.get("ts", 0) > CONSISTENCY_WINDOW_SEC:
        cache.pop(key, None)
        _flush_state()
        return None
    return item.get("out")

def _cache_put(slot: str, key: str, out: str):
    cache: Dict[str, Any] = _room(slot)["cache"]
    cache[key] = {"out": out, "ts": time.time()}
    # 캐시 사이즈 제한(최근 200개)
    if len(cache) > 200:
        for k in list(cache.keys())[:-200]:
            cache.pop(k, None)
    _flush_state()

# ===== detectors =====
RE_THAI   = re.compile(r"[\u0E00-\u0E7F]")
RE_HANGUL = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]")
EMOJI_REGEX = re.compile(
    r"["
    r"\U0001F600-\U0001F64F"
    r"\U0001F300-\U0001F5FF"
    r"\U0001F680-\U0001F6FF"
    r"\U0001F1E0-\U0001F1FF"
    r"]+",
    flags=re.UNICODE
)
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
    if RE_THAI.search(text):
        return "th"
    if RE_HANGUL.search(text):
        return "ko"
    if KOREAN_REACTIONS.search(text):
        return last_lang or "ko"
    if THAI_REACTIONS.search(text):
        return last_lang or "th"
    if EMOJI_REGEX.search(text):
        return last_lang
    return None

# ===== prompts =====
STRICT_KO_TH = (
    "역할: 전문 통역사(한→태)\n"
    "규칙:\n"
    "1) 의미 보존(추가/삭제/각색 금지), 2) 고유명사/숫자 보존, 3) 자연스러운 태국어 구어체\n"
    "4) 존댓말·반말 등 어감을 유지하되 태국어 문맥에 맞게만 최소한으로 조정\n"
    "5) 번역문만 출력(설명/따옴표/접두사 금지)\n"
)

STRICT_TH_KO = (
    "역할: 전문 통역사(태→한)\n"
    "규칙:\n"
    "1) 의미 보존(추가/삭제/각색 금지), 2) 고유명사/숫자 보존, 3) 자연스러운 한국어 구어체\n"
    "4) 태국식 직역 피하고 한국어 문맥에 맞게만 최소한으로 조정\n"
    "5) 번역문만 출력(설명/따옴표/접두사 금지)\n"
)

def system_prompt(src: str, tgt: str) -> str:
    return STRICT_KO_TH if (src, tgt) == ("ko","th") else STRICT_TH_KO

def _compose_messages(sys_prompt: str, ctx: List[str], current: str) -> List[Dict[str, str]]:
    msgs: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]
    for prev in ctx[-5:]:  # 최근 5개까지만
        msgs.append({"role": "user", "content": prev})
    msgs.append({"role": "user", "content": current})
    return msgs

def _chat_once(messages: List[Dict[str, str]], timeout: int = 18) -> str:
    resp = oai.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.0,         # 결정론 강화
        top_p=1.0,
        presence_penalty=0,
        frequency_penalty=0,
        timeout=timeout,
    )
    return (resp.choices[0].message.content or "").strip()

def _guard_retry(slot: str, src: str, tgt: str, inp: str, out: str) -> str:
    """산출 길이가 입력 대비 과도하게 짧거나 길면 보수 프롬프트로 1회 재시도."""
    li, lo = len(inp), len(out)
    if li >= 8:
        if lo < max(3, int(li*0.25)) or lo > int(li*2.5):
            sys_p = system_prompt(src, tgt) + "\n추가 규칙: 원문 의미를 절대 바꾸지 말고, 길이감도 과도하게 바뀌지 않게 하라."
            msgs = _compose_messages(sys_p, _get_context(slot), inp)
            try:
                return _chat_once(msgs, timeout=18)
            except Exception as e:
                print("[OpenAI RETRY ERROR]", repr(e), file=sys.stderr)
                return out
    return out

def translate(slot: str, text: str, src: str, tgt: str) -> str:
    # 캐시: 동일 입력은 CONSISTENCY_WINDOW_SEC 내 동일 결과 반환
    key = _hash_key(slot, src, tgt, text)
    cached = _cache_get(slot, key)
    if cached is not None:
        return cached

    sp = system_prompt(src, tgt)
    ctx = _get_context(slot)
    msgs = _compose_messages(sp, ctx, text)

    try:
        out = _chat_once(msgs)
        out = _guard_retry(slot, src, tgt, text, out)
    except Exception as e:
        print("[OpenAI ERROR]", repr(e), file=sys.stderr)
        out = "번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요."

    _cache_put(slot, key, out)
    _push_context(slot, text)
    return out

# ===== routes =====
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

# ===== handler =====
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    _load_state()

    slot = _room_key(event)
    text = (event.message.text or "").strip()
    app.logger.info("[MESSAGE] %s | %s", slot, text)

    # 이모지/반응만 오면 그대로 되돌려줌(안내문 방지)
    if _looks_like_only_emoji_or_reaction(text):
        _reply(event.reply_token, text)
        return

    last_lang = _get_last_lang(slot)
    detected = detect_lang(text, last_lang)

    if detected == "ko":
        src, tgt = "ko", "th"
    elif detected == "th":
        src, tgt = "th", "ko"
    else:
        if last_lang in {"ko", "th"}:
            src = last_lang
            tgt = "th" if last_lang == "ko" else "ko"
        else:
            _reply(event.reply_token, "지원 언어는 한국어/태국어입니다.\n한국어↔태국어 문장을 보내주세요.")
            return

    out = translate(slot, text, src, tgt)
    label = "🇰🇷→🇹🇭" if src=="ko" else "🇹🇭→🇰🇷"
    _reply(event.reply_token, f"{label}\n{out}")

    try:
        _set_last_lang(slot, src)
    except Exception as e:
        print("[STATE] set last_lang failed:", repr(e), file=sys.stderr)

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

# ===== main =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
