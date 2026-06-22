from flask import Flask, request, abort
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
import google.generativeai as genai
import os, sys, re

app = Flask(__name__)

# --- 환경 변수 ---
# LINE 설정
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

# Gemini 설정 (OpenAI 대신 사용)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# 무료 티어에서 사용 가능한 모델 (gemini-1.5-flash가 빠르고 무료 티어 제한이 넉넉함)
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# --- 클라이언트 초기화 ---
config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Gemini 초기화
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(MODEL_NAME)

# --- 언어 감지 ---
RE_THAI = re.compile(r"[\u0E00-\u0E7F]")
RE_KO   = re.compile(r"[\uAC00-\uD7A3]")

def detect_lang(t):
    if RE_THAI.search(t): return "th"
    if RE_KO.search(t): return "ko"
    return None

def get_translate_prompt(src, tgt):
    """
    번역 품질을 높이기 위한 개선된 시스템 프롬프트
    """
    return f"""당신은 한국어와 태국어에 능통한 전문 번역가입니다.
다음 지침에 따라 입력된 텍스트를 {src}에서 {tgt}로 번역하세요:

1. 자연스러운 구어체 사용: 친구나 지인과 대화하는 듯한 자연스러운 말투를 사용하세요.
2. 문화적 맥락 유지: 직역보다는 해당 국가의 문화와 상황에 맞는 적절한 표현을 선택하세요.
3. 뉘앙스 보존: 원문의 감정, 존댓말/반말 여부, 어조를 최대한 살리세요.
4. 불필요한 설명 금지: 오직 번역된 결과물만 출력하세요. 추가적인 설명이나 인사말은 생략합니다.
5. 고유명사 보호: 이름, 장소 등 고유명사는 틀리지 않게 주의하세요.

번역할 텍스트:
"""

@app.route("/", methods=["GET"])
def home(): return "Gemini Translation Bot is running!", 200

@app.route("/callback", methods=["POST"])
def callback():
    sig = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except Exception as e:
        print("[ERROR]", e, file=sys.stderr)
        abort(400)
    return "OK", 200

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    text = event.message.text.strip()
    lang = detect_lang(text)

    if lang == "ko":
        src, tgt, label = "한국어", "태국어", "🇰🇷→🇹🇭"
    elif lang == "th":
        src, tgt, label = "태국어", "한국어", "🇹🇭→🇰🇷"
    else:
        # 한국어나 태국어가 아닌 경우 응답하지 않거나 안내 메시지 전송
        # reply(event.reply_token, "한국어 또는 태국어만 번역 가능합니다.")
        return

    try:
        # Gemini API 호출 (무료 티어 사용)
        prompt = get_translate_prompt(src, tgt) + text
        
        # 안전 설정 및 생성 설정 (필요시 조절 가능)
        response = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.3, # 약간의 창의성을 주어 더 자연스러운 번역 유도
            )
        )
        
        out = response.text.strip()
        
        # 결과 전송
        reply(event.reply_token, f"{label}\n{out}")
        
    except Exception as e:
        print("[Gemini ERROR]", e, file=sys.stderr)
        reply(event.reply_token, "번역 중 문제가 발생했어요. 잠시 후 다시 시도해주세요.")

def reply(token, text):
    with ApiClient(config) as client:
        MessagingApi(client).reply_message(
            ReplyMessageRequest(
                reply_token=token, 
                messages=[TextMessage(text=text)]
            )
        )

if __name__ == "__main__":
    # 포트는 환경 변수에서 가져오거나 기본값 10000 사용
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
