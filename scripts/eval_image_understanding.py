#!/usr/bin/env python3
"""Evaluate historical chat image understanding failures.

Usage example:
  .venv/bin/python scripts/eval_image_understanding.py \
    --db data/studyagent.db \
    --attachments-dir data/chat_attachments \
    --status failed \
    --limit 200 \
    --since 2026-06-01 \
    --out eval_results.csv
"""

from __future__ import annotations

import argparse
import asyncio
from base64 import b64encode
import csv
import math
import mimetypes
from pathlib import Path
import sqlite3
import sys
from time import perf_counter
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.chat_image_understanding_service import chat_image_understanding_service  # noqa: E402


DETAIL_FIELDS = [
    "attachment_id",
    "storage_key",
    "ocr_status",
    "subject",
    "source",
    "confidence_level",
    "must_short_circuit",
    "prompt_summary_preview",
    "elapsed_seconds",
]
DEFAULT_MATRIX_MODELS = [
    "Qwen/Qwen3-VL-32B-Instruct",
    "Qwen/Qwen3-VL-8B-Thinking",
    "gemini-2.5-flash",
    "doubao-1.5-vision-pro-250328",
    "THUDM/GLM-4.1V-9B-Thinking",
    "deepseek-ai/DeepSeek-OCR",
]
DEFAULT_MATRIX_MODELS_TEXT = ",".join(DEFAULT_MATRIX_MODELS)
VLM_EXTRACTION_PROMPT = (
    "你正在评测高中题目图片识别质量。请只做题目结构化提取，不解答，不给最终答案，不编造看不清的内容。\n"
    "请按 Markdown 输出：\n"
    "1. 题干：完整抄录能看清的题干文字。\n"
    "2. 已知条件：列出数字、单位、公式、变量关系，公式使用 LaTeX。\n"
    "3. 选项：如有选择题选项，逐项抄录。\n"
    "4. 图形结构：描述图像、电路、实验装置、坐标系、几何关系或标注。\n"
    "5. 看不清内容：只说明确实模糊或遮挡的位置。\n"
)
MODEL_PROMPT_TEMPLATES = {
    "deepseek-ai/DeepSeek-OCR": "Free OCR.",
}
MATRIX_FIELDS = [
    "attachment_id",
    "storage_key",
    "subject",
    "model",
    "success",
    "output_length",
    "elapsed_seconds",
    "prompt_tokens",
    "completion_tokens",
    "output_preview",
]
SCORE_FIELDS = [
    *MATRIX_FIELDS,
    "stem_completeness_score",
    "formula_correctness_score",
    "diagram_description_score",
    "review_notes",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline evaluation for StudyAgent chat image understanding.")
    parser.add_argument("--db", default="data/studyagent.db", help="SQLite database path.")
    parser.add_argument("--attachments-dir", default="data/chat_attachments", help="Chat attachment root directory.")
    parser.add_argument("--status", default="failed", help="ocr_status to evaluate.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum attachment rows to evaluate.")
    parser.add_argument("--since", default="2026-06-01", help="Only evaluate rows created at or after this date.")
    parser.add_argument("--out", default="eval_results.csv", help="CSV detail output path.")
    parser.add_argument("--matrix", action="store_true", help="Compare candidate vision/OCR models directly.")
    parser.add_argument("--provider", default="302ai", help="Provider account name for --matrix mode.")
    parser.add_argument("--models", default=DEFAULT_MATRIX_MODELS_TEXT, help="Comma-separated model names for --matrix.")
    parser.add_argument(
        "--score-template",
        default="",
        help="Optional CSV path with blank manual scoring columns for --matrix mode.",
    )
    return parser.parse_args(argv)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def fetch_attachment_rows(
    connection: sqlite3.Connection,
    *,
    status: str,
    since: str | None,
    limit: int,
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            a.id AS attachment_id,
            a.storage_key,
            a.original_filename,
            a.mime_type,
            a.ocr_status,
            m.content AS user_text,
            m.llm_model_key,
            c.subject
        FROM chat_message_attachments AS a
        JOIN messages AS m ON m.id = a.message_id
        JOIN conversations AS c ON c.id = m.conversation_id
        WHERE a.ocr_status = ?
          AND (? IS NULL OR a.created_at >= ?)
        ORDER BY a.created_at DESC, a.id DESC
        LIMIT ?
        """,
        (status, since, since, max(0, limit)),
    ).fetchall()


def load_provider_account(connection: sqlite3.Connection, *, provider_name: str) -> dict[str, str]:
    row = connection.execute(
        """
        SELECT provider_name, base_url, api_key
        FROM llm_provider_accounts
        WHERE provider_name = ?
          AND is_enabled = 1
        ORDER BY id ASC
        LIMIT 1
        """,
        (provider_name,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"Provider account not found or disabled: {provider_name}")
    return {
        "provider_name": row["provider_name"],
        "base_url": str(row["base_url"] or "").rstrip("/"),
        "api_key": str(row["api_key"] or ""),
    }


def resolve_attachment_path(base_dir: Path, storage_key: str) -> Path:
    base = base_dir.resolve()
    candidate = (base / storage_key).resolve()
    if candidate == base or base in candidate.parents:
        return candidate
    raise ValueError(f"attachment path escapes base directory: {storage_key}")


def percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return ordered[index]


def format_seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def parse_model_list(raw_models: str) -> list[str]:
    return [item.strip() for item in (raw_models or "").split(",") if item.strip()]


def prompt_for_model(model: str) -> str:
    return MODEL_PROMPT_TEMPLATES.get(model, VLM_EXTRACTION_PROMPT)


def sanitize_text(text: object, *, secrets: list[str]) -> str:
    sanitized = str(text or "")
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized


def build_matrix_payload(*, model: str, prompt: str, image_bytes: bytes, mime_type: str) -> dict[str, Any]:
    data_url = f"data:{mime_type};base64,{b64encode(image_bytes).decode('ascii')}"
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "temperature": 0.1,
        "stream": False,
    }


def chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


async def post_json(*, url: str, api_key: str, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()


def content_text(content: object) -> str:
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return str(content or "")


def parse_completion(body: dict[str, Any]) -> tuple[str, int | None, int | None]:
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message") if isinstance(choice, dict) else {}
    text = content_text(message.get("content") if isinstance(message, dict) else "")
    usage = body.get("usage") if isinstance(body, dict) else {}
    prompt_tokens = None
    completion_tokens = None
    if isinstance(usage, dict):
        if usage.get("prompt_tokens") is not None:
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
        if usage.get("completion_tokens") is not None:
            completion_tokens = int(usage.get("completion_tokens") or 0)
    return text, prompt_tokens, completion_tokens


async def evaluate_row(row: sqlite3.Row, *, image_path: Path) -> dict[str, Any]:
    image_bytes = image_path.read_bytes()
    mime_type = row["mime_type"] or mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    started_at = perf_counter()
    try:
        result = await chat_image_understanding_service.understand(
            image_bytes=image_bytes,
            mime_type=mime_type,
            subject=row["subject"] or "",
            user_text=row["user_text"] or "",
            model_key=row["llm_model_key"],
            image_path=str(image_path),
            attachment_id=row["attachment_id"],
        )
        elapsed = perf_counter() - started_at
        return {
            "attachment_id": row["attachment_id"],
            "storage_key": row["storage_key"],
            "ocr_status": row["ocr_status"],
            "subject": row["subject"] or "",
            "source": result.source,
            "confidence_level": result.confidence_level,
            "must_short_circuit": result.must_short_circuit,
            "prompt_summary_preview": " ".join((result.prompt_summary or "").split())[:120],
            "elapsed_seconds": f"{elapsed:.3f}",
        }
    except Exception as exc:
        elapsed = perf_counter() - started_at
        return {
            "attachment_id": row["attachment_id"],
            "storage_key": row["storage_key"],
            "ocr_status": row["ocr_status"],
            "subject": row["subject"] or "",
            "source": "failed",
            "confidence_level": "low",
            "must_short_circuit": True,
            "prompt_summary_preview": f"ERROR {type(exc).__name__}: {str(exc)[:100]}",
            "elapsed_seconds": f"{elapsed:.3f}",
        }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DETAIL_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_matrix_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MATRIX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_score_template(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SCORE_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "stem_completeness_score": "",
                    "formula_correctness_score": "",
                    "diagram_description_score": "",
                    "review_notes": "",
                }
            )


def print_summary(*, total_rows: int, missing_files: int, detail_rows: list[dict[str, Any]], out_path: Path) -> None:
    evaluated = len(detail_rows)
    short_circuits = sum(1 for row in detail_rows if str(row["must_short_circuit"]) == "True")
    latencies = [float(row["elapsed_seconds"]) for row in detail_rows]
    source_counts: dict[str, int] = {}
    for row in detail_rows:
        source = str(row["source"] or "unknown")
        source_counts[source] = source_counts.get(source, 0) + 1

    print("Image understanding evaluation")
    print(f"Matched rows: {total_rows}")
    print(f"Evaluated images: {evaluated}")
    print(f"Missing files: {missing_files}")
    rate = (short_circuits / evaluated * 100) if evaluated else 0.0
    print(f"Short-circuit failure rate: {rate:.1f}%")
    print("Source counts:")
    if source_counts:
        for source, count in sorted(source_counts.items()):
            pct = count / evaluated * 100 if evaluated else 0.0
            print(f"  {source}: {count} ({pct:.1f}%)")
    else:
        print("  none: 0 (0.0%)")
    print(
        "Latency seconds: "
        f"p50={format_seconds(percentile(latencies, 0.50))} "
        f"p95={format_seconds(percentile(latencies, 0.95))}"
    )
    print(f"CSV written: {out_path}")


async def evaluate_matrix_cell(
    row: sqlite3.Row,
    *,
    image_path: Path,
    model: str,
    provider: dict[str, str],
) -> dict[str, Any]:
    image_bytes = image_path.read_bytes()
    mime_type = row["mime_type"] or mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    payload = build_matrix_payload(
        model=model,
        prompt=prompt_for_model(model),
        image_bytes=image_bytes,
        mime_type=mime_type,
    )
    started_at = perf_counter()
    try:
        body = await post_json(
            url=chat_completions_url(provider["base_url"]),
            api_key=provider["api_key"],
            payload=payload,
            timeout_seconds=90,
        )
        elapsed = perf_counter() - started_at
        text, prompt_tokens, completion_tokens = parse_completion(body)
        return {
            "attachment_id": row["attachment_id"],
            "storage_key": row["storage_key"],
            "subject": row["subject"] or "",
            "model": model,
            "success": bool(text.strip()),
            "output_length": len(text),
            "elapsed_seconds": f"{elapsed:.3f}",
            "prompt_tokens": "" if prompt_tokens is None else prompt_tokens,
            "completion_tokens": "" if completion_tokens is None else completion_tokens,
            "output_preview": sanitize_text(" ".join(text.split())[:200], secrets=[provider["api_key"]]),
        }
    except Exception as exc:
        elapsed = perf_counter() - started_at
        return {
            "attachment_id": row["attachment_id"],
            "storage_key": row["storage_key"],
            "subject": row["subject"] or "",
            "model": model,
            "success": False,
            "output_length": 0,
            "elapsed_seconds": f"{elapsed:.3f}",
            "prompt_tokens": "",
            "completion_tokens": "",
            "output_preview": sanitize_text(f"ERROR {type(exc).__name__}: {exc}", secrets=[provider["api_key"]])[:200],
        }


def print_matrix_summary(
    *,
    total_rows: int,
    missing_files: int,
    matrix_rows: list[dict[str, Any]],
    models: list[str],
    out_path: Path,
    score_path: Path | None,
) -> None:
    print("Image understanding matrix evaluation")
    print(f"Matched images: {total_rows}")
    print(f"Missing files: {missing_files}")
    print(f"Matrix evaluated rows: {len(matrix_rows)}")
    print("Model summary:")
    for model in models:
        model_rows = [row for row in matrix_rows if row["model"] == model]
        latencies = [float(row["elapsed_seconds"]) for row in model_rows]
        successes = sum(1 for row in model_rows if str(row["success"]) == "True")
        rate = successes / len(model_rows) * 100 if model_rows else 0.0
        print(
            f"  {model}: success_rate={rate:.1f}% "
            f"p50={format_seconds(percentile(latencies, 0.50))} "
            f"p95={format_seconds(percentile(latencies, 0.95))}"
        )
    print(f"CSV written: {out_path}")
    if score_path:
        print(f"Score template written: {score_path}")


def run_matrix(
    *,
    rows: list[sqlite3.Row],
    attachments_dir: Path,
    provider: dict[str, str],
    models: list[str],
    out_path: Path,
    score_path: Path | None,
) -> int:
    matrix_rows: list[dict[str, Any]] = []
    missing_files = 0
    total_cells = len(rows) * len(models)
    completed_cells = 0
    for row in rows:
        try:
            image_path = resolve_attachment_path(attachments_dir, row["storage_key"])
        except ValueError:
            missing_files += 1
            continue
        if not image_path.exists():
            missing_files += 1
            continue
        for model in models:
            completed_cells += 1
            print(f"[{completed_cells}/{total_cells}] attachment={row['attachment_id']} model={model}", flush=True)
            matrix_rows.append(
                asyncio.run(
                    evaluate_matrix_cell(
                        row,
                        image_path=image_path,
                        model=model,
                        provider=provider,
                    )
                )
            )

    write_matrix_csv(out_path, matrix_rows)
    if score_path:
        write_score_template(score_path, matrix_rows)
    print_matrix_summary(
        total_rows=len(rows),
        missing_files=missing_files,
        matrix_rows=matrix_rows,
        models=models,
        out_path=out_path,
        score_path=score_path,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.db)
    attachments_dir = Path(args.attachments_dir)
    out_path = Path(args.out)

    with connect_readonly(db_path) as connection:
        rows = fetch_attachment_rows(
            connection,
            status=args.status,
            since=args.since or None,
            limit=args.limit,
        )
        provider = load_provider_account(connection, provider_name=args.provider) if args.matrix and rows else None

    if args.matrix:
        models = parse_model_list(args.models)
        if not models:
            raise SystemExit("At least one model is required for --matrix mode.")
        return run_matrix(
            rows=rows,
            attachments_dir=attachments_dir,
            provider=provider or {},
            models=models,
            out_path=out_path,
            score_path=Path(args.score_template) if args.score_template else None,
        )

    detail_rows: list[dict[str, Any]] = []
    missing_files = 0
    for row in rows:
        try:
            image_path = resolve_attachment_path(attachments_dir, row["storage_key"])
        except ValueError:
            missing_files += 1
            continue
        if not image_path.exists():
            missing_files += 1
            continue
        detail_rows.append(asyncio.run(evaluate_row(row, image_path=image_path)))

    write_csv(out_path, detail_rows)
    print_summary(total_rows=len(rows), missing_files=missing_files, detail_rows=detail_rows, out_path=out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
