# Translator Bot — 프로덕션 튜닝판 (Render/Gunicorn)

## 핵심
- Flask 개발 서버 대신 **gunicorn** 사용 → 경고 제거 + 동시성 개선
- Python **3.11.9** 고정 (`runtime.txt`, `.python-version`)
- 타임아웃 상향: LINE (15,60), OpenAI 60초, 실패 시 재시도
- 워커/스레드/keep-alive/프리로드 튜닝

## 배포 (Render)
1) 이 파일들을 **리포지토리 루트**에 커밋
2) Render → New → Web Service (혹은 기존 서비스 Manual Deploy)
   - Build Command:
     ```
     pip install --upgrade pip wheel setuptools && pip install --no-cache-dir -r requirements.txt
     ```
   - Start Command: (Procfile 자동 인식) 또는
     ```
     gunicorn app:app --workers 2 --threads 8 --timeout 60 --graceful-timeout 30 --keep-alive 75 --max-requests 1000 --max-requests-jitter 100 --preload
     ```
3) 배포 후 `https://<service>.onrender.com/` 접속 → **OK** 확인
4) LINE Webhook URL:
   ```
   https://<service>.onrender.com/callback
   ```
   Use Webhook ON → Verify

## 속도 개선 팁
- Free 플랜: 콜드 스타트 있으므로 UptimeRobot/Better Stack으로 `GET /` 3분 간격 핑 추천
- 의존성 최소화(본 파일 구성 기준) / Poetry 비활성화(pyproject.toml/poetry.lock 제거)
- 로그 과다 출력 줄이기

## 사용
- 일반 모드: 한글/태국어 아무 말 → 자동 번역
- 강제 모드: `/ko <텍스트>`, `/th <텍스트>`
