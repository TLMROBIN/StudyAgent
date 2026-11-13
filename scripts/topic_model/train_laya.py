"""Train and compare the original and fine-tuned native multilingual Laya checkpoints."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from topic_model_common import LABELS, LAYA_TOPIC_QUESTIONS, format_input
from train import INPUT_FORMAT_VERSION, report, resolve_labels, split


def state(sample: dict) -> str:
    return format_input(sample["subject"], sample["text"], sample.get("context"))


def write_records(path: Path, samples: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for sample in samples:
            record = {"state": state(sample), "questions": LAYA_TOPIC_QUESTIONS, "expected": {"topic": sample["label"]}}
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def evaluate(checkpoint: Path, samples: list[dict], args) -> list[float]:
    from laya import Agent

    agent = Agent(str(checkpoint), device=args.device)
    results = agent.predict_batch([state(sample) for sample in samples], LAYA_TOPIC_QUESTIONS, batch_size=args.batch_size)
    return [result["answers"]["topic"]["probabilities"]["academic"] for result in results]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--review-csv")
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--eval-ratio", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=20261113)
    parser.add_argument("--max-false-refusal", type=float, default=0.005)
    parser.add_argument("--max-false-pass", type=float, default=0.02)
    parser.add_argument("--accept", type=float, default=0.9)
    parser.add_argument("--reject", type=float, default=0.05)
    parser.add_argument("--freeze-encoder", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    if args.out.exists():
        parser.error("--out 已存在，请为每次训练指定新目录")
    samples = [json.loads(line) for line in args.data.read_text(encoding="utf-8").splitlines() if line.strip()]
    train_set, eval_set = split(resolve_labels(samples, args.review_csv), args.eval_ratio, args.seed)
    print(f"train={dict(Counter(s['label'] for s in train_set))} eval={dict(Counter(s['label'] for s in eval_set))}", flush=True)
    if {sample["label"] for sample in train_set} != set(LABELS) or not eval_set:
        parser.error("训练需要两类标注，且评估集不能为空")

    with tempfile.TemporaryDirectory() as temporary:
        records = Path(temporary) / "train.jsonl"
        write_records(records, train_set)
        cmd = [
            str(Path(sys.executable).with_name("laya-train")),
            "--data", str(records), "--base", str(args.base_model), "--out", str(args.out),
            "--device", args.device, "--epochs", str(args.epochs), "--micro-batch", str(args.batch_size),
            "--seed", str(args.seed),
        ]
        if args.freeze_encoder:
            cmd.append("--freeze-encoder")
        subprocess.run(cmd, check=True)

    base_probs = evaluate(args.base_model, eval_set, args)
    tuned_probs = evaluate(args.out, eval_set, args)
    metrics = {"laya_base": report(eval_set, base_probs, args), "laya_tuned": report(eval_set, tuned_probs, args)}
    meta = {
        "backend": "laya", "model_version": args.out.name, "base_model": str(args.base_model),
        "labels": list(LABELS), "input_format_version": INPUT_FORMAT_VERSION,
        "train_size": len(train_set), "eval_size": len(eval_set), "metrics": metrics,
    }
    (args.out / "topic_model_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
