import asyncio
from io import BytesIO
import json
import time

from PIL import Image

from backend.config import Settings
from backend.services import chat_image_understanding_service as image_service_module
from backend.services.chat_image_understanding_service import ChatImageUnderstandingService, ImageUnderstandingResult
from backend.services.metrics_service import chat_image_understanding_total


def _make_png_bytes(size: tuple[int, int] = (8, 8)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color="white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_understand_upscales_tiny_images_before_llm_ocr(monkeypatch):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="llm")
    service = ChatImageUnderstandingService(settings=settings)
    captured: dict[str, object] = {}

    async def fake_extract_image_text(*, image_bytes: bytes, mime_type: str, subject: str, **kwargs) -> str:
        captured["mime_type"] = mime_type
        with Image.open(BytesIO(image_bytes)) as image:
            captured["size"] = image.size
        return "已知函数图像经过点 A，求单调区间"

    async def fake_summarize_academic_image(**kwargs) -> str:
        raise AssertionError("high-confidence OCR should not need multimodal fallback")

    monkeypatch.setattr(image_service_module.llm_service, "extract_image_text", fake_extract_image_text)
    monkeypatch.setattr(image_service_module.llm_service, "summarize_academic_image", fake_summarize_academic_image)

    result = asyncio.run(
        service.understand(
            image_bytes=_make_png_bytes(),
            mime_type="image/png",
            subject="数学",
            user_text="",
        )
    )

    assert result.source == "ocr"
    assert captured["mime_type"] == "image/jpeg"
    assert max(captured["size"]) >= 1200


def test_understand_records_result_metric(monkeypatch):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="llm")
    service = ChatImageUnderstandingService(settings=settings)

    async def fake_extract_image_text(**kwargs) -> str:
        return "已知函数 f(x)=x^2 的图像经过原点，求函数单调区间。"

    async def fake_summarize_academic_image(**kwargs) -> str:
        raise AssertionError("high-confidence OCR should not need multimodal fallback")

    monkeypatch.setattr(image_service_module.llm_service, "extract_image_text", fake_extract_image_text)
    monkeypatch.setattr(image_service_module.llm_service, "summarize_academic_image", fake_summarize_academic_image)

    counter = chat_image_understanding_total.labels(source="ocr", confidence="high")
    before = counter._value.get()

    result = asyncio.run(
        service.understand(
            image_bytes=_make_png_bytes(),
            mime_type="image/png",
            subject="数学",
            user_text="",
        )
    )

    assert result.source == "ocr"
    assert counter._value.get() == before + 1


def test_understand_uses_paddleocr_backend_before_llm(monkeypatch, tmp_path):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="paddleocr")
    service = ChatImageUnderstandingService(settings=settings)
    image_path = tmp_path / "question.png"
    image_path.write_bytes(_make_png_bytes())

    async def fake_extract_image_text(**kwargs) -> str:
        raise AssertionError("paddleocr backend should not call LLM OCR after confident text")

    monkeypatch.setattr(image_service_module.llm_service, "extract_image_text", fake_extract_image_text)
    monkeypatch.setattr(
        service,
        "_run_paddleocr",
        lambda path: "4. 如图，空间存在水平向左的匀强电场和垂直纸面向外的匀强磁场，求正确说法。",
    )

    result = asyncio.run(
        service.understand(
            image_bytes=_make_png_bytes(),
            mime_type="image/png",
            subject="物理",
            user_text="",
            image_path=str(image_path),
        )
    )

    assert result.source == "paddleocr"
    assert result.confidence_level == "high"
    assert "匀强电场" in result.prompt_summary


def test_hybrid_chat_image_understanding_skips_mineru(monkeypatch, tmp_path):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="hybrid")
    service = ChatImageUnderstandingService(settings=settings)
    image_path = tmp_path / "question.png"
    image_path.write_bytes(_make_png_bytes())

    async def fake_paddleocr(*, image_path: str):
        return None

    async def fake_mineru(**kwargs):
        raise AssertionError("chat image understanding should not invoke MinerU")

    async def fake_extract_image_text(**kwargs) -> str:
        return "已知函数 f(x)=x^2 的图像经过原点，求函数在不同区间上的单调性。"

    async def fake_summarize_academic_image(**kwargs) -> str:
        raise AssertionError("high-confidence LLM OCR should not need summary fallback")

    monkeypatch.setattr(service, "_try_paddleocr_ocr", fake_paddleocr)
    monkeypatch.setattr(service, "_try_mineru_ocr", fake_mineru, raising=False)
    monkeypatch.setattr(image_service_module.llm_service, "extract_image_text", fake_extract_image_text)
    monkeypatch.setattr(image_service_module.llm_service, "summarize_academic_image", fake_summarize_academic_image)

    result = asyncio.run(
        service.understand(
            image_bytes=_make_png_bytes(),
            mime_type="image/png",
            subject="数学",
            user_text="",
            image_path=str(image_path),
        )
    )

    assert result.source == "ocr"
    assert result.confidence_level == "high"


