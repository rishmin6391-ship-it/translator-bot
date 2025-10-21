# 완전정리판 — LINE Thai ↔ Korean Translator

## 포함
- Python 3.11.9 고정 (`runtime.txt`, `.python-version`)
- `line-bot-sdk==2.4.3` (v2 API)
- 타임아웃 (10,30) + 재시도 (10,45)
- Flask webhook: `/callback` GET/POST

## 배포
1) 이 파일들을 **리포 루트**에 업로드/커밋 (pyproject/poetry.lock 제거)
2) Render → New → Web Service (Root Directory 확인)
   - Build: `pip install --upgrade pip wheel setuptools && pip install --no-cache-dir -r requirements.txt`
   - Start: `python app.py`
3) `/` 접속 시 `OK` + Logs에 `[BOOT] Python: 3.11.9` 확인
4) Webhook: `https://<service>.onrender.com/callback` → Verify
