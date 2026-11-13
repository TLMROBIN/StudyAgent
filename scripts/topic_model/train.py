"""微调 bge-m3 等标准 Transformers 二分类模型，并按三档策略评估。

原生 Laya 检查点使用 train_laya.py。bge-m3 的运行示例：
    python scripts/topic_model/train.py ... --base-model BAAI/bge-m3 --out models/topic-bgem3-v1 --device cuda:1
    python scripts/topic_model/train.py ... --base-model BAAI/bge-m3 --freeze-encoder --out models/topic-bgem3-head-v1

标签来源优先级：对抗用例金标 > 复核表 label 列（skip 剔除） > 无需复核的大模型标签；
需复核却未填写的样本不参与训练。tests/adversarial_cases.json 只进评估集，作为留出压力测试。
输出目录含模型、分词器与 topic_model_meta.json（指标、建议阈值、线上基线对比）。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

from topic_model_common import LABELS, binary_metrics, format_input, normalize, sweep_thresholds

INPUT_FORMAT_VERSION = "v1"


def resolve_labels(samples: list[dict], review_csv: str | None) -> list[dict]:
    overrides: dict[str, str] = {}
    if review_csv:
        with open(review_csv, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                value = (row.get("label") or "").strip().lower()
                if value:
                    overrides[row["sample_key"]] = value
    resolved = []
    stats: Counter = Counter()
    for sample in samples:
        if sample.get("label_source"):
            label = sample["label"]
            stats["gold"] += 1
        elif sample["sample_key"] in overrides:
            label = overrides[sample["sample_key"]]
            stats["reviewed"] += 1
        elif not sample.get("needs_review") and sample.get("llm_label"):
            label = sample["llm_label"]
            stats["llm"] += 1
        else:
            stats["unreviewed_dropped"] += 1
            continue
        if label == "skip":
            stats["skipped"] += 1
            continue
        if label not in LABELS:
            raise SystemExit(f"非法标签 {label!r}（sample_key={sample['sample_key']}），只能是 academic/off_topic/skip")
        resolved.append({**sample, "label": label})
    print(f"label_sources {dict(stats)}", file=sys.stderr)
    return resolved


def split(samples: list[dict], eval_ratio: float, seed: int) -> tuple[list[dict], list[dict]]:
    """同一正文跨上下文、学科与标签分到同一组，对抗题正文不进入训练。"""
    adversarial = [s for s in samples if s["source"] == "adversarial"]
    groups: dict[str, list[dict]] = defaultdict(list)
    adversarial_texts = {normalize(s.get("text") or s["sample_id"]) for s in adversarial}
    for sample in samples:
        if sample["source"] != "adversarial":
            key = normalize(sample.get("text") or sample["sample_id"])
            if key not in adversarial_texts:
                groups[key].append(sample)
    train, evaluation = [], list(adversarial)
    rng = random.Random(seed)
    strata: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for key, rows in sorted(groups.items()):
        strata[tuple(sorted({row["label"] for row in rows}))].append(key)
    for keys in strata.values():
        rng.shuffle(keys)
        cut = max(1, round(len(keys) * eval_ratio)) if len(keys) > 1 else 0
        for index, key in enumerate(keys):
            (evaluation if index < cut else train).extend(groups[key])
    return train, evaluation


def _encode(tokenizer, batch: list[dict], max_length: int):
    texts = [format_input(s["subject"], s["text"], s.get("context")) for s in batch]
    return tokenizer(texts, truncation=True, max_length=max_length, padding=True, return_tensors="pt")


def predict(model, tokenizer, samples: list[dict], device: str, batch_size: int, max_length: int) -> list[float]:
    import torch

    model.eval()
    probs: list[float] = []
    with torch.no_grad():
        for start in range(0, len(samples), batch_size):
            inputs = _encode(tokenizer, samples[start : start + batch_size], max_length).to(device)
            logits = model(**inputs).logits.float()
            probs.extend(torch.softmax(logits, dim=-1)[:, LABELS.index("academic")].tolist())
    return probs


def train(args, train_set: list[dict]):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, get_linear_schedule_with_warmup

    torch.manual_seed(args.seed)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=args.trust_remote_code)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.base_model,
        num_labels=len(LABELS),
        id2label=dict(enumerate(LABELS)),
        label2id={label: i for i, label in enumerate(LABELS)},
        trust_remote_code=args.trust_remote_code,
    ).to(args.device)
    if args.freeze_encoder:
        head_names = ("classifier", "score", "pre_classifier", "head")
        for name, param in model.named_parameters():
            param.requires_grad = any(part in name for part in head_names)

    counts = Counter(s["label"] for s in train_set)
    # 类别不均衡（越界样本少）时按频率反比加权，避免模型一律判 academic
    weights = torch.tensor([len(train_set) / (len(LABELS) * max(counts[label], 1)) for label in LABELS], device=args.device)
    loss_fn = torch.nn.CrossEntropyLoss(weight=weights)
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.01)
    steps = args.epochs * ((len(train_set) + args.batch_size - 1) // args.batch_size)
    scheduler = get_linear_schedule_with_warmup(optimizer, int(steps * 0.1), steps)
    use_bf16 = args.device.startswith("cuda") and torch.cuda.is_bf16_supported()

    rng = random.Random(args.seed)
    for epoch in range(args.epochs):
        model.train()
        order = list(train_set)
        rng.shuffle(order)
        total_loss = 0.0
        for start in range(0, len(order), args.batch_size):
            batch = order[start : start + args.batch_size]
            inputs = _encode(tokenizer, batch, args.max_length).to(args.device)
            targets = torch.tensor([LABELS.index(s["label"]) for s in batch], device=args.device)
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=use_bf16):
                logits = model(**inputs).logits
            loss = loss_fn(logits.float(), targets)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()
            total_loss += loss.item() * len(batch)
        print(f"epoch={epoch + 1} loss={total_loss / len(order):.4f}", file=sys.stderr)
    return model, tokenizer


def report(samples: list[dict], probs: list[float], args) -> dict:
    labels = [int(s["label"] == "academic") for s in samples]
    # 线上基线：当前规则 + LLM 分类的实际放行/拦截结果，按确定判定处理
    baseline_probs = [0.0 if s["current_decision"] == "blocked" else 1.0 for s in samples]
    prod_idx = [i for i, s in enumerate(samples) if s["source"] == "prod"]
    adv_idx = [i for i, s in enumerate(samples) if s["source"] == "adversarial"]
    synthetic_idx = [i for i, s in enumerate(samples) if s["source"] == "synthetic"]
    pick = lambda values, idx: [values[i] for i in idx]  # noqa: E731
    prod_labels = pick(labels, prod_idx)
    suggestion = None
    if set(prod_labels) == {0, 1}:
        suggestion = sweep_thresholds(
            prod_labels, pick(probs, prod_idx),
            max_false_refusal=args.max_false_refusal, max_false_pass=args.max_false_pass,
        )
    accept, reject = (suggestion["accept"], suggestion["reject"]) if suggestion else (args.accept, args.reject)
    return {
        "thresholds_suggested": suggestion,
        "thresholds_used_for_breakdown": {"accept": accept, "reject": reject},
        "model_all": binary_metrics(labels, probs, accept, reject),
        "model_prod": binary_metrics(pick(labels, prod_idx), pick(probs, prod_idx), accept, reject),
        "model_prod_at_configured_thresholds": binary_metrics(prod_labels, pick(probs, prod_idx), args.accept, args.reject),
        "model_adversarial": binary_metrics(pick(labels, adv_idx), pick(probs, adv_idx), accept, reject),
        "model_synthetic": binary_metrics(pick(labels, synthetic_idx), pick(probs, synthetic_idx), accept, reject),
        "baseline_prod_current_decision": binary_metrics(pick(labels, prod_idx), pick(baseline_probs, prod_idx), 0.5, 0.5),
        "label_counts_eval": dict(Counter(s["label"] for s in samples)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", required=True, help="prelabel.py 输出的 JSONL")
    parser.add_argument("--review-csv")
    parser.add_argument("--base-model", required=True, help="标准 Transformers 模型目录或 HF 模型名（如 BAAI/bge-m3）")
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="cuda:1")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--eval-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20261113)
    parser.add_argument("--freeze-encoder", action="store_true", help="只训练分类头（bge-m3 + 分类头方案）")
    parser.add_argument("--trust-remote-code", action="store_true")
    parser.add_argument("--max-false-refusal", type=float, default=0.005)
    parser.add_argument("--max-false-pass", type=float, default=0.02)
    parser.add_argument("--accept", type=float, default=0.9, help="找不到满足上限的阈值时用于分项统计")
    parser.add_argument("--reject", type=float, default=0.05)
    args = parser.parse_args(argv)
    if args.freeze_encoder and args.lr == 2e-5:
        args.lr = 1e-3

    samples = [json.loads(line) for line in Path(args.data).read_text(encoding="utf-8").splitlines() if line.strip()]
    train_set, eval_set = split(resolve_labels(samples, args.review_csv), args.eval_ratio, args.seed)
    print(f"train={len(train_set)} eval={len(eval_set)} train_labels={dict(Counter(s['label'] for s in train_set))}", file=sys.stderr)
    if {s["label"] for s in train_set} != set(LABELS) or not eval_set:
        raise SystemExit("训练需要两类标注，且评估集不能为空")

    model, tokenizer = train(args, train_set)
    probs = predict(model, tokenizer, eval_set, args.device, args.batch_size, args.max_length)
    metrics = report(eval_set, probs, args)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(out)
    tokenizer.save_pretrained(out)
    meta = {
        "model_version": out.name,
        "base_model": args.base_model,
        "freeze_encoder": args.freeze_encoder,
        "input_format_version": INPUT_FORMAT_VERSION,
        "labels": list(LABELS),
        "max_length": args.max_length,
        "train_size": len(train_set),
        "eval_size": len(eval_set),
        "metrics": metrics,
    }
    (out / "topic_model_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    errors = [
        {"sample_key": s["sample_key"], "label": s["label"], "p_academic": round(p, 4), "subject": s["subject"], "text": s["text"][:200]}
        for s, p in zip(eval_set, probs)
        if (p >= 0.5) != (s["label"] == "academic")
    ]
    (out / "eval_errors.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in errors), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
