from __future__ import annotations

import asyncio
from io import BytesIO
from dataclasses import dataclass
import re

from backend.config import Settings, get_settings
from backend.services.llm_service import llm_service
from backend.services.mineru_service import MineruError, mineru_service

_WHITESPACE_PATTERN = re.compile(r"\s+")


@dataclass
class ImageUnderstandingResult:
    filter_text: str
    prompt_summary: str
    ocr_raw_text: str
    confidence_level: str
    source: str
    must_short_circuit: bool

    @property
    def ocr_confidence_value(self) -> float:
        if self.confidence_level == "high":
            return 0.92
        if self.confidence_level == "medium":
            return 0.66
        if self.confidence_level == "low":
            return 0.25
        return 0.0


class ChatImageUnderstandingService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def understand(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        subject: str,
        user_text: str,
        model_key: str | None = None,
        image_path: str | None = None,
        attachment_id: int | None = None,
    ) -> ImageUnderstandingResult:
        mineru_result = await self._try_mineru_ocr(
            image_path=image_path,
            attachment_id=attachment_id,
        )
        if mineru_result is not None:
            return mineru_result
        if self._ocr_backend() == "mineru":
            return ImageUnderstandingResult(
                filter_text="",
                prompt_summary="",
                ocr_raw_text="",
                confidence_level="low",
                source="failed",
                must_short_circuit=True,
            )

        llm_image_bytes, llm_mime_type = self._prepare_llm_image_payload(
            image_bytes=image_bytes,
            mime_type=mime_type,
        )
        ocr_raw_text = await llm_service.extract_image_text(
            image_bytes=llm_image_bytes,
            mime_type=llm_mime_type,
            subject=subject,
            model_key=model_key,
        )
        normalized_ocr = self.normalize_text(ocr_raw_text)
        confidence_level = self._assess_ocr_confidence(normalized_ocr)

        if confidence_level == "high":
            return ImageUnderstandingResult(
                filter_text=normalized_ocr,
                prompt_summary=normalized_ocr,
                ocr_raw_text=ocr_raw_text,
                confidence_level="high",
                source="ocr",
                must_short_circuit=False,
            )

        if confidence_level == "medium" and self._looks_sufficient_for_direct_use(normalized_ocr):
            return ImageUnderstandingResult(
                filter_text=normalized_ocr,
                prompt_summary=normalized_ocr,
                ocr_raw_text=ocr_raw_text,
                confidence_level="medium",
                source="ocr",
                must_short_circuit=False,
            )

        multimodal_summary = self.normalize_text(
            await llm_service.summarize_academic_image(
                image_bytes=llm_image_bytes,
                mime_type=llm_mime_type,
                subject=subject,
                user_text=user_text,
                ocr_text=normalized_ocr,
                model_key=model_key,
            )
        )
        multimodal_confidence = self._assess_multimodal_confidence(multimodal_summary)
        if multimodal_confidence in {"high", "medium"}:
            filter_text = normalized_ocr or multimodal_summary
            return ImageUnderstandingResult(
                filter_text=filter_text,
                prompt_summary=multimodal_summary,
                ocr_raw_text=ocr_raw_text,
                confidence_level=multimodal_confidence,
                source="multimodal",
                must_short_circuit=False,
            )

        return ImageUnderstandingResult(
            filter_text="",
            prompt_summary="",
            ocr_raw_text=ocr_raw_text,
            confidence_level="low",
            source="failed",
            must_short_circuit=True,
        )

    async def _try_mineru_ocr(
        self,
        *,
        image_path: str | None,
        attachment_id: int | None,
    ) -> ImageUnderstandingResult | None:
        backend = self._ocr_backend()
        if backend not in {"hybrid", "mineru"} or not image_path:
            return None

        task_id = -(abs(attachment_id or 1))
        document_id = -(abs(attachment_id or 1))
        try:
            parsed = await asyncio.to_thread(
                mineru_service.ocr_image_via_pdf,
                image_path,
                task_id=task_id,
                document_id=document_id,
                timeout_seconds=self.settings.chat_image_ocr_timeout_seconds,
            )
        except MineruError:
            return None

        normalized_text = self.normalize_text(parsed.text)
        if len(normalized_text) < self.settings.chat_image_mineru_min_text_chars:
            return None
        confidence_level = self._assess_ocr_confidence(normalized_text)
        if confidence_level == "high" or (
            confidence_level == "medium" and self._looks_sufficient_for_direct_use(normalized_text)
        ):
            return ImageUnderstandingResult(
                filter_text=normalized_text,
                prompt_summary=normalized_text,
                ocr_raw_text=parsed.text,
                confidence_level=confidence_level,
                source="mineru_ocr",
                must_short_circuit=False,
            )
        return None

    @staticmethod
    def normalize_text(text: str) -> str:
        return _WHITESPACE_PATTERN.sub(" ", (text or "").strip())

    def _ocr_backend(self) -> str:
        backend = str(self.settings.chat_image_ocr_backend or "hybrid").strip().lower()
        if backend not in {"hybrid", "mineru", "llm"}:
            return "hybrid"
        return backend

    def _assess_ocr_confidence(self, text: str) -> str:
        normalized = self.normalize_text(text)
        if self._looks_like_no_image_received(normalized):
            return "low"
        compact = normalized.replace(" ", "")
        if len(compact) < 6:
            return "low"
        if self._meaningful_char_ratio(compact) < 0.45:
            return "low"
        if len(compact) >= 24:
            return "high"
        if self._looks_sufficient_for_direct_use(normalized):
            return "medium"
        return "low"

    def _assess_multimodal_confidence(self, text: str) -> str:
        if self._looks_like_no_image_received(text):
            return "low"
        if len(text) >= 24:
            return "high"
        if len(text) >= 10:
            return "medium"
        return "low"

    @staticmethod
    def _looks_sufficient_for_direct_use(text: str) -> bool:
        if ChatImageUnderstandingService._looks_like_no_image_received(text):
            return False
        if len(text) < 10:
            return False
        academic_signal_count = sum(
            token in text
            for token in ["求", "解", "图", "函数", "方程", "受力", "电路", "化学", "物理", "数学", "证明"]
        )
        return academic_signal_count >= 1 or any(char.isdigit() for char in text)

    @staticmethod
    def _looks_like_no_image_received(text: str) -> bool:
        normalized = _WHITESPACE_PATTERN.sub("", (text or "").strip())
        if not normalized:
            return False
        patterns = [
            r"(?:没有|未|没)收到.*图片",
            r"(?:没有|未|没)看到.*图片",
            r"看不到.*图片",
            r"无法(?:查看|识别|读取|看清).*图片",
            r"不能(?:看到|查看|识别|读取).*图片",
            r"请(?:重新)?上传.*图片",
            r"图片.*(?:未|没有|没)提供",
        ]
        return any(re.search(pattern, normalized) for pattern in patterns)

    @staticmethod
    def _meaningful_char_ratio(text: str) -> float:
        if not text:
            return 0.0
        formula_symbols = set("+-*/=^_().,，。:：;；<>≤≥√∠παβγθλΩ%[]{}")
        meaningful_count = sum(char.isalnum() or char in formula_symbols for char in text)
        return meaningful_count / len(text)

    def _prepare_llm_image_payload(self, *, image_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
        try:
            from PIL import Image, ImageFilter, ImageOps

            with Image.open(BytesIO(image_bytes)) as source:
                image = ImageOps.exif_transpose(source)
                if image.mode in {"RGBA", "LA"}:
                    background = Image.new("RGB", image.size, "white")
                    alpha = image.getchannel("A")
                    background.paste(image.convert("RGB"), mask=alpha)
                    image = background
                elif image.mode == "P":
                    image = image.convert("RGB")
                elif image.mode != "RGB":
                    image = image.convert("RGB")

                image = self._resize_for_ocr(image)
                image = ImageOps.autocontrast(image, cutoff=1)
                image = image.filter(ImageFilter.SHARPEN)

                output = BytesIO()
                image.save(
                    output,
                    format="JPEG",
                    quality=max(75, min(95, int(self.settings.chat_image_preprocess_jpeg_quality))),
                    optimize=True,
                )
                return output.getvalue(), "image/jpeg"
        except Exception:
            return image_bytes, mime_type

    def _resize_for_ocr(self, image):
        width, height = image.size
        long_edge = max(width, height)
        if long_edge <= 0:
            return image
        min_long_edge = max(1, int(self.settings.chat_image_preprocess_min_long_edge))
        max_long_edge = max(min_long_edge, int(self.settings.chat_image_preprocess_max_long_edge))
        scale = 1.0
        if long_edge < min_long_edge:
            scale = min_long_edge / long_edge
        elif long_edge > max_long_edge:
            scale = max_long_edge / long_edge
        if scale == 1.0:
            return image
        from PIL import Image

        resampling = getattr(Image, "Resampling", Image).LANCZOS
        return image.resize((max(1, round(width * scale)), max(1, round(height * scale))), resampling)


chat_image_understanding_service = ChatImageUnderstandingService()
