import os
import re
import sys
import json
import time
import hashlib
from typing import Optional, Dict, Any, List

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
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
CONSISTENCY_WINDOW_SEC = int(os.getenv("CONSISTENCY_WINDOW_SEC", "300"))

# 문맥 때문에 오역/역번역이 생길 수 있어 기본값은 OFF.
# 정말 이전 문맥이 필요하면 Render 환경변수에 USE_TRANSLATION_CONTEXT=1 설정.
USE_TRANSLATION_CONTEXT = os.getenv("USE_TRANSLATION_CONTEXT", "0") == "1"

# 캐시 버전 변경: 예전 오역 캐시가 남아 있어도 다시 쓰지 않게 함.
CACHE_VERSION = "v2_ko_th_en_emoji"

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
        _state_mem.setdefault("rooms", {})
        _loaded = True
        _last_flush = time.time()
        print("[STATE] Loaded ok")
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
    r.setdefault("context", [])
    r.setdefault("cache", {})
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
    m.update((CACHE_VERSION + "|" + slot + "|" + src + ">" + tgt + "|" + text).encode("utf-8", errors="ignore"))
    return m.hexdigest()

def _cache_get(slot: str, key: str) -> Optional[str]:
    cache: Dict[str, Any] = _room(slot)["cache"]
    item = cache.get(key)
    if not item:
        return None

    if time.time() - item.get("ts", 0) > CONSISTENCY_WINDOW_SEC:
        cache.pop(key, None)
        _flush_state()
        return None
    return item.get("out")

def _cache_put(slot: str, key: str, out: str):
    cache: Dict[str, Any] = _room(slot)["cache"]
    cache[key] = {"out": out, "ts": time.time()}

    if len(cache) > 200:
        for k in list(cache.keys())[:-200]:
            cache.pop(k, None)
    _flush_state()

# ===== detectors =====
RE_THAI   = re.compile(r"[\u0E00-\u0E7F]")
RE_HANGUL = re.compile(r"[\u1100-\u11FF\u3130-\u318F\uAC00-\uD7A3]")
RE_LATIN  = re.compile(r"[A-Za-z]")

# 최신 유니코드 이모지 범위를 넓게 포함.
EMOJI_REGEX = re.compile(
    r"(?:"
    r"[\U0001F1E6-\U0001F1FF]{2}|"          # flags
    r"[\U0001F300-\U0001F5FF]|"
    r"[\U0001F600-\U0001F64F]|"
    r"[\U0001F680-\U0001F6FF]|"
    r"[\U0001F700-\U0001F77F]|"
    r"[\U0001F780-\U0001F7FF]|"
    r"[\U0001F800-\U0001F8FF]|"
    r"[\U0001F900-\U0001F9FF]|"
    r"[\U0001FA70-\U0001FAFF]|"
    r"[\u2600-\u27BF]"
    r")(?:[\uFE0F\u200D][\U0001F300-\U0001FAFF\u2600-\u27BF])*",
    flags=re.UNICODE
)

KOREAN_REACTIONS = re.compile(r"^(ㅋ+|ㅎ+|ㅠ+|ㅜ+|ㄷㄷ|ㅇㅇ|ㄴㄴ|\^\^|넵|넹|ㅇㅋ)$")
THAI_REACTIONS   = re.compile(r"^(5{2,}|555+|คริ+|คิคิ+|ฮ่า+)$")

def _looks_like_only_emoji_or_reaction(text: str) -> bool:
    s = text.strip()
    if not s:
        return True

    # 이모지, 공백, 기호만 있으면 그대로 반환
    without_emoji = EMOJI_REGEX.sub("", s).strip()
    if without_emoji == "":
        return True

    if KOREAN_REACTIONS.fullmatch(s) or THAI_REACTIONS.fullmatch(s):
        return True
    return False

def _first_script(text: str) -> Optional[str]:
    """한국어/태국어가 섞인 경우 먼저 등장하는 문자 기준."""
    for ch in text:
        if RE_HANGUL.match(ch):
            return "ko"
        if RE_THAI.match(ch):
            return "th"
    return None

def detect_lang(text: str, last_lang: Optional[str]) -> Optional[str]:
    has_ko = bool(RE_HANGUL.search(text))
    has_th = bool(RE_THAI.search(text))
    has_en = bool(RE_LATIN.search(text))

    if has_ko and not has_th:
        return "ko"
    if has_th and not has_ko:
        return "th"
    if has_ko and has_th:
        return _first_script(text) or last_lang

    # 영어만 입력하면 번역하지 않고 영어 그대로 출력하기 위한 처리
    if has_en:
        return "en"

    if KOREAN_REACTIONS.fullmatch(text.strip()) or THAI_REACTIONS.fullmatch(text.strip()):
        return "echo"
    if _looks_like_only_emoji_or_reaction(text):
        return "echo"

    return None

