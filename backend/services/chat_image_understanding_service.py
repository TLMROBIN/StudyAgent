from __future__ import annotations

from dataclasses import dataclass
import re

from backend.services.llm_service import llm_service

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
    async def understand(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        subject: str,
        user_text: str,
    ) -> ImageUnderstandingResult:
        ocr_raw_text = await llm_service.extract_image_text(
            image_bytes=image_bytes,
            mime_type=mime_type,
            subject=subject,
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
                image_bytes=image_bytes,
                mime_type=mime_type,
                subject=subject,
                user_text=user_text,
                ocr_text=normalized_ocr,
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

    @staticmethod
    def normalize_text(text: str) -> str:
        return _WHITESPACE_PATTERN.sub(" ", (text or "").strip())

    def _assess_ocr_confidence(self, text: str) -> str:
        if len(text) >= 28:
            return "high"
        if len(text) >= 10:
            return "medium"
        return "low"

    def _assess_multimodal_confidence(self, text: str) -> str:
        if len(text) >= 24:
            return "high"
        if len(text) >= 10:
            return "medium"
        return "low"

    @staticmethod
    def _looks_sufficient_for_direct_use(text: str) -> bool:
        if len(text) < 10:
            return False
        academic_signal_count = sum(
            token in text
            for token in ["求", "解", "图", "函数", "方程", "受力", "电路", "化学", "物理", "数学", "证明"]
        )
        return academic_signal_count >= 1 or any(char.isdigit() for char in text)


chat_image_understanding_service = ChatImageUnderstandingService()
