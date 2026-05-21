import asyncio
from io import BytesIO
import time

from PIL import Image

from backend.config import Settings
from backend.services import chat_image_understanding_service as image_service_module
from backend.services.chat_image_understanding_service import ChatImageUnderstandingService, ImageUnderstandingResult


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