# ===== prompts =====
STRICT_KO_TH = (
    "You are a professional Korean to Thai translator for LINE chat.\n"
    "Translate ONLY the user's latest message from Korean to natural Thai.\n"
    "Rules:\n"
    "1) Output only the Thai translation. Do not explain. Do not add labels or quotes.\n"
    "2) Never answer in Korean. If Korean appears in the output, it is a failure.\n"
    "3) Preserve meaning exactly. Do not add, omit, summarize, or reinterpret.\n"
    "4) Preserve numbers, dates, names, URLs, product names, and English words as-is unless they clearly need Thai localization.\n"
    "5) Preserve emojis, emoticons, punctuation mood, and line breaks as much as possible.\n"
    "6) Keep the tone: casual, polite, angry, friendly, formal, etc.\n"
)

STRICT_TH_KO = (
    "You are a professional Thai to Korean translator for LINE chat.\n"
    "Translate ONLY the user's latest message from Thai to natural Korean.\n"
    "Rules:\n"
    "1) Output only the Korean translation. Do not explain. Do not add labels or quotes.\n"
    "2) Never answer in Thai. If Thai appears in the output, it is a failure.\n"
    "3) Preserve meaning exactly. Do not add, omit, summarize, or reinterpret.\n"
    "4) Preserve numbers, dates, names, URLs, product names, and English words as-is unless they clearly need Korean localization.\n"
    "5) Preserve emojis, emoticons, punctuation mood, and line breaks as much as possible.\n"
    "6) Keep the tone: casual, polite, angry, friendly, formal, etc.\n"
)

def system_prompt(src: str, tgt: str) -> str:
    if (src, tgt) == ("ko", "th"):
        return STRICT_KO_TH
    if (src, tgt) == ("th", "ko"):
        return STRICT_TH_KO
    return "Return the user's message as-is."

def _compose_messages(sys_prompt: str, ctx: List[str], current: str) -> List[Dict[str, str]]:
    msgs: List[Dict[str, str]] = [{"role": "system", "content": sys_prompt}]

    # 기존 코드처럼 이전 문장을 user 메시지로 계속 넣으면,
    # 모델이 이전 문장까지 번역하거나 방향을 헷갈려 한국어로 답하는 일이 생길 수 있음.
    if USE_TRANSLATION_CONTEXT and ctx:
        context_text = "\n".join(ctx[-5:])
        msgs.append({
            "role": "system",
            "content": (
                "Reference context only. Do not translate the context. "
                "Translate only the latest user message.\n"
                f"{context_text}"
            )
        })

    msgs.append({"role": "user", "content": current})
    return msgs

def _chat_once(messages: List[Dict[str, str]], timeout: int = 18) -> str:
    resp = oai.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.0,
        top_p=1.0,
        presence_penalty=0,
        frequency_penalty=0,
        timeout=timeout,
    )
    return (resp.choices[0].message.content or "").strip()

def _has_wrong_script(src: str, tgt: str, out: str) -> bool:
    if tgt == "th" and RE_HANGUL.search(out):
        return True
    if tgt == "ko" and RE_THAI.search(out):
        return True
    return False

def _restore_missing_emojis(inp: str, out: str) -> str:
    """모델이 이모지를 빠뜨렸을 때 최소한 누락 이모지를 끝에 복원."""
    in_emojis = EMOJI_REGEX.findall(inp)
    if not in_emojis:
        return out

    fixed = out
    for emoji in in_emojis:
        if emoji not in fixed:
            fixed += emoji
    return fixed

def _guard_retry(slot: str, src: str, tgt: str, inp: str, out: str) -> str:
    """길이 이상 또는 역언어 출력이면 엄격 프롬프트로 1회 재시도."""
    li, lo = len(inp), len(out)
    should_retry = False

    if _has_wrong_script(src, tgt, out):
        should_retry = True

    if li >= 8:
        if lo < max(3, int(li * 0.20)) or lo > int(li * 3.0):
            should_retry = True

    if not should_retry:
        return out

    sys_p = (
        system_prompt(src, tgt)
        + "\nCritical correction: Your previous output used the wrong language or changed the length too much. "
          "Translate the latest message again into the target language only."
    )
    msgs = _compose_messages(sys_p, _get_context(slot), inp)

    try:
        retry_out = _chat_once(msgs, timeout=18)
        if retry_out:
            return retry_out
    except Exception as e:
        print("[OpenAI RETRY ERROR]", repr(e), file=sys.stderr)

    return out

def translate(slot: str, text: str, src: str, tgt: str) -> str:
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
        out = _restore_missing_emojis(text, out)
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

    detected = detect_lang(text, _get_last_lang(slot))

    # 이모지/리액션/숫자/기호/영어만 입력하면 그대로 출력
    # 예: 😂 / ㅋㅋㅋ / OK / Thank you / 010-0000-0000
    if detected in {"echo", "en"} or detected is None:
        _reply(event.reply_token, text)
        return

    if detected == "ko":
        src, tgt = "ko", "th"
    elif detected == "th":
        src, tgt = "th", "ko"
    else:
        _reply(event.reply_token, text)
        return

    out = translate(slot, text, src, tgt)
    label = "🇰🇷→🇹🇭" if src == "ko" else "🇹🇭→🇰🇷"
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
