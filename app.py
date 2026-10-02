    else:
        return "Return payload.current as-is."

    if correction:
        p += (
            "\n\nCORRECTION PASS: Translate from the ORIGINAL payload.current again. "
            "Do not copy the previous bad output. Be extra strict about target language, "
            "complete meaning, actor/object, negation, relationship terms, numbers, and tone."
        )
    return p


def review_prompt(src: str, tgt: str) -> str:
    if (src, tgt) == ("ko", "th"):
        return REVIEW_RULES + "\n\n" + KO_TO_TH_RULES
    if (src, tgt) == ("th", "ko"):
        return REVIEW_RULES + "\n\n" + TH_TO_KO_RULES
    return REVIEW_RULES


def _build_payload(ctx: List[Dict[str, str]], current: str) -> str:
    return json.dumps(
        {
            "context": ctx,
            "current": current,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _build_review_payload(
    ctx: List[Dict[str, str]],
    current: str,
    draft: str,
    src: str,
    tgt: str,
) -> str:
    return json.dumps(
        {
            "source_language": src,
            "target_language": tgt,
            "context": ctx,
            "current": current,
            "draft": draft,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _responses_once(
    instructions: str,
    payload: str,
    model: str,
    timeout: float = OPENAI_TIMEOUT_SEC,
) -> str:
    """최신 SDK의 Responses API 사용. 추론을 끄고 번역만 빠르게 수행."""
    kwargs: Dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": payload,
        "max_output_tokens": 1200,
        "timeout": timeout,
    }

    # 번역은 긴 추론이 필요하지 않지만, low 정도를 주면 의미 보존이 더 안정적이다.
    if model.startswith(("gpt-5", "gpt-6")):
        kwargs["reasoning"] = {"effort": OPENAI_REASONING_EFFORT}

    try:
        resp = oai.responses.create(**kwargs)
    except TypeError:
        # 일부 구버전 SDK가 reasoning 인자를 모를 수 있으므로 한 번만 제거 후 호환 시도.
        kwargs.pop("reasoning", None)
        resp = oai.responses.create(**kwargs)
    return (getattr(resp, "output_text", "") or "").strip()


def _chat_compat_once(
    instructions: str,
    payload: str,
    model: str,
    timeout: float = OPENAI_TIMEOUT_SEC,
) -> str:
    """구버전 OpenAI SDK 호환용 fallback."""
    resp = oai.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": instructions},
            {"role": "user", "content": payload},
        ],
        timeout=timeout,
    )
    return (resp.choices[0].message.content or "").strip()


def _translate_once(
    instructions: str,
    payload: str,
    model: str,
    timeout: float = OPENAI_TIMEOUT_SEC,
) -> str:
    # 최신 SDK에서는 Responses API. 아주 오래된 SDK라 responses가 없으면 기존 Chat API로 동작.
    if hasattr(oai, "responses"):
        return _responses_once(instructions, payload, model, timeout)
    return _chat_compat_once(instructions, payload, OPENAI_COMPAT_MODEL, timeout)


def _has_wrong_script(tgt: str, out: str) -> bool:
    # 일반 문장 번역 결과에는 원문 언어 문자가 남아 있으면 안 된다.
    if tgt == "th" and RE_HANGUL.search(out):
        return True
    if tgt == "ko" and RE_THAI.search(out):
        return True
    return False


def _has_wrong_speaker_gender(src: str, tgt: str, out: str) -> bool:
    if (src, tgt) == ("ko", "th") and THAI_FEMALE_SPEAKER_RE.search(out):
        return True
    return False


def _looks_like_meta_answer(out: str) -> bool:
    s = out.strip().lower()
    prefixes = (
        "translation:",
        "translated text:",
        "thai translation:",
        "korean translation:",
        "번역:",
        "번역문:",
        "คำแปล:",
    )
    return any(s.startswith(p) for p in prefixes)


def _normalized_for_compare(text: str) -> str:
    return re.sub(r"\s+", "", text).strip().lower()


def _same_as_source(inp: str, out: str) -> bool:
    a = _normalized_for_compare(inp)
    b = _normalized_for_compare(out)
    return bool(a and a == b)


PROTECTED_TOKEN_RE = re.compile(
    r"https?://\S+|www\.\S+|@[A-Za-z0-9_.$-]+|"
    r"\d+(?:[.,:/-]\d+)*"
)


def _missing_protected_token(inp: str, out: str) -> bool:
    """숫자/시간/가격/URL/@mention이 번역 중 사라졌는지 검사."""
    tokens = PROTECTED_TOKEN_RE.findall(inp)
    return any(token not in out for token in tokens)


def _restore_missing_emojis(inp: str, out: str) -> str:
    """모델이 이모지를 빠뜨렸을 때 누락분만 끝에 복원."""
    in_emojis = EMOJI_REGEX.findall(inp)
    if not in_emojis:
        return out

    fixed = out
    for emoji in in_emojis:
        if emoji not in fixed:
            fixed += emoji
    return fixed


def _needs_retry(src: str, tgt: str, inp: str, out: str) -> bool:
    if not out.strip():
        return True
    if _same_as_source(inp, out):
        return True
    if _has_wrong_script(tgt, out):
        return True
    if _has_wrong_speaker_gender(src, tgt, out):
        return True
    if _looks_like_meta_answer(out):
        return True
    if _missing_protected_token(inp, out):
        return True

    li, lo = len(inp.strip()), len(out.strip())
    if li >= 20 and lo < 2:
        return True
    # 예전의 `lo > 250` 검사는 정상적인 긴 번역도 오류로 처리했다.
    # 입력 길이에 비해 비정상적으로 길어진 경우만 재시도한다.
