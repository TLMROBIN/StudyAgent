import csv
import sqlite3
from pathlib import Path


def _create_eval_db(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY,
                subject VARCHAR(32) NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                llm_model_key VARCHAR(64)
            );
            CREATE TABLE chat_message_attachments (
                id INTEGER PRIMARY KEY,
                message_id INTEGER NOT NULL,
                owner_student_id INTEGER NOT NULL,
                storage_key VARCHAR(500) NOT NULL,
                original_filename VARCHAR(255) NOT NULL,
                mime_type VARCHAR(100) NOT NULL,
                file_size INTEGER NOT NULL,
                sha256 VARCHAR(64) NOT NULL,
                ocr_status VARCHAR(32) NOT NULL,
                ocr_confidence FLOAT,
                created_at DATETIME NOT NULL
            );
            INSERT INTO conversations (id, subject) VALUES (1, '物理');
            INSERT INTO messages (id, conversation_id, content, llm_model_key)
            VALUES (1, 1, '请看图', NULL), (2, 1, '已识别', NULL), (3, 1, '缺文件', NULL);
            INSERT INTO chat_message_attachments (
                id, message_id, owner_student_id, storage_key, original_filename, mime_type,
                file_size, sha256, ocr_status, ocr_confidence, created_at
            ) VALUES
                (1, 1, 1, '1/1/existing.png', 'existing.png', 'image/png', 8, 'a', 'failed', NULL, '2026-06-05 08:00:00'),
                (2, 2, 1, '1/1/skipped.png', 'skipped.png', 'image/png', 8, 'b', 'llm_ocr', 0.9, '2026-06-05 08:00:00'),
                (3, 3, 1, '1/1/missing.png', 'missing.png', 'image/png', 8, 'c', 'failed', NULL, '2026-06-06 08:00:00');
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_eval_image_understanding_reads_failed_rows_without_updating_db(monkeypatch, tmp_path, capsys):
    from backend.services.chat_image_understanding_service import ImageUnderstandingResult
    from scripts import eval_image_understanding

    db_path = tmp_path / "studyagent.db"
    attachments_dir = tmp_path / "chat_attachments"
    image_path = attachments_dir / "1/1/existing.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"png-bytes")
    _create_eval_db(db_path)

    seen: list[dict] = []

    async def fake_understand(**kwargs):
        seen.append(kwargs)
        return ImageUnderstandingResult(
            filter_text="题图条件",
            prompt_summary="题图显示物块受到水平力，要求判断运动状态。",
            ocr_raw_text="题图条件",
            confidence_level="medium",
            source="multimodal",
            must_short_circuit=False,
        )

    monkeypatch.setattr(eval_image_understanding.chat_image_understanding_service, "understand", fake_understand)
    out_path = tmp_path / "eval_results.csv"

    exit_code = eval_image_understanding.main(
        [
            "--db",
            str(db_path),
            "--attachments-dir",
            str(attachments_dir),
            "--status",
            "failed",
            "--limit",
            "10",
            "--since",
            "2026-06-01",
            "--out",
            str(out_path),
        ]
    )

    captured = capsys.readouterr().out
    assert exit_code == 0
    assert "Evaluated images: 1" in captured
    assert "Missing files: 1" in captured
    assert "multimodal: 1" in captured
    assert seen[0]["image_bytes"] == b"png-bytes"
    assert seen[0]["subject"] == "物理"

    rows = list(csv.DictReader(out_path.open()))
    assert rows[0]["attachment_id"] == "1"
    assert rows[0]["source"] == "multimodal"
    assert rows[0]["confidence_level"] == "medium"
    assert rows[0]["must_short_circuit"] == "False"

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute("SELECT ocr_status FROM chat_message_attachments WHERE id = 1").fetchone()[0] == "failed"
    finally:
        connection.close()
