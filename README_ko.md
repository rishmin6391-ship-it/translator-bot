# LINE 통역봇 (한국어↔태국어, v3 SDK)

이 패키지는 **바로 실행** 가능한 통역봇입니다.  
압축을 풀고 **키만 입력**하면 로컬/Render에서 곧바로 구동됩니다.

## 1) 로컬 실행
```bash
cp .env.example .env
# .env 파일을 열어 LINE 채널 시크릿/액세스 토큰, OPENAI_API_KEY 입력
pip install -r requirements.txt
python app.py
```
- 브라우저 http://localhost:10000 → OK 확인
- LINE Developers > Webhook URL: `http://<내IP 또는 터널주소>/callback` → Verify

## 2) Render (Starter + Persistent Disk)
- Service 생성 후 Environment에 다음 입력:
```
LINE_CHANNEL_SECRET=...
LINE_CHANNEL_ACCESS_TOKEN=...
OPENAI_API_KEY=sk-...
PERSIST_DIR=/var/data
```
- Disks > Add Disk → mountPath `/var/data`
- Start Command:
```
gunicorn app:app --bind 0.0.0.0:${PORT} --workers 2 --threads 8 --timeout 60 --keep-alive 75 --preload
```

## 3) 채팅방 명령어
- `/help` : 도움말
- `/show` : 현재 설정 보기
- `/mode auto|ko-th|th-ko|off`
- `/formal auto|casual|formal`
- `/native on|off`
- `/tag on|off`
