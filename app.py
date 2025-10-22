# app.py (v3 SDK 완성본)
import os, sys, json
from flask import Flask, request, abort
from dotenv import load_dotenv

# ── LINE v3 SDK ─────────────────────────────────────────────
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage
)

# ── OpenAI ─────────────────────────────────────────────────
from openai import OpenAI

# 환경변수(.env는 로컬에서만; Render는 대시보드에 변수로 입력)
load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET")
OPENAI_API_KEY            = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL              = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and OPENAI_API_KEY):
    print("[ENV ERROR] 환경변수 누락 확인:", {
        "LINE_CHANNEL_ACCESS_TOKEN": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "LINE_CHANNEL_SECRET": bool(LINE_CHANNEL_SECRET),
        "OPENAI_API_KEY": bool(OPENAI_API_KEY),
    }, file=sys.stderr)

# LINE v3 객체 준비
handler = WebhookHandler(LINE_CHANNEL_SECRET)
line_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

# OpenAI 클라이언트
client = OpenAI(api_key=OPENAI_API_KEY)

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    # 원문 이벤트를 찍어서 group/room/user 확인
    try:
        j = json.loads(body)
        print("[EVENT IN]", json.dumps(j, ensure_ascii=False), file=sys.stderr)
    except Exception as e:
        print("[EVENT PARSE WARN]", e, file=sys.stderr)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("[SIGNATURE ERROR] invalid signature", file=sys.stderr)
        abort(400)
    except Exception as e:
        print("[HANDLE ERROR]", type(e).__name__, str(e), file=sys.stderr)
        # LINE은 200만 원함. 내부 에러는 로그로만 남기고 200
    return "OK", 200

# ── 메시지 이벤트 핸들러 (텍스트) ───────────────────────────
@handler.add("message")
def on_message(event):
    """v3에서는 문자열 타입으로도 이벤트 라우팅 가능.
       Text만 처리하도록 가드 걸어줌.
    """
    try:
        if not hasattr(event, "message") or event.message.type != "text":
            return  # 스티커/이미지 등은 무시

        user_text = (event.message.text or "").strip()
        source_type = getattr(event.source, "type", "unknown")
        print("[EVENT META]", {
            "source_type": source_type,
            "user_id": getattr(event.source, "userId", None),
            "group_id": getattr(event.source, "groupId", None),
            "room_id": getattr(event.source, "roomId", None),
            "text": user_text
        }, file=sys.stderr)

        if not user_text:
            return

        # OpenAI 번역 호출
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system",
                 "content": "너는 번역 도우미야. 한국어↔태국어를 자동으로 감지해서 상대 언어로 자연스럽게 번역해. 존댓말/반말은 문맥에 맞춰 공손하게."},
                {"role": "user", "content": user_text},
            ],
            timeout=25  # reply_token 만료 전 처리 목표
        )
        translated = (completion.choices[0].message.content or "").strip()
        if not translated:
            translated = "번역 결과가 비어 있어요."

        # v3: ApiClient 컨텍스트에서 MessagingApi 사용
        with ApiClient(line_config) as api_client:
            messaging_api = MessagingApi(api_client)
            messaging_api.reply_message(
                ReplyMessageRequest(
                    replyToken=event.reply_token,
                    messages=[TextMessage(text=translated)]
                )
            )

    except Exception as e:
        print("[ERROR on_message]", type(e).__name__, str(e), file=sys.stderr)
        # 에러가 나도 reply_token은 최대한 소비하여 사용자에게 안내
        try:
            with ApiClient(line_config) as api_client:
                messaging_api = MessagingApi(api_client)
                messaging_api.reply_message(
                    ReplyMessageRequest(
                        replyToken=event.reply_token,
                        messages=[TextMessage(text="번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요.")]
                    )
                )
        except Exception as inner:
            print("[ReplyError]", inner, file=sys.stderr)

if __name__ == "__main__":
    # 로컬 실행용 (Render는 gunicorn 사용 권장)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
