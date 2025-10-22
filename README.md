# LINE Translator Bot (Korean ↔ Thai) — LINE SDK v3

## 1) 준비물
- LINE Official Account (Bot), Messaging API 활성화
- LINE Developers Console에서:
  - Channel secret / Channel access token 확보
  - "그룹 초대 허용" ON
  - "응답 모드"가 UI에 없다면 기본 Bot 모드로 동작 (SDK v3 기준, 웹훅만 켜져 있으면 됩니다)

## 2) 환경변수
- LINE_CHANNEL_SECRET
- LINE_CHANNEL_ACCESS_TOKEN
- OPENAI_API_KEY
- (선택) OPENAI_MODEL=gpt-4o-mini

## 3) 배포(Render)
- 리포 업로드
- runtime.txt 에 python-3.11.9
- requirements.txt, Procfile 포함
- Build Command:
  pip install --upgrade pip wheel setuptools && pip install --no-cache-dir -r requirements.txt
- Start Command:
  gunicorn app:app --bind 0.0.0.0:${PORT} --workers 2 --threads 8 --timeout 60 --graceful-timeout 30 --keep-alive 75 --max-requests 1000 --max-requests-jitter 100 --preload

## 4) Webhook 설정
- 배포 URL 예: https://<your-service>.onrender.com
- LINE Console → Messaging API → Webhook URL:
  https://<your-service>.onrender.com/callback
- Use webhook: ON → Verify (200 OK)

## 5) 테스트
- 1:1 채팅 또는 그룹방에 봇 초대
- 한국어/태국어로 아무 말 입력 → 상호 번역되어 응답
