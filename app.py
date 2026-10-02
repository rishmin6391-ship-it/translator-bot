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

# 번역 품질 우선 설정.
# - 1차 번역: gpt-5.6-terra (속도/비용 균형)
# - 재번역/의미 검수: gpt-5.6-sol (정확도 우선)
# 비용을 줄이려면 Render 환경변수에서 REVIEW_TRANSLATION=0 으로 끌 수 있다.
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-terra")
OPENAI_RETRY_MODEL = os.getenv("OPENAI_RETRY_MODEL", "gpt-5.6-sol")
OPENAI_REVIEW_MODEL = os.getenv("OPENAI_REVIEW_MODEL", "gpt-5.6-sol")
# 아주 오래된 OpenAI SDK에서 Responses API가 없을 때만 사용하는 호환 모델.
OPENAI_COMPAT_MODEL = os.getenv("OPENAI_COMPAT_MODEL", "gpt-4o")

OPENAI_TIMEOUT_SEC = float(os.getenv("OPENAI_TIMEOUT_SEC", "20"))
OPENAI_REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "low")
REVIEW_TRANSLATION = os.getenv("REVIEW_TRANSLATION", "1") == "1"
CONSISTENCY_WINDOW_SEC = int(os.getenv("CONSISTENCY_WINDOW_SEC", "300"))

# 자연스러운 대화 번역을 위해 최근 문맥 2개만 참고한다.
# 문맥은 '참고용'이며 절대 다시 번역하지 않는다.
USE_TRANSLATION_CONTEXT = os.getenv("USE_TRANSLATION_CONTEXT", "1") == "1"
CONTEXT_MAXLEN = max(0, min(3, int(os.getenv("TRANSLATION_CONTEXT_MESSAGES", "2"))))

# 예전 캐시/문맥과 섞이지 않도록 버전 갱신.
STATE_VERSION = "v6_native_meaning_translation"
CACHE_VERSION = "v6_native_meaning_translation"

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
