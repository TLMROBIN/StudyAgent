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
            CREATE TABLE llm_provider_accounts (
                id INTEGER PRIMARY KEY,
                provider_name VARCHAR(64) NOT NULL,
                display_name VARCHAR(64) NOT NULL,
                base_url VARCHAR(255) NOT NULL,
                api_key VARCHAR(512) NOT NULL,
                account_billing_type VARCHAR(32) NOT NULL,
                is_enabled BOOLEAN NOT NULL
            );
            INSERT INTO llm_provider_accounts (
                id, provider_name, display_name, base_url, api_key, account_billing_type, is_enabled
            ) VALUES (
                1, '302ai', '302AI', 'https://api.302ai.cn', 'secret-302-key', 'pay_as_you_go', 1
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
    stopped = []

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
    monkeypatch.setattr(eval_image_understanding, "stop_paddleocr_worker", lambda: stopped.append(True))
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
    assert stopped == [True]

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


def test_eval_image_understanding_matrix_mode_compares_models_without_leaking_api_key(
    monkeypatch,
    tmp_path,
    capsys,
):
    from scripts import eval_image_understanding

    db_path = tmp_path / "studyagent.db"
    attachments_dir = tmp_path / "chat_attachments"
    image_path = attachments_dir / "1/1/existing.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"png-bytes")
    _create_eval_db(db_path)

    requests: list[dict] = []

    async def fake_post_json(*, url, api_key, payload, timeout_seconds):
        requests.append(
            {
                "url": url,
                "api_key": api_key,
                "model": payload["model"],
                "prompt": payload["messages"][0]["content"][0]["text"],
                "image_url": payload["messages"][0]["content"][1]["image_url"]["url"],
                "timeout_seconds": timeout_seconds,
            }
        )
        if payload["model"] == "bad-model":
            raise RuntimeError("upstream refused secret-302-key")
        return {
            "choices": [{"message": {"content": "题干：如图所示，物块受到水平力。"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7},
        }

    monkeypatch.setattr(eval_image_understanding, "post_json", fake_post_json)
    out_path = tmp_path / "matrix_results.csv"
    score_path = tmp_path / "matrix_scores.csv"

    assert (
        eval_image_understanding.chat_completions_url("https://api.302ai.cn/v1")
        == "https://api.302ai.cn/v1/chat/completions"
    )

    exit_code = eval_image_understanding.main(
        [
            "--matrix",
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
            "--provider",
            "302ai",
            "--models",
            "Qwen/Qwen3-VL-32B-Instruct,deepseek-ai/DeepSeek-OCR,bad-model",
            "--out",
            str(out_path),
            "--score-template",
            str(score_path),
        ]
    )

    captured = capsys.readouterr().out
    assert exit_code == 0
    assert "Matrix evaluated rows: 3" in captured
    assert "Qwen/Qwen3-VL-32B-Instruct: success_rate=100.0%" in captured
    assert "bad-model: success_rate=0.0%" in captured
    assert "secret-302-key" not in captured

    assert requests[0]["url"] == "https://api.302ai.cn/v1/chat/completions"
    assert requests[0]["api_key"] == "secret-302-key"
    assert requests[0]["timeout_seconds"] == 90
    assert requests[0]["image_url"].startswith("data:image/png;base64,")
    assert "题干" in requests[0]["prompt"]
    assert "不解答" in requests[0]["prompt"]
    assert requests[1]["prompt"] == "Free OCR."

    rows = list(csv.DictReader(out_path.open()))
    assert [row["model"] for row in rows] == [
        "Qwen/Qwen3-VL-32B-Instruct",
        "deepseek-ai/DeepSeek-OCR",
        "bad-model",
    ]
    assert rows[0]["success"] == "True"
    assert rows[0]["prompt_tokens"] == "11"
    assert rows[0]["completion_tokens"] == "7"
    assert rows[0]["output_preview"] == "题干：如图所示，物块受到水平力。"
    assert rows[2]["success"] == "False"
    assert "secret-302-key" not in rows[2]["output_preview"]

    score_rows = list(csv.DictReader(score_path.open()))
    assert score_rows[0]["stem_completeness_score"] == ""
    assert score_rows[0]["formula_correctness_score"] == ""
    assert score_rows[0]["diagram_description_score"] == ""
