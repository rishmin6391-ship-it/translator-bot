            if retry:
                candidates.append(retry)
        except Exception as e:
            print("[OpenAI RETRY ERROR]", repr(e), file=sys.stderr)

    # 뒤에서부터 정상 후보를 고른다. 잘못된 재시도 결과가 정상 1차 결과를 덮지 않게 한다.
    out = ""
    for candidate in reversed(candidates):
        if not _needs_retry(src, tgt, text, candidate):
            out = candidate
            break

    if not out:
        # 중요: 예전처럼 `return text`를 하지 않는다.
        # 그 방식 때문에 태국어 입력이 태국어 그대로 사용자에게 출력되었다.
        return _failure_message(tgt)

    out = _restore_missing_emojis(text, out.strip())

    # 의미 전달 검수: 원문과 초안을 함께 보고 누락/반대 의미/관계어/말투를 교정한다.
    out = _review_translation(ctx, text, src, tgt, out)
    out = _restore_missing_emojis(text, out.strip())

    # 검수 결과까지 마지막으로 안전 검사한다.
    if _needs_retry(src, tgt, text, out):
        return _failure_message(tgt)

    # 정상 번역만 캐시 및 대화 문맥에 저장한다.
    _cache_put(slot, key, out)
    _push_context(slot, src, tgt, text, out)
    return out


# ===== routes =====
@app.route("/", methods=["GET"])
def home():
    return "OK", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info("[EVENT IN] %s", body)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print("[Webhook ERROR]", repr(e), file=sys.stderr)
        abort(400)
    return "OK", 200


# ===== handler =====
@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    _load_state()

    slot = _room_key(event)
    text = (event.message.text or "").strip()
    app.logger.info("[MESSAGE] %s | %s", slot, text)

    if not text:
        _reply(event.reply_token, text)
        return

    # 오역 문맥이 쌓였다고 느낄 때 LINE에서 /reset 입력하면 즉시 초기화.
    if text.lower() in {"/reset", "/clear", "번역초기화", "문맥초기화"}:
        _clear_room_context(slot)
        _reply(event.reply_token, "번역 문맥을 초기화했어요.")
        return

    detected = detect_lang(text, _get_last_lang(slot))

    # 이모지/리액션/숫자/기호/영어만 입력하면 그대로 출력
    if detected in {"echo", "en"} or detected is None:
        _reply(event.reply_token, text)
        return

    if detected == "ko":
        src, tgt = "ko", "th"
    elif detected == "th":
        src, tgt = "th", "ko"
    else:
        _reply(event.reply_token, text)
        return

    out = translate(slot, text, src, tgt)
    label = "🇰🇷→🇹🇭" if src == "ko" else "🇹🇭→🇰🇷"
    _reply(event.reply_token, f"{label}\n{out}")

    try:
        _set_last_lang(slot, src)
    except Exception as e:
        print("[STATE] set last_lang failed:", repr(e), file=sys.stderr)


def _reply(reply_token: str, text: str):
    try:
        with ApiClient(line_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text)],
                )
            )
    except Exception as e:
        print("[LINE Reply ERROR]", repr(e), file=sys.stderr)


# ===== main =====
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
