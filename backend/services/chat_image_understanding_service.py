from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from functools import cached_property
from io import BytesIO
from itertools import count
import json
import logging
import multiprocessing as mp
import os
import queue
import re
from threading import Lock
from typing import Any

from backend.config import Settings, get_settings
from backend.services.llm_service import llm_service
from backend.services.metrics_service import chat_image_understanding_total

logger = logging.getLogger(__name__)
_WHITESPACE_PATTERN = re.compile(r"\s+")
_PADDLEOCR_THREAD_ENV_DEFAULTS = {
    "OMP_NUM_THREADS": "2",
    "OPENBLAS_NUM_THREADS": "2",
    "MKL_NUM_THREADS": "2",
    "PADDLE_NUM_THREADS": "2",
}
_PADDLEOCR_STOP = "__studyagent_paddleocr_stop__"


@dataclass
class ImageUnderstandingResult:
    filter_text: str
    prompt_summary: str
    ocr_raw_text: str
    confidence_level: str
    source: str
    must_short_circuit: bool
    understanding_json: dict[str, Any] | None = None
    is_academic: bool = True
    subject_guess: str = ""
    question_text: str = ""
    known_conditions: list[str] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)
    formulas_latex: list[str] = field(default_factory=list)
    diagrams: list[dict[str, str]] = field(default_factory=list)
    handwriting: str = ""
    printed_answer: str = ""
    uncertainties: list[str] = field(default_factory=list)
    quality_issues: list[str] = field(default_factory=list)

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
        llm_image_bytes: bytes | None = None
        llm_mime_type: str | None = None
        if llm_service.prefers_vision_understanding(model_key):
            llm_image_bytes, llm_mime_type = self._prepare_llm_image_payload(
                image_bytes=image_bytes,
                mime_type=mime_type,
            )
            vision_result = await self._understand_vision_first(
                image_bytes=llm_image_bytes,
                mime_type=llm_mime_type,
                subject=subject,
                user_text=user_text,
                model_key=model_key,
                image_path=image_path,
            )
            if vision_result is not None:
                return self._record_understanding_result(vision_result)

        paddleocr_result = await self._try_paddleocr_ocr(image_path=image_path)
        if paddleocr_result is not None:
            return self._record_understanding_result(paddleocr_result)
        if self._ocr_backend() == "paddleocr":
            return self._record_understanding_result(
                ImageUnderstandingResult(
                    filter_text="",
                    prompt_summary="",
                    ocr_raw_text="",
                    confidence_level="low",
                    source="failed",
                    must_short_circuit=True,
                )
            )

        if llm_image_bytes is None or llm_mime_type is None:
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
            return self._record_understanding_result(
                ImageUnderstandingResult(
                    filter_text=normalized_ocr,
                    prompt_summary=normalized_ocr,
                    ocr_raw_text=ocr_raw_text,
                    confidence_level="high",
                    source="ocr",
                    must_short_circuit=False,
                )
            )

        if confidence_level == "medium" and self._looks_sufficient_for_direct_use(normalized_ocr):
            return self._record_understanding_result(
                ImageUnderstandingResult(
                    filter_text=normalized_ocr,
                    prompt_summary=normalized_ocr,
                    ocr_raw_text=ocr_raw_text,
                    confidence_level="medium",
                    source="ocr",
                    must_short_circuit=False,
                )
            )

        multimodal_raw = await llm_service.summarize_academic_image(
            image_bytes=llm_image_bytes,
            mime_type=llm_mime_type,
            subject=subject,
            user_text=user_text,
            ocr_text=normalized_ocr,
            model_key=model_key,
        )
        structured = self._structured_result_from_summary(
            summary_text=multimodal_raw,
            fallback_filter_text=normalized_ocr,
            ocr_raw_text=ocr_raw_text,
            source="multimodal",
        )
        if structured is not None:
            return self._record_understanding_result(structured)

        multimodal_summary = self.normalize_text(multimodal_raw)
        multimodal_confidence = self._assess_multimodal_confidence(multimodal_summary)
        if multimodal_confidence in {"high", "medium"}:
            filter_text = normalized_ocr or multimodal_summary
            return self._record_understanding_result(
                ImageUnderstandingResult(
                    filter_text=filter_text,
                    prompt_summary=multimodal_summary,
                    ocr_raw_text=ocr_raw_text,
                    confidence_level=multimodal_confidence,
                    source="multimodal",
                    must_short_circuit=False,
                )
            )

        partial_summary = multimodal_summary or normalized_ocr
        if partial_summary:
            return self._record_understanding_result(
                ImageUnderstandingResult(
                    filter_text=normalized_ocr or partial_summary,
                    prompt_summary=partial_summary,
                    ocr_raw_text=ocr_raw_text,
                    confidence_level="low",
                    source="multimodal" if multimodal_summary else "ocr",
                    must_short_circuit=True,
                )
            )

        return self._record_understanding_result(
            ImageUnderstandingResult(
                filter_text="",
                prompt_summary="",
                ocr_raw_text=ocr_raw_text,
                confidence_level="low",
                source="failed",
                must_short_circuit=True,
            )
        )

    def _record_understanding_result(self, result: ImageUnderstandingResult) -> ImageUnderstandingResult:
        chat_image_understanding_total.labels(source=result.source, confidence=result.confidence_level).inc()
        return result

    async def _understand_vision_first(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        subject: str,
        user_text: str,
        model_key: str | None,
        image_path: str | None,
    ) -> ImageUnderstandingResult | None:
        multimodal_raw = await llm_service.summarize_academic_image(
            image_bytes=image_bytes,
            mime_type=mime_type,
            subject=subject,
            user_text=user_text,
            ocr_text="",
            model_key=model_key,
        )
        structured = self._structured_result_from_summary(
            summary_text=multimodal_raw,
            fallback_filter_text="",
            ocr_raw_text="",
            source="multimodal",
        )
        multimodal_summary = structured.prompt_summary if structured is not None else self.normalize_text(multimodal_raw)
        skip_ocr_supplement = (
            structured is not None
            and structured.confidence_level == "high"
            and structured.is_academic
            and bool(structured.question_text)
            and not structured.formulas_latex
        )
        ocr_raw_text = ""
        if not skip_ocr_supplement:
            ocr_raw_text = await self._extract_ocr_supplement(
                image_bytes=image_bytes,
                mime_type=mime_type,
                subject=subject,
                model_key=model_key,
                image_path=image_path,
            )
        normalized_ocr = self.normalize_text(ocr_raw_text)
        if structured is not None:
            structured.ocr_raw_text = ocr_raw_text
            if not structured.filter_text:
                structured.filter_text = normalized_ocr
            return structured
        prompt_summary = self._combine_vision_summary_and_ocr(multimodal_summary, normalized_ocr)
        multimodal_confidence = self._assess_multimodal_confidence(multimodal_summary)
        if multimodal_confidence in {"high", "medium"}:
            return ImageUnderstandingResult(
                filter_text=normalized_ocr or multimodal_summary,
                prompt_summary=prompt_summary,
                ocr_raw_text=ocr_raw_text,
                confidence_level=multimodal_confidence,
                source="multimodal",
                must_short_circuit=False,
            )

        ocr_confidence = self._assess_ocr_confidence(normalized_ocr)
        if ocr_confidence == "high" or (
            ocr_confidence == "medium" and self._looks_sufficient_for_direct_use(normalized_ocr)
        ):
            return ImageUnderstandingResult(
                filter_text=normalized_ocr,
                prompt_summary=prompt_summary or normalized_ocr,
                ocr_raw_text=ocr_raw_text,
                confidence_level=ocr_confidence,
                source="ocr",
                must_short_circuit=False,
            )

        if prompt_summary:
            return ImageUnderstandingResult(
                filter_text=normalized_ocr or prompt_summary,
                prompt_summary=prompt_summary,
                ocr_raw_text=ocr_raw_text,
                confidence_level="low",
                source="multimodal" if multimodal_summary else "ocr",
                must_short_circuit=True,
            )
        return None

    async def _extract_ocr_supplement(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        subject: str,
        model_key: str | None,
        image_path: str | None,
    ) -> str:
        paddleocr_result = await self._try_paddleocr_ocr(image_path=image_path)
        if paddleocr_result is not None:
            return paddleocr_result.ocr_raw_text or paddleocr_result.prompt_summary
        if self._ocr_backend() == "paddleocr":
            return ""
        return await llm_service.extract_image_text(
            image_bytes=image_bytes,
            mime_type=mime_type,
            subject=subject,
            model_key=model_key,
        )

    def _combine_vision_summary_and_ocr(self, summary: str, ocr_text: str) -> str:
        summary = self.normalize_text(summary)
        ocr_text = self.normalize_text(ocr_text)
        if summary and ocr_text and ocr_text not in summary:
            return f"{summary} OCR补充：{ocr_text}"
        return summary or ocr_text

    def _structured_result_from_summary(
        self,
        *,
        summary_text: str,
        fallback_filter_text: str,
        ocr_raw_text: str,
        source: str,
    ) -> ImageUnderstandingResult | None:
        data = self._parse_structured_understanding(summary_text)
        if data is None:
            return None

        is_academic = bool(data.get("is_academic", True))
        question_text = self.normalize_text(str(data.get("question_text") or ""))
        known_conditions = self._string_list(data.get("known_conditions"))
        options = self._string_dict(data.get("options"))
        formulas_latex = self._string_list(data.get("formulas_latex"))
        diagrams = self._diagram_list(data.get("diagrams"))
        handwriting = self.normalize_text(str(data.get("handwriting") or ""))
        printed_answer = self.normalize_text(str(data.get("printed_answer") or ""))
        uncertainties = self._string_list(data.get("uncertainties"))
        quality_issues = self._quality_issue_list(data.get("quality_issues"))
        prompt_summary = self._render_structured_prompt_summary(
            question_text=question_text,
            known_conditions=known_conditions,
            options=options,
            formulas_latex=formulas_latex,
            diagrams=diagrams,
            handwriting=handwriting,
            uncertainties=uncertainties,
        )
        filter_text = self._render_structured_filter_text(question_text=question_text, options=options)
        confidence_level = self._assess_multimodal_confidence(prompt_summary)
        if question_text and confidence_level == "low":
            confidence_level = "medium"
        if not is_academic:
            confidence_level = "low"
        return ImageUnderstandingResult(
            filter_text=filter_text or self.normalize_text(fallback_filter_text),
            prompt_summary=prompt_summary,
            ocr_raw_text=ocr_raw_text,
            confidence_level=confidence_level,
            source="off_topic" if not is_academic else source,
            must_short_circuit=not is_academic or confidence_level == "low",
            understanding_json=data,
            is_academic=is_academic,
            subject_guess=self.normalize_text(str(data.get("subject_guess") or "")),
            question_text=question_text,
            known_conditions=known_conditions,
            options=options,
            formulas_latex=formulas_latex,
            diagrams=diagrams,
            handwriting=handwriting,
            printed_answer=printed_answer,
            uncertainties=uncertainties,
            quality_issues=quality_issues,
        )

    def _parse_structured_understanding(self, text: str) -> dict[str, Any] | None:
        raw = (text or "").strip()
        if not raw:
            return None
        for candidate in (raw, self._strip_json_fence(raw)):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _strip_json_fence(text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("```"):
            return stripped
        lines = stripped.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()

    def _render_structured_prompt_summary(
        self,
        *,
        question_text: str,
        known_conditions: list[str],
        options: dict[str, str],
        formulas_latex: list[str],
        diagrams: list[dict[str, str]],
        handwriting: str,
        uncertainties: list[str],
    ) -> str:
        parts: list[str] = []
        if question_text:
            parts.append(f"题干：{question_text}")
        if known_conditions:
            parts.append(f"已知条件：{'；'.join(known_conditions)}")
        if options:
            option_text = "；".join(f"{key}. {value}" for key, value in options.items())
            parts.append(f"选项：{option_text}")
        if formulas_latex:
            parts.append(f"公式：{'；'.join(formulas_latex)}")
        if diagrams:
            diagram_text = "；".join(
                item.get("description") or item.get("type") or "" for item in diagrams if item.get("description") or item.get("type")
            )
            if diagram_text:
                parts.append(f"图示：{diagram_text}")
        if handwriting:
            parts.append(f"手写内容：{handwriting}")
        if uncertainties:
            parts.append(f"不确定处：{'；'.join(uncertainties)}")
        return self.normalize_text(" ".join(parts))

    def _render_structured_filter_text(self, *, question_text: str, options: dict[str, str]) -> str:
        parts = [question_text] if question_text else []
        if options:
            parts.extend(f"{key}. {value}" for key, value in options.items())
        return self.normalize_text(" ".join(parts))

    def _string_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [self.normalize_text(str(item)) for item in value if self.normalize_text(str(item))]

    def _string_dict(self, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        items: dict[str, str] = {}
        for key, item in value.items():
            text = self.normalize_text(str(item))
            if text:
                items[self.normalize_text(str(key))] = text
        return items

    def _diagram_list(self, value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        diagrams: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            diagram_type = self.normalize_text(str(item.get("type") or ""))
            description = self.normalize_text(str(item.get("description") or ""))
            if diagram_type or description:
                diagrams.append({"type": diagram_type, "description": description})
        return diagrams

    def _quality_issue_list(self, value: Any) -> list[str]:
        allowed = {"blur", "dark", "incomplete", "glare"}
        return [item for item in self._string_list(value) if item in allowed]

    async def _try_paddleocr_ocr(self, *, image_path: str | None) -> ImageUnderstandingResult | None:
        backend = self._ocr_backend()
        if backend not in {"hybrid", "paddleocr"} or not image_path:
            return None
        timeout_seconds = max(0.0, float(self.settings.chat_image_ocr_timeout_seconds))
        outer_timeout_seconds = timeout_seconds + 1 if timeout_seconds > 0 else 0
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._try_paddleocr_sync, image_path=image_path),
                timeout=outer_timeout_seconds,
            )
        except TimeoutError:
            logger.warning("chat_image_paddleocr_timeout timeout_seconds=%s", timeout_seconds)
            return None

    def _try_paddleocr_sync(self, *, image_path: str) -> ImageUnderstandingResult | None:
        try:
            raw_text = self._run_paddleocr_safely(image_path)
        except Exception:
            return None

        normalized_text = self.normalize_text(raw_text)
        confidence_level = self._assess_ocr_confidence(normalized_text)
        if confidence_level == "high" or (
            confidence_level == "medium" and self._looks_sufficient_for_direct_use(normalized_text)
        ):
            return ImageUnderstandingResult(
                filter_text=normalized_text,
                prompt_summary=normalized_text,
                ocr_raw_text=raw_text,
                confidence_level=confidence_level,
                source="paddleocr",
                must_short_circuit=False,
            )
        return None

    def _run_paddleocr_safely(self, image_path: str) -> str | None:
        if self._has_test_injected_paddleocr():
            return self._run_paddleocr(image_path)
        return self._run_paddleocr_in_subprocess(
            image_path=image_path,
            timeout_seconds=max(0.0, float(self.settings.chat_image_ocr_timeout_seconds)),
        )

    def _has_test_injected_paddleocr(self) -> bool:
        return "_run_paddleocr" in self.__dict__ or "_paddleocr_class" in self.__dict__

    def _run_paddleocr_in_subprocess(self, *, image_path: str, timeout_seconds: float) -> str | None:
        if timeout_seconds <= 0:
            return None
        return _get_paddleocr_worker().run(image_path, timeout_seconds=timeout_seconds)

    def _run_paddleocr_once_in_subprocess(self, *, image_path: str, timeout_seconds: float) -> str | None:
        if timeout_seconds <= 0:
            return None

        ctx = mp.get_context("spawn")
        output_queue = ctx.Queue(maxsize=1)
        process = ctx.Process(target=_paddleocr_subprocess_worker, args=(image_path, output_queue))
        process.start()
        process.join(timeout_seconds)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            logger.warning("chat_image_paddleocr_subprocess_timeout timeout_seconds=%s", timeout_seconds)
            return None

        try:
            status, payload = output_queue.get_nowait()
        except queue.Empty:
            return None
        finally:
            output_queue.close()

        if status == "ok":
            return str(payload or "")
        logger.warning("chat_image_paddleocr_subprocess_failed error=%s", str(payload)[:300])
        return None

    def _run_paddleocr(self, image_path: str) -> str:
        paddleocr_class = self._paddleocr_class()
        if paddleocr_class is None:
            return ""
        ocr = self._paddleocr_instance(paddleocr_class)
        if hasattr(ocr, "ocr"):
            result = ocr.ocr(image_path)
        elif hasattr(ocr, "predict"):
            result = ocr.predict(input=image_path)
        else:
            return ""
        return self._flatten_paddleocr_text(result)

    @cached_property
    def _cached_paddleocr(self):
        paddleocr_class = self._paddleocr_class()
        if paddleocr_class is None:
            return None
        return self._create_paddleocr(paddleocr_class)

    def _paddleocr_instance(self, paddleocr_class):
        cached = self._cached_paddleocr
        if cached is not None and isinstance(cached, paddleocr_class):
            return cached
        return self._create_paddleocr(paddleocr_class)

    @staticmethod
    def _paddleocr_class():
        for name, value in _PADDLEOCR_THREAD_ENV_DEFAULTS.items():
            os.environ.setdefault(name, value)
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        os.environ.setdefault("DISABLE_MODEL_SOURCE_CHECK", "True")
        try:
            from paddleocr import PaddleOCR
        except Exception:
            return None
        return PaddleOCR

    @staticmethod
    def _create_paddleocr(paddleocr_class):
        try:
            return paddleocr_class(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                lang="ch",
            )
        except TypeError:
            return paddleocr_class(use_angle_cls=False, lang="ch")

    def _flatten_paddleocr_text(self, result: Any) -> str:
        texts: list[str] = []
        self._collect_paddleocr_text(result, texts)
        return self.normalize_text(" ".join(texts))

    def _collect_paddleocr_text(self, value: Any, texts: list[str]) -> None:
        if value is None:
            return
        if isinstance(value, str):
            text = value.strip()
            if text:
                texts.append(text)
            return
        if isinstance(value, dict):
            for key in ("rec_text", "text", "transcription"):
                text = value.get(key)
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
            for key in ("rec_texts", "texts"):
                items = value.get(key)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, str) and item.strip():
                            texts.append(item.strip())
            return
        if isinstance(value, tuple):
            if value and isinstance(value[0], str):
                texts.append(value[0].strip())
                return
            for item in value:
                self._collect_paddleocr_text(item, texts)
            return
        if isinstance(value, list):
            if len(value) >= 2 and isinstance(value[1], tuple) and value[1] and isinstance(value[1][0], str):
                texts.append(value[1][0].strip())
                return
            for item in value:
                self._collect_paddleocr_text(item, texts)

    @staticmethod
    def normalize_text(text: str) -> str:
        return _WHITESPACE_PATTERN.sub(" ", (text or "").strip())

    def _ocr_backend(self) -> str:
        backend = str(self.settings.chat_image_ocr_backend or "hybrid").strip().lower()
        if backend not in {"hybrid", "paddleocr", "llm"}:
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