def test_vision_priority_summarizes_image_before_ocr_supplement(monkeypatch):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="hybrid")
    service = ChatImageUnderstandingService(settings=settings)
    calls: list[str] = []

    monkeypatch.setattr(image_service_module.llm_service, "prefers_vision_understanding", lambda model_key: True)

    async def fake_extract_image_text(**kwargs) -> str:
        calls.append("ocr")
        return "A 点坐标 (1, 2)"

    async def fake_summarize_academic_image(**kwargs) -> str:
        calls.append("summary")
        assert kwargs["ocr_text"] == ""
        return "图中有函数图像，并标出 A 点。"

    monkeypatch.setattr(image_service_module.llm_service, "extract_image_text", fake_extract_image_text)
    monkeypatch.setattr(image_service_module.llm_service, "summarize_academic_image", fake_summarize_academic_image)

    result = asyncio.run(
        service.understand(
            image_bytes=_make_png_bytes(),
            mime_type="image/png",
            subject="数学",
            user_text="",
            model_key="vision-model",
        )
    )

    assert calls == ["summary", "ocr"]
    assert result.source == "multimodal"
    assert "函数图像" in result.prompt_summary
    assert "A 点坐标" in result.prompt_summary


def test_vision_priority_skips_ocr_for_high_confidence_structured_result_without_formulas(monkeypatch):
    service = ChatImageUnderstandingService(settings=Settings(CHAT_IMAGE_OCR_BACKEND="hybrid"))
    payload = {
        "is_academic": True,
        "subject_guess": "math",
        "question_text": "如图所示，函数图像经过 A 点，判断函数在两个区间上的单调性。",
        "known_conditions": ["A 点坐标为 (1, 2)"],
        "options": {},
        "formulas_latex": [],
        "diagrams": [{"type": "graph", "description": "函数图像"}],
        "handwriting": "",
        "printed_answer": "",
        "uncertainties": [],
        "quality_issues": [],
    }

    monkeypatch.setattr(image_service_module.llm_service, "prefers_vision_understanding", lambda model_key: True)

    async def fake_summarize_academic_image(**kwargs) -> str:
        return json.dumps(payload, ensure_ascii=False)

    async def fail_ocr_supplement(**kwargs) -> str:
        raise AssertionError("high-confidence structured result without formulas should skip OCR")

    monkeypatch.setattr(image_service_module.llm_service, "summarize_academic_image", fake_summarize_academic_image)
    monkeypatch.setattr(service, "_extract_ocr_supplement", fail_ocr_supplement)

    result = asyncio.run(
        service.understand(
            image_bytes=_make_png_bytes(),
            mime_type="image/png",
            subject="数学",
            user_text="",
            model_key="vision-model",
        )
    )

    assert result.confidence_level == "high"
    assert result.question_text == payload["question_text"]
    assert result.formulas_latex == []
    assert result.ocr_raw_text == ""


