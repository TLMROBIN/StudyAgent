"""从生产库只读导出学生提问，生成话题决策模型的待标注样本（JSONL）。

在 63 上的 backend 容器内运行（能读到 DATABASE_URL / SQLITE_PATH）：
    python scripts/topic_model/export_samples.py --out /tmp/topic_samples.jsonl

只读保证：
- SQLite：先用 sqlite3 backup API 拷贝到临时文件再读（库是 WAL 模式，且 62 夜间同步会整库回写，
  直接打开原库读有锁与不一致风险）；
- 其他数据库：会话设为 READ ONLY 事务。
脱敏：不导出 student_id / conversation_id / 用户名；正文内手机号、证件号、长数字串等替换为占位符。
正文里的人名无法可靠识别，导出文件只能留在局域网内，标注后删除。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from sqlalchemy import create_engine, text

from topic_model_common import mask_pii, normalize, sample_key, text_hash

IMAGE_ONLY_PLACEHOLDER = "[图片提问]"
QUERY = text(
    """
    SELECT m.conversation_id AS conversation_id, m.turn_index AS turn_index, m.id AS id,
           m.content AS content, m.delivery_status AS delivery_status, c.subject AS subject,
           EXISTS (SELECT 1 FROM chat_message_attachments a WHERE a.message_id = m.id) AS has_image
    FROM messages m JOIN conversations c ON c.id = m.conversation_id
    WHERE m.role IN ('USER', 'user') AND m.delivery_status IN ('completed', 'blocked')
    ORDER BY m.conversation_id, m.turn_index, m.id
    """
)


def _default_database_url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    sqlite_path = os.environ.get("SQLITE_PATH", "data/studyagent.db")
    return sqlite_path if sqlite_path.startswith("sqlite") else f"sqlite:///{sqlite_path}"


def _snapshot_sqlite(url: str, workdir: str) -> str:
    source = url.split("sqlite:///", 1)[1]
    if not Path(source).exists():
        raise SystemExit(f"SQLite 文件不存在：{source}")
    target = os.path.join(workdir, "snapshot.db")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return f"sqlite:///{target}"


def fetch_rows(url: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as workdir:
        if url.startswith("sqlite"):
            url = _snapshot_sqlite(url, workdir)
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                if engine.dialect.name == "postgresql":
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                return [dict(row._mapping) for row in conn.execute(QUERY)]
        finally:
            engine.dispose()


def build_samples(rows: list[dict], candidate_check=None) -> list[dict]:
    """把同一会话里上一条已送达的学生提问作为上下文（短追问依赖它判断）。"""
    by_conversation: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        by_conversation[row["conversation_id"]].append(row)
    samples: dict[tuple[str, str, str], dict] = {}
    for turns in by_conversation.values():
        previous_question = ""
        has_academic_history = False
        for row in turns:
            content = (row["content"] or "").strip()
            if not content or content == IMAGE_ONLY_PLACEHOLDER:
                continue
            subject = row["subject"] or ""
            if candidate_check is not None:
                candidate, academic = candidate_check(content, subject, has_academic_history)
                if row["has_image"] or not candidate:
                    if row["delivery_status"] == "completed":
                        previous_question = content
                        has_academic_history |= academic
                    continue
                if row["delivery_status"] == "completed":
                    has_academic_history |= academic
            masked = mask_pii(content)
            context = mask_pii(previous_question)
            key = (subject, normalize(masked), normalize(context))
            sample = samples.get(key)
            if sample is None:
                samples[key] = {
                    "sample_key": sample_key(subject, masked, context),
                    "sample_id": text_hash(subject, content),
                    "source": "prod",
                    "subject": subject,
                    "text": masked,
                    "context": context,
                    "current_decision": row["delivery_status"],
                    "occurrences": 1,
                }
            else:
                sample["occurrences"] += 1
                if row["delivery_status"] == "blocked":
                    sample["current_decision"] = "blocked"
            if row["delivery_status"] == "completed":
                previous_question = content
    return list(samples.values())


def adversarial_samples(path: Path) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    return [
        {
            "sample_key": sample_key(case.get("subject", ""), case["text"], ""),
            "sample_id": text_hash("", case["text"]),
            "source": "adversarial",
            "subject": case.get("subject", ""),
            "text": case["text"],
            "context": "",
            "current_decision": "completed" if case["allowed"] else "blocked",
            "occurrences": 1,
            "label": "academic" if case["allowed"] else "off_topic",
            "label_source": "adversarial_cases",
        }
        for case in cases
    ]


def stratified_limit(samples: list[dict], limit: int, seed: int) -> list[dict]:
    """被拦截的样本稀少且最有信息量，全部保留；剩余名额随机抽取正常放行的样本。"""
    if limit <= 0 or len(samples) <= limit:
        return samples
    blocked = [s for s in samples if s["current_decision"] == "blocked"]
    completed = [s for s in samples if s["current_decision"] != "blocked"]
    random.Random(seed).shuffle(completed)
    return blocked + completed[: max(limit - len(blocked), 0)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database-url", default=_default_database_url())
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=2000, help="生产样本上限；0 表示不限")
    parser.add_argument("--seed", type=int, default=20261113)
    parser.add_argument("--adversarial", default="tests/adversarial_cases.json", help="空字符串表示不合并")
    parser.add_argument("--candidates-only", action="store_true", help="仅导出当前规则会送入话题分类器的纯文本提问")
    args = parser.parse_args(argv)

    candidate_check = None
    if args.candidates_only:
        from backend.services.filter_service import filter_service

        def candidate_check(content, subject, has_academic_history):
            base_decision = filter_service.check_question(content, subject)
            decision = (
                filter_service.check_question(content, subject, allow_contextual_short_followup=True)
                if has_academic_history else base_decision
            )
            return decision.allowed and decision.reason.endswith("_candidate"), base_decision.allowed

    samples = stratified_limit(build_samples(fetch_rows(args.database_url), candidate_check), args.limit, args.seed)
    prod_count = len(samples)
    if args.adversarial:
        samples.extend(adversarial_samples(Path(args.adversarial)))
    with open(args.out, "w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample, ensure_ascii=False) + "\n")
    blocked = sum(s["current_decision"] == "blocked" for s in samples[:prod_count])
    print(f"exported prod={prod_count} (blocked={blocked}) adversarial={len(samples) - prod_count} -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
