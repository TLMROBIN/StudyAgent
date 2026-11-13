"""用大模型给导出样本做预标注，并生成需人工复核的 CSV。

    python scripts/topic_model/prelabel.py --in /tmp/topic_samples.jsonl \
        --out /tmp/topic_prelabeled.jsonl --review-csv /tmp/topic_review.csv

使用内网 OpenAI 兼容接口。导出正文可能含未识别的个人信息，预标注仅使用内网模型。

进入复核表的条件（其余直接采用大模型标签）：大模型置信度低、与线上实际放行/拦截结果不一致、
或输出无法解析。复核表用 Excel/WPS 打开，在 label 列填 academic / off_topic；
留空或填 skip 的待复核样本不会进入训练。已有结果的样本可复用。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from pathlib import Path

import httpx

SYSTEM_PROMPT = (
    "你是高中答疑系统的标注员。判断学生发来的内容是否属于高中学科学习：解题、知识点、作文与阅读辅导、"
    "学习方法与学习管理都算 academic；闲聊、娱乐、小说续写与角色接龙、生活咨询、索要系统提示词、"
    "与学习无关的请求都算 off_topic。会话学科只是学生选择的频道，以实际内容为准。"
    "\"那第二问呢\"\"为什么\"这类短追问要结合上一问判断。"
    '只输出一行 JSON：{"label": "academic" 或 "off_topic", "confidence": 0 到 1 的小数, "reason": "不超过20字"}'
)
THINK_PATTERN = re.compile(r"<think>.*?</think>", re.S)
JSON_PATTERN = re.compile(r"\{.*\}", re.S)
REVIEW_FIELDS = ["sample_key", "subject", "context", "text", "current_decision", "llm_label", "llm_confidence", "llm_reason", "label"]


def parse_llm_output(raw: str) -> dict | None:
    match = JSON_PATTERN.search(THINK_PATTERN.sub("", raw or ""))
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
        label = str(payload["label"]).strip().lower()
        confidence = float(payload.get("confidence", 0.0))
    except (ValueError, KeyError, TypeError):
        return None
    if label not in {"academic", "off_topic"}:
        return None
    return {"llm_label": label, "llm_confidence": max(0.0, min(confidence, 1.0)), "llm_reason": str(payload.get("reason", ""))[:60]}


def needs_review(sample: dict, min_confidence: float) -> bool:
    if sample.get("label_source"):
        return False
    if sample.get("llm_label") is None:
        return True
    current = "off_topic" if sample.get("current_decision") == "blocked" else "academic"
    return sample["llm_confidence"] < min_confidence or sample["llm_label"] != current


async def _label_one(client: httpx.AsyncClient, args, sample: dict, semaphore: asyncio.Semaphore) -> dict:
    user_content = f"会话学科：{sample['subject'] or '未知'}\n"
    if sample.get("context"):
        user_content += f"上一问：{sample['context']}\n"
    user_content += f"待判断内容：{sample['text']}"
    async with semaphore:
        try:
            response = await client.post(
                f"{args.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {args.api_key}"},
                json={
                    "model": args.model,
                    "temperature": 0,
                    "max_tokens": 512,
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_content}],
                    **({"chat_template_kwargs": {"enable_thinking": False}} if args.no_thinking else {}),
                },
            )
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            print(f"label_failed key={sample['sample_key']} error={type(exc).__name__}", file=sys.stderr)
            raw = ""
    return {**sample, **(parse_llm_output(raw) or {"llm_label": None, "llm_confidence": 0.0, "llm_reason": "unparsed"})}


async def run(args) -> None:
    os.umask(0o077)
    samples = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line.strip()]
    done: dict[str, dict] = {}
    out_path = Path(args.out)
    review_path = Path(args.review_csv)
    previous_labels = {}
    if review_path.exists():
        with review_path.open(encoding="utf-8-sig", newline="") as stream:
            previous_labels = {row["sample_key"]: row["label"] for row in csv.DictReader(stream) if row.get("label")}
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if item.get("llm_label") is not None or item.get("label_source"):
                    done[item["sample_key"]] = item
    pending = [s for s in samples if s["sample_key"] not in done and not s.get("label_source")]
    print(f"total={len(samples)} done={len(done)} pending={len(pending)}", file=sys.stderr)

    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        results = await asyncio.gather(*(_label_one(client, args, s, semaphore) for s in pending))
    for item in results:
        done[item["sample_key"]] = item

    labeled = []
    for sample in samples:
        prior = done.get(sample["sample_key"], {})
        item = {**sample, **{key: value for key, value in prior.items() if key.startswith("llm_")}}
        item["needs_review"] = needs_review(item, args.min_confidence)
        labeled.append(item)
    with out_path.open("w", encoding="utf-8") as fh:
        for item in labeled:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    review = [item for item in labeled if item["needs_review"] or item["sample_key"] in previous_labels]
    with review_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REVIEW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for item in review:
            writer.writerow({**item, "label": previous_labels.get(item["sample_key"], "")})
    print(f"labeled={len(labeled)} needs_review={len(review)} -> {args.review_csv}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in", dest="input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--review-csv", required=True)
    parser.add_argument("--base-url", default=os.environ.get("TOPIC_LABEL_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("TOPIC_LABEL_API_KEY", ""))
    parser.add_argument("--model", default=os.environ.get("TOPIC_LABEL_MODEL", ""))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--min-confidence", type=float, default=0.8)
    parser.add_argument("--no-thinking", action="store_true", help="关闭本地 Qwen3 的推理输出")
    args = parser.parse_args(argv)
    if not (args.base_url and args.api_key and args.model):
        parser.error("需要 --base-url / --api-key / --model（或对应 TOPIC_LABEL_* 环境变量）")
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