def test_vision_priority_keeps_ocr_supplement_for_structured_formulas(monkeypatch):
    service = ChatImageUnderstandingService(settings=Settings(CHAT_IMAGE_OCR_BACKEND="hybrid"))
    payload = {
        "is_academic": True,
        "subject_guess": "physics",
        "question_text": "如图所示，物体沿斜面运动，结合公式判断加速度方向。",
        "known_conditions": ["斜面光滑"],
        "options": {},
        "formulas_latex": ["a=\\frac{F}{m}"],
        "diagrams": [{"type": "force", "description": "斜面受力图"}],
        "handwriting": "",
        "printed_answer": "",
        "uncertainties": [],
        "quality_issues": [],
    }
    ocr_calls: list[str] = []

    monkeypatch.setattr(image_service_module.llm_service, "prefers_vision_understanding", lambda model_key: True)

    async def fake_summarize_academic_image(**kwargs) -> str:
        return json.dumps(payload, ensure_ascii=False)

    async def fake_ocr_supplement(**kwargs) -> str:
        ocr_calls.append("ocr")
        return "图中公式 a=F/m"

    monkeypatch.setattr(image_service_module.llm_service, "summarize_academic_image", fake_summarize_academic_image)
    monkeypatch.setattr(service, "_extract_ocr_supplement", fake_ocr_supplement)

    result = asyncio.run(
        service.understand(
            image_bytes=_make_png_bytes(),
            mime_type="image/png",
            subject="物理",
            user_text="",
            model_key="vision-model",
        )
    )

    assert result.confidence_level == "high"
    assert result.formulas_latex == payload["formulas_latex"]
    assert result.ocr_raw_text == "图中公式 a=F/m"
    assert ocr_calls == ["ocr"]


def test_low_confidence_partial_understanding_keeps_summary_for_user_correction(monkeypatch):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="llm")
    service = ChatImageUnderstandingService(settings=settings)

    async def fake_extract_image_text(**kwargs) -> str:
        return "模糊"

    async def fake_summarize_academic_image(**kwargs) -> str:
        return "像是电路图"

    monkeypatch.setattr(image_service_module.llm_service, "extract_image_text", fake_extract_image_text)
    monkeypatch.setattr(image_service_module.llm_service, "summarize_academic_image", fake_summarize_academic_image)

    result = asyncio.run(
        service.understand(
            image_bytes=_make_png_bytes(),
            mime_type="image/png",
            subject="物理",
            user_text="",
        )
    )

    assert result.confidence_level == "low"
    assert result.must_short_circuit is True
    assert result.prompt_summary == "像是电路图"


def test_structured_summary_renders_safe_fields_and_isolates_printed_answer(monkeypatch):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="llm")
    service = ChatImageUnderstandingService(settings=settings)
    payload = {
        "is_academic": True,
        "subject_guess": "physics",
        "question_text": "如图所示，电路中电源电压恒定，求电流表示数变化。",
        "known_conditions": ["R1=10Ω", "滑片向右移动"],
        "options": {"A": "变大", "B": "变小"},
        "formulas_latex": ["I=\\frac{U}{R}"],
        "diagrams": [{"type": "circuit", "description": "串联电路含滑动变阻器"}],
        "handwriting": "学生圈出了 A",
        "printed_answer": "参考答案：B",
        "uncertainties": ["右侧电阻标注不清"],
        "quality_issues": ["glare"],
    }

    async def fake_extract_image_text(**kwargs) -> str:
        return "模糊"

    async def fake_summarize_academic_image(**kwargs) -> str:
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(image_service_module.llm_service, "extract_image_text", fake_extract_image_text)
    monkeypatch.setattr(image_service_module.llm_service, "summarize_academic_image", fake_summarize_academic_image)

    result = asyncio.run(
        service.understand(
            image_bytes=_make_png_bytes(),
            mime_type="image/png",
            subject="物理",
            user_text="",
        )
    )

    assert result.is_academic is True
    assert result.question_text == payload["question_text"]
    assert result.printed_answer == "参考答案：B"
    assert result.understanding_json == payload
    assert result.confidence_level in {"medium", "high"}
    assert result.must_short_circuit is False
    assert "参考答案" not in result.prompt_summary
    assert "参考答案" not in result.filter_text
    assert "电流表示数变化" in result.prompt_summary
    assert "R1=10Ω" in result.prompt_summary
    assert "A. 变大" in result.prompt_summary
    assert "B. 变小" in result.filter_text
    assert "I=\\frac{U}{R}" in result.prompt_summary
    assert "右侧电阻标注不清" in result.prompt_summary