class _PaddleOCRWorker:
    def __init__(self, *, context=None, worker_target=None) -> None:
        self._context = context or mp.get_context("spawn")
        self._worker_target = worker_target or _paddleocr_worker_loop
        self._process = None
        self._task_queue = None
        self._result_queue = None
        self._request_lock = Lock()
        self._task_ids = count(1)

    def start(self) -> None:
        with self._request_lock:
            self._ensure_started()

    def run(self, image_path: str, *, timeout_seconds: float) -> str | None:
        if timeout_seconds <= 0:
            return None
        with self._request_lock:
            self._ensure_started()
            if self._process is None or self._task_queue is None or self._result_queue is None:
                return None
            task_id = str(next(self._task_ids))
            try:
                self._task_queue.put((task_id, image_path), timeout=1)
                result_task_id, status, payload = self._result_queue.get(timeout=timeout_seconds)
            except queue.Empty:
                logger.warning("chat_image_paddleocr_worker_timeout timeout_seconds=%s", timeout_seconds)
                self._stop_locked(force=True)
                return None
            except Exception as exc:
                logger.warning("chat_image_paddleocr_worker_failed error=%s", str(exc)[:300])
                self._stop_locked(force=True)
                return None

            if result_task_id != task_id:
                logger.warning(
                    "chat_image_paddleocr_worker_stale_result expected_task_id=%s actual_task_id=%s",
                    task_id,
                    result_task_id,
                )
                self._stop_locked(force=True)
                return None
            if status == "ok":
                return str(payload or "")
            logger.warning("chat_image_paddleocr_worker_task_failed error=%s", str(payload)[:300])
            if self._process is not None and not self._process.is_alive():
                self._stop_locked(force=True)
            return None

    def stop(self) -> None:
        with self._request_lock:
            self._stop_locked(force=True)

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        self._close_queues_locked()
        self._task_queue = self._context.Queue(maxsize=1)
        self._result_queue = self._context.Queue(maxsize=1)
        self._process = self._context.Process(
            target=self._worker_target,
            args=(self._task_queue, self._result_queue),
        )
        self._process.start()

    def _stop_locked(self, *, force: bool) -> None:
        process = self._process
        if process is not None and process.is_alive():
            if not force and self._task_queue is not None:
                try:
                    self._task_queue.put(_PADDLEOCR_STOP, timeout=0.2)
                except Exception:
                    pass
                process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        self._process = None
        self._close_queues_locked()

    def _close_queues_locked(self) -> None:
        for worker_queue in (self._task_queue, self._result_queue):
            if worker_queue is None:
                continue
            try:
                worker_queue.close()
            except Exception:
                pass
        self._task_queue = None
        self._result_queue = None


