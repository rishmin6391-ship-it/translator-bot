# app.py (LINE SDK v3 + Flask + OpenAI 번역 봇 - 완전판)
import os
import re
import sys
from flask import Flask, request, abort

# ===== LINE v3 SDK =====
from linebot.v3.webhook import WebhookHandler
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from linebot.v3.messaging import (
    MessagingApi,
    Configuration,
    ApiClient,
    ReplyMessageRequest,
    TextMessage as V3TextMessage,
)

# ===== OpenAI =====
from openai import OpenAI
from openai import RateLimitError

# -----------------------------------------------------------------------------
# 환경 변수
# -----------------------------------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("[BOOT] Missing LINE credentials (LINE_CHANNEL_ACCESS_TOKEN/LINE_CHANNEL_SECRET).", file=sys.stderr)

if not OPENAI_API_KEY:
    print("[BOOT] Missing OPENAI_API_KEY.", file=sys.stderr)

# -----------------------------------------------------------------------------
# 초기화
# -----------------------------------------------------------------------------
app = Flask(__name__)
handler = WebhookHandler(LINE_CHANNEL_SECRET or "")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN or "")

# -----------------------------------------------------------------------------
# 유틸
# -----------------------------------------------------------------------------
def strip_bot_mention(text: str) -> str:
    """
    그룹에서 @멘션/봇 이름 등을 제거.
    예) "@Kira Translator2 안녕" -> "안녕"
    """
    # 라인 멘션 토큰 형태 제거(@xxxxx)
    t = re.sub(r"@\S+\s*", "", text)
    return t.strip()

def translate_ko_th(text: str, timeout: int = 30) -> str:
    """
    한국어 <-> 태국어 양방향 번역.
      - 입력이 한국어면 태국어로만
      - 입력이 태국어면 한국어로만
      - 그 외 언어면 안내 한 줄
    """
    system = "Strict bilingual translator (KO<->TH). Return ONLY the translated text."

    user_prompt = f"""
당신은 엄격한 양방향 번역 엔진입니다.

규칙:
1) 입력 언어가 한국어(KO)이면 출력은 태국어(TH)로만.
2) 입력 언어가 태국어(TH)이면 출력은 한국어(KO)로만.
3) 다른 언어일 경우: "지원 언어는 한국어/태국어입니다." 한 줄만 한국어로 출력.
4) 출력에는 설명, 원문, 따옴표, 괄호, 언어 태그, 접두사/접미사 등을 절대 포함하지 말 것.
5) 이모지/URL/고유명사(이름, 지명, 브랜드)는 원칙적으로 보존.
6) 문체는 자연스럽고 간결하게. 존댓말/반말은 원문의 톤을 최대한 따라감.

입력:
{text}
    """.strip()

    resp = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        timeout=timeout,
    )
    out = (resp.choices[0].message.content or "").strip()
    # 혹시 생길 수 있는 불필요한 포맷 제거
    return out.strip().strip("`").strip()

def reply_text(reply_token: str, text: str):
    """LINE v3로 텍스트 회신"""
    with ApiClient(line_config) as api_client:
        messaging_api = MessagingApi(api_client)
        messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[V3TextMessage(text=text[:5000])]  # 안전상 길이 제한
            )
        )

# -----------------------------------------------------------------------------
# 라우트
# -----------------------------------------------------------------------------
@app.get("/")
def health():
    # Render/Load balancer 헬스체크용
    return "OK", 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    # 디버깅 로그
    print("[EVENT IN]", body, file=sys.stdout)

    if not signature:
        # 시그니처 없으면 400
        return "Bad Request: missing signature", 400

    try:
        handler.handle(body, signature)
    except Exception as e:
        # 시그니처 오류 포함 모든 오류 -> 400
        print("[WEBHOOK ERROR]", type(e).__name__, str(e), file=sys.stderr)
        return "Bad Request", 400

    return "OK", 200

# -----------------------------------------------------------------------------
# 이벤트 핸들러 (v3 데코레이터)
# -----------------------------------------------------------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    try:
        user_text = (event.message.text or "").strip()
        print("[MESSAGE]", user_text, file=sys.stdout)

        # 그룹 멘션 제거
        clean_text = strip_bot_mention(user_text)
        if not clean_text:
            return  # 빈 입력은 무시

        # 번역
        translated = translate_ko_th(clean_text)

        # 회신
        reply_text(event.reply_token, translated)

    except RateLimitError as e:
        # OpenAI 크레딧/쿼터 초과
        print("[OpenAI RateLimit]", str(e), file=sys.stderr)
        try:
            reply_text(event.reply_token, "번역 사용량이 초과되었습니다. 잠시 후 다시 시도해주세요.")
        except Exception:
            pass
    except Exception as e:
        print("[ERROR]", type(e).__name__, str(e), file=sys.stderr)
        try:
            reply_text(event.reply_token, "번역 중 오류가 발생했어요. 잠시 후 다시 시도해주세요.")
        except Exception:
            pass

# -----------------------------------------------------------------------------
# 로컬 실행 (Render에서는 Procfile/Start command로 gunicorn 권장)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