def test_structured_summary_parses_markdown_fenced_json(monkeypatch):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="llm")
    service = ChatImageUnderstandingService(settings=settings)

    async def fake_extract_image_text(**kwargs) -> str:
        return ""

    async def fake_summarize_academic_image(**kwargs) -> str:
        return """```json
{"is_academic": true, "subject_guess": "math", "question_text": "求函数单调区间", "known_conditions": [], "options": {}, "formulas_latex": [], "diagrams": [], "handwriting": "", "printed_answer": "答案略", "uncertainties": [], "quality_issues": []}
```"""

    monkeypatch.setattr(image_service_module.llm_service, "extract_image_text", fake_extract_image_text)
    monkeypatch.setattr(image_service_module.llm_service, "summarize_academic_image", fake_summarize_academic_image)

    result = asyncio.run(
        service.understand(
            image_bytes=_make_png_bytes(),
            mime_type="image/png",
            subject="数学",
            user_text="",
        )
    )

    assert result.question_text == "求函数单调区间"
    assert result.understanding_json["printed_answer"] == "答案略"
    assert "答案略" not in result.prompt_summary
    assert result.confidence_level in {"medium", "high"}


def test_structured_question_text_floors_confidence_at_medium(monkeypatch):
    service = ChatImageUnderstandingService(settings=Settings())
    monkeypatch.setattr(service, "_assess_multimodal_confidence", lambda text: "low")

    result = service._structured_result_from_summary(
        summary_text=json.dumps(
            {
                "is_academic": True,
                "subject_guess": "math",
                "question_text": "如图",
                "known_conditions": [],
                "options": {},
                "formulas_latex": [],
                "diagrams": [],
                "handwriting": "",
                "printed_answer": "",
                "uncertainties": [],
                "quality_issues": [],
            },
            ensure_ascii=False,
        ),
        fallback_filter_text="",
        ocr_raw_text="",
        source="multimodal",
    )

    assert result is not None
    assert result.confidence_level == "medium"
    assert result.must_short_circuit is False


def test_structured_summary_invalid_json_falls_back_to_free_text(monkeypatch):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="llm")
    service = ChatImageUnderstandingService(settings=settings)

    async def fake_extract_image_text(**kwargs) -> str:
        return "模糊"

    async def fake_summarize_academic_image(**kwargs) -> str:
        return "像是一道电路图题，右侧公式不清。"

    monkeypatch.setattr(image_service_module.llm_service, "extract_image_text", fake_extract_image_text)
    monkeypatch.setattr(image_service_module.llm_service, "summarize_academic_image", fake_summarize_academic_image)

    result = asyncio.run(
        service.understand(
            image_bytes=_make_png_bytes(),
            mime_type="image/png",
            subject="物理",
            user_text="",
        )
    )

    assert result.understanding_json is None
    assert result.prompt_summary == "像是一道电路图题，右侧公式不清。"
    assert result.filter_text == "模糊"


def test_paddleocr_backend_flattens_common_result_shapes(monkeypatch, tmp_path):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="paddleocr")
    service = ChatImageUnderstandingService(settings=settings)
    image_path = tmp_path / "question.png"
    image_path.write_bytes(_make_png_bytes())

    class FakePaddleOCR:
        def __init__(self, **kwargs):
            pass

        def ocr(self, path):
            return [
                [
                    [[[0, 0], [1, 0], [1, 1], [0, 1]], ("如图，空间存在匀强电场", 0.98)],
                    [[[0, 1], [1, 1], [1, 2], [0, 2]], ("A. 微粒可能带正电", 0.96)],
                ]
            ]

    monkeypatch.setattr(service, "_paddleocr_class", lambda: FakePaddleOCR)

    assert service._run_paddleocr(str(image_path)) == "如图，空间存在匀强电场 A. 微粒可能带正电"


def test_paddleocr_backend_disables_remote_source_check(monkeypatch):
    monkeypatch.delenv("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", raising=False)
    monkeypatch.delenv("DISABLE_MODEL_SOURCE_CHECK", raising=False)
    service = ChatImageUnderstandingService(settings=Settings(CHAT_IMAGE_OCR_BACKEND="paddleocr"))

    service._paddleocr_class()

    assert image_service_module.os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] == "True"
    assert image_service_module.os.environ["DISABLE_MODEL_SOURCE_CHECK"] == "True"


def test_paddleocr_backend_uses_lightweight_v3_constructor(monkeypatch):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="paddleocr")
    service = ChatImageUnderstandingService(settings=settings)
    captured: dict[str, object] = {}

    class FakePaddleOCR:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    service._create_paddleocr(FakePaddleOCR)

    assert captured["use_doc_orientation_classify"] is False
    assert captured["use_doc_unwarping"] is False
    assert captured["use_textline_orientation"] is False
    assert captured["lang"] == "ch"


