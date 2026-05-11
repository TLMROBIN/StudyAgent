import asyncio
from io import BytesIO

from PIL import Image

from backend.config import Settings
from backend.services import chat_image_understanding_service as image_service_module
from backend.services.chat_image_understanding_service import ChatImageUnderstandingService


def _make_png_bytes(size: tuple[int, int] = (8, 8)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color="white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_understand_upscales_tiny_images_before_llm_ocr(monkeypatch):
    settings = Settings(CHAT_IMAGE_OCR_BACKEND="llm")
    service = ChatImageUnderstandingService(settings=settings)
    captured: dict[str, object] = {}

    async def fake_extract_image_text(*, image_bytes: bytes, mime_type: str, subject: str) -> str:
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


def test_ocr_confidence_rejects_long_garbage_text():
    service = ChatImageUnderstandingService(settings=Settings())

    assert service._assess_ocr_confidence("||||||||||||||||||||||||||||||||||||||||") == "low"
    assert service._assess_ocr_confidence("求 x 满足 x^2=4") == "medium"
    assert service._assess_ocr_confidence("已知函数 f(x)=x^2 的图像经过原点，求单调区间。") == "high"
