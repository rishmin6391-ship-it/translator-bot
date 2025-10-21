# Translator Bot — Patch (v2.4.3 & Python 3.11)

## What's changed
- line-bot-sdk==2.4.3 (존재하는 v2 최신)
- python-3.11.9 고정 (runtime.txt, .python-version 포함)
- Reply API 타임아웃 튜플(10,30) + 재시도(10,45)

## Deploy
1) 이 파일들을 **리포지토리 루트**에 업로드/커밋
2) Render → Settings → Advanced → **Clear build cache**
3) 상단 **Manual Deploy → Deploy latest commit**
4) Build Log에서 Python 3.11.9 적용 확인
5) 브라우저로 / 열어 OK 확인 → LINE Webhook Verify