def test_paddleocr_backend_reports_missing_dependency(monkeypatch, tmp_path):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="paddleocr")
    service = ChatImageUnderstandingService(settings=settings)
    image_path = tmp_path / "question.png"
    image_path.write_bytes(_make_png_bytes())

    monkeypatch.setattr(service, "_paddleocr_class", lambda: None)

    assert service._try_paddleocr_sync(image_path=str(image_path)) is None


def test_paddleocr_worker_restarts_after_task_timeout():
    started_processes: list[FakeProcess] = []

    class FakeQueue:
        def __init__(self, responses=None):
            self.responses = list(responses or [])
            self.items = []
            self.closed = False

        def put(self, item, timeout=None):
            self.items.append(item)

        def get(self, timeout=None):
            if not self.responses:
                raise image_service_module.queue.Empty
            return self.responses.pop(0)

        def close(self):
            self.closed = True

    class FakeProcess:
        def __init__(self, *, target, args):
            self.target = target
            self.args = args
            self.alive = True
            self.terminated = False
            self.killed = False

        def start(self):
            started_processes.append(self)

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.terminated = True
            self.alive = False

        def kill(self):
            self.killed = True
            self.alive = False

        def join(self, timeout=None):
            return None

    class FakeContext:
        def __init__(self):
            self.queue_calls = 0

        def Queue(self, maxsize=0):
            self.queue_calls += 1
            if self.queue_calls == 2:
                return FakeQueue()
            if self.queue_calls == 4:
                return FakeQueue([("2", "ok", "如图，空间存在匀强电场，求正确说法。")])
            return FakeQueue()

        def Process(self, *, target, args):
            return FakeProcess(target=target, args=args)

    worker = image_service_module._PaddleOCRWorker(context=FakeContext())

    assert worker.run("first.png", timeout_seconds=0.01) is None
    assert started_processes[0].terminated is True

    assert worker.run("second.png", timeout_seconds=0.01) == "如图，空间存在匀强电场，求正确说法。"
    assert len(started_processes) == 2


def test_test_injected_paddleocr_bypasses_worker(monkeypatch, tmp_path):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="paddleocr")
    service = ChatImageUnderstandingService(settings=settings)
    image_path = tmp_path / "question.png"
    image_path.write_bytes(_make_png_bytes())

    def fail_worker():
        raise AssertionError("test-injected PaddleOCR should bypass singleton worker")

    monkeypatch.setattr(image_service_module, "_get_paddleocr_worker", fail_worker, raising=False)
    monkeypatch.setattr(service, "_run_paddleocr", lambda path: "已知函数图像经过点 A，求单调区间。")

    assert service._run_paddleocr_safely(str(image_path)) == "已知函数图像经过点 A，求单调区间。"


def test_paddleocr_backend_times_out_hung_worker(monkeypatch, tmp_path):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="paddleocr", CHAT_IMAGE_OCR_TIMEOUT_SECONDS=0)
    service = ChatImageUnderstandingService(settings=settings)
    image_path = tmp_path / "question.png"
    image_path.write_bytes(_make_png_bytes())

    def slow_ocr(*, image_path: str) -> ImageUnderstandingResult:
        time.sleep(0.1)
        return ImageUnderstandingResult(
            filter_text="如图，空间存在匀强电场，求正确说法。",
            prompt_summary="如图，空间存在匀强电场，求正确说法。",
            ocr_raw_text="如图，空间存在匀强电场，求正确说法。",
            confidence_level="high",
            source="paddleocr",
            must_short_circuit=False,
        )

    monkeypatch.setattr(service, "_try_paddleocr_sync", slow_ocr)

    assert asyncio.run(service._try_paddleocr_ocr(image_path=str(image_path))) is None


def test_ocr_confidence_rejects_long_garbage_text():
    service = ChatImageUnderstandingService(settings=Settings())

    assert service._assess_ocr_confidence("||||||||||||||||||||||||||||||||||||||||") == "low"
    assert service._assess_ocr_confidence("求 x 满足 x^2=4") == "medium"
    assert service._assess_ocr_confidence("已知函数 f(x)=x^2 的图像经过原点，求单调区间。") == "high"


def test_image_understanding_rejects_no_image_received_text():
    service = ChatImageUnderstandingService(settings=Settings())

    assert service._assess_ocr_confidence("目前没有收到任何图片可供识别。") == "low"
    assert service._assess_multimodal_confidence("我没有看到图片，请重新上传。") == "low"
