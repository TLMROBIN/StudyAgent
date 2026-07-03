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
import csv
import math
import mimetypes
from pathlib import Path
import sqlite3
import sys
from time import perf_counter
from typing import Any

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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline evaluation for StudyAgent chat image understanding.")
    parser.add_argument("--db", default="data/studyagent.db", help="SQLite database path.")
    parser.add_argument("--attachments-dir", default="data/chat_attachments", help="Chat attachment root directory.")
    parser.add_argument("--status", default="failed", help="ocr_status to evaluate.")
    parser.add_argument("--limit", type=int, default=200, help="Maximum attachment rows to evaluate.")
    parser.add_argument("--since", default="2026-06-01", help="Only evaluate rows created at or after this date.")
    parser.add_argument("--out", default="eval_results.csv", help="CSV detail output path.")
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