_paddleocr_worker: _PaddleOCRWorker | None = None
_paddleocr_worker_lock = Lock()


def _get_paddleocr_worker() -> _PaddleOCRWorker:
    global _paddleocr_worker
    with _paddleocr_worker_lock:
        if _paddleocr_worker is None:
            _paddleocr_worker = _PaddleOCRWorker()
        return _paddleocr_worker


def warmup_paddleocr_worker() -> None:
    _get_paddleocr_worker().start()


def stop_paddleocr_worker() -> None:
    global _paddleocr_worker
    with _paddleocr_worker_lock:
        worker = _paddleocr_worker
        _paddleocr_worker = None
    if worker is not None:
        worker.stop()


def _paddleocr_worker_loop(task_queue, result_queue) -> None:
    for name, value in _PADDLEOCR_THREAD_ENV_DEFAULTS.items():
        os.environ.setdefault(name, value)
    service = ChatImageUnderstandingService(settings=Settings(CHAT_IMAGE_OCR_BACKEND="paddleocr"))
    try:
        service._cached_paddleocr
    except BaseException as exc:
        logger.warning("chat_image_paddleocr_worker_warmup_failed error=%s", str(exc)[:300])
    while True:
        task = task_queue.get()
        if task == _PADDLEOCR_STOP:
            return
        task_id, image_path = task
        try:
            result_queue.put((task_id, "ok", service._run_paddleocr(image_path)))
        except BaseException as exc:
            result_queue.put((task_id, "error", f"{type(exc).__name__}: {exc}"))


def _paddleocr_subprocess_worker(image_path: str, output_queue) -> None:
    for name, value in _PADDLEOCR_THREAD_ENV_DEFAULTS.items():
        os.environ.setdefault(name, value)
    try:
        service = ChatImageUnderstandingService(settings=Settings(CHAT_IMAGE_OCR_BACKEND="paddleocr"))
        output_queue.put(("ok", service._run_paddleocr(image_path)))
    except BaseException as exc:
        output_queue.put(("error", f"{type(exc).__name__}: {exc}"))


chat_image_understanding_service = ChatImageUnderstandingService()
