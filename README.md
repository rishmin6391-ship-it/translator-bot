# Translator Bot — Production (gunicorn)
- Flask dev server 경고 제거 (gunicorn 사용)
- Python 3.11.9 고정
- LINE SDK v2 + OpenAI

## Render
Build: pip install -r requirements.txt
Start: (Procfile 자동 인식) 또는 `gunicorn app:app --workers 2 --threads 4 --timeout 60`
Webhook: https://<service>.onrender.com/callback
