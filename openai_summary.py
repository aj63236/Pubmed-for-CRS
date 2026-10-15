"""OpenAI Responses contract shared by synchronous and discounted Batch requests.

No SDK dependency. Clinical prompt and deterministic ARR/NNT calculation remain in
fetch_papers.py; this module constrains the transport's JSON output shape.
"""


def obj(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1}
TEXTS = {"type": "array", "items": TEXT}
ARM = obj({"label": TEXT, "events": {"type": ["number", "null"]},
           "total": {"type": ["number", "null"]},
           "rate": {"type": ["number", "null"]}})
SUMMARY_SCHEMA = obj({
    "title_zh": TEXT, "abstract_zh": TEXT,
    "evidence_level": {"type": "string", "enum": ["RCT", "系統性回顧", "統合分析",
        "前瞻性世代", "回溯性研究", "病例對照", "橫斷面", "診斷性研究", "病例系列", "綜述", "其他"]},
    "score": {"type": "integer", "minimum": 0, "maximum": 10},
    "score_reason": TEXT,
    "relevance": {"type": "string", "enum": ["高度相關", "中度相關", "低度相關"]},
    "relevance_why": TEXT,
    "novelty": {"type": "string", "enum": ["可能改變實務", "再確認已知", "仍屬早期", "結果為陰性"]},
    "one_liner": TEXT, "action": TEXT,
    "pico": obj({k: TEXT for k in ("P", "I", "C", "O")}),
    "effect_data": {"type": "array", "maxItems": 3, "items": obj({
        "outcome": TEXT, "event_is_bad": {"type": "boolean"},
        "intervention": ARM, "control": ARM})},
    "key_numbers": TEXTS,
    "cautions": {"type": "array", "items": TEXT, "minItems": 3, "maxItems": 3},
    "unassessable": TEXTS,
    "context_verdict": {"type": "string", "enum": ["再確認", "與前作矛盾", "延伸前作", "無相關前作"]},
    "context": TEXT,
})


def response_request(prompt, model, effort="medium", max_output_tokens=12000):
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("OPENAI_REASONING_EFFORT must be low, medium, high, xhigh, or max")
    if not 1 <= max_output_tokens <= 128000:
        raise ValueError("OPENAI_MAX_OUTPUT_TOKENS must be between 1 and 128000")
    return {
        "model": model,
        "input": [{"role": "user", "content": prompt}],
        "reasoning": {"effort": effort},
        "max_output_tokens": max_output_tokens,  # includes reasoning + visible output
        "store": False,
        "text": {"format": {"type": "json_schema", "name": "clinical_summary",
                            "strict": True, "schema": SUMMARY_SCHEMA}},
    }


def response_text(body):
    """Read only completed assistant output, never reasoning or a refusal."""
    if body.get("status") != "completed" or body.get("error") or body.get("incomplete_details"):
        raise ValueError("OpenAI response not completed: " + str(
            body.get("incomplete_details") or body.get("error") or body.get("status")))
    parts = []
    for item in body.get("output") or []:
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        if item.get("status") not in (None, "completed"):
            raise ValueError("OpenAI assistant message is incomplete")
        for block in item.get("content") or []:
            if block.get("type") == "refusal":
                raise ValueError("OpenAI refused this summary")
            if block.get("type") == "output_text":
                parts.append(block.get("text", ""))
    text = "".join(parts).strip()
    if not text:
        raise ValueError("OpenAI response contains no assistant output_text")
    return text
