# Student App Issue Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the student-side production issues reported from the wrapped Android app: missing new chat flow, weak photo entry, image understanding uncertainty, blank assistant output, raw 503 HTML, and over-heavy guidance for basic knowledge questions.

**Architecture:** Treat this as an end-to-end product path, not a single WebView bug. The repair is split across Android WebView file chooser/camera behavior, Vue student chat UX, FastAPI SSE/error handling, image understanding fallback, and Socratic mode selection. Each task is independently testable and should preserve the project's "no final answer during guided solving" constraint.

**Tech Stack:** Android Kotlin WebView, Vue 3 + Vite + Element Plus, FastAPI SSE, SQLAlchemy, pytest, existing LLM/image understanding services.

---

## File Map

- `/Users/binyu/Projects/StudyAgentApp/app/src/main/java/com/example/webviewapp/MainActivity.kt`: Android WebView file chooser, camera capture, WebView diagnostics, URL whitelist.
- `/Users/binyu/Projects/StudyAgentApp/app/src/main/AndroidManifest.xml`: optional camera/query metadata only if真机验证 needs it.
- `/Users/binyu/Projects/StudyAgentApp/app/src/main/res/xml/file_paths.xml`: confirm cache paths support camera output.
- `/Users/binyu/Projects/StudyAgent/frontend/src/views/StudentChat.vue`: new conversation action, separate camera/gallery inputs, empty-output UI, basic knowledge mode UI if needed.
- `/Users/binyu/Projects/StudyAgent/frontend/src/utils/api.ts`: SSE error normalization and HTML error sanitization.
- `/Users/binyu/Projects/StudyAgent/backend/routers/chat.py`: empty stream fallback, structured SSE error event, safer 503 handling.
- `/Users/binyu/Projects/StudyAgent/backend/services/chat_image_understanding_service.py`: OCR/multimodal failure result and user-facing status.
- `/Users/binyu/Projects/StudyAgent/backend/services/socratic_service.py`: basic knowledge explanation mode while preserving guided solving rules.
- `/Users/binyu/Projects/StudyAgent/backend/models/schemas.py`: only if a new request field such as `answer_mode` is introduced.
- `/Users/binyu/Projects/StudyAgent/tests/test_chat_stream.py`: SSE, image, empty-output, and 503 regressions.
- `/Users/binyu/Projects/StudyAgent/tests/test_socratic.py`: basic knowledge mode and no-final-answer guard.

---

## Priority Order

1. P0: Stop raw 503/HTML and blank assistant bubbles.
2. P0: Verify and fix Android camera invocation from WebView.
3. P1: Add explicit new conversation/reset flow.
4. P1: Add hybrid image understanding: MinerU OCR first, LLM OCR/multimodal fallback.
5. P2: Add a constrained basic knowledge explanation mode.
6. P2: Add real-device verification checklist and diagnostics.

---

### Task 1: Normalize SSE Failures and Prevent Blank Assistant Replies

**Files:**
- Modify: `/Users/binyu/Projects/StudyAgent/backend/routers/chat.py`
- Modify: `/Users/binyu/Projects/StudyAgent/frontend/src/utils/api.ts`
- Modify: `/Users/binyu/Projects/StudyAgent/frontend/src/views/StudentChat.vue`
- Test: `/Users/binyu/Projects/StudyAgent/tests/test_chat_stream.py`

- [ ] **Step 1: Add failing backend test for empty LLM stream fallback**

Add a test in `tests/test_chat_stream.py` that monkeypatches `llm_service.stream_response` to yield no chunks, then asserts the SSE response emits a non-empty `done.content`. Expected fallback copy:

```text
我刚刚没有生成出有效内容。我们换一种方式继续：请你把题目条件或卡住的一步再发我一次，我会先帮你整理已知条件。
```

- [ ] **Step 2: Implement backend empty-output fallback**

In `backend/routers/chat.py`, after the stream loop and before `done`, if `should_send_done` is true and `emitted_text.strip()` is empty, set `emitted_text` to the fallback copy above. Persist that fallback as the assistant message so replay does not return an empty answer.

- [ ] **Step 3: Add failing frontend unit-level expectation by inspection**

In `frontend/src/utils/api.ts`, update `extractResponseDetail()` so HTML such as `<html><head><title>503 Service Temporarily Unavailable</title>` becomes a friendly message instead of raw HTML:

```text
服务暂时不可用，请稍后重试
```

Keep JSON `{ "detail": "..." }` behavior unchanged.

- [ ] **Step 4: Implement frontend HTML/status sanitization**

Change `extractResponseDetail()` to:

- Return JSON `detail` when available.
- Return `服务暂时不可用，请稍后重试` for status 502/503/504.
- Return `请求失败，请稍后重试` for HTML payloads.
- Return trimmed plain text only when it is not HTML.

- [ ] **Step 5: Make blank assistant bubble self-heal**

In `StudentChat.vue`, after `streamChat()` resolves, check the final assistant message. If it is still blank, replace it with:

```text
这次没有收到有效回复，请重新发送一次，或补充题目条件后再试。
```

This is a UI safety net; the backend fallback remains the source of truth.

- [ ] **Step 6: Verify**

Run:

```bash
.venv/bin/python -m pytest tests/test_chat_stream.py -q
cd frontend && npm run build
```

Expected: chat stream tests pass; frontend builds without TypeScript errors.

---

### Task 2: Fix Android Camera Entry in the Wrapped App

**Files:**
- Modify: `/Users/binyu/Projects/StudyAgentApp/app/src/main/java/com/example/webviewapp/MainActivity.kt`
- Inspect/modify if needed: `/Users/binyu/Projects/StudyAgentApp/app/src/main/AndroidManifest.xml`
- Inspect/modify if needed: `/Users/binyu/Projects/StudyAgentApp/app/src/main/res/xml/file_paths.xml`
- Modify: `/Users/binyu/Projects/StudyAgent/frontend/src/views/StudentChat.vue`

- [ ] **Step 1: Confirm current behavior**

Install the current App on the M9 device. Open student chat and tap `上传图片 / 拍照`. Record which path opens:

- Only album picker opens: frontend is not requesting capture.
- Camera opens but result is lost: Android URI/callback problem.
- Nothing opens: WebView `onShowFileChooser()` or permission/activity result problem.

- [ ] **Step 2: Split frontend inputs into camera and gallery**

In `StudentChat.vue`, replace the single hidden file input with two hidden inputs:

```html
<input
  ref="cameraInputRef"
  type="file"
  accept="image/*"
  capture="environment"
  style="display: none"
  @change="handleImageSelection"
/>
<input
  ref="galleryInputRef"
  type="file"
  accept="image/*"
  style="display: none"
  @change="handleImageSelection"
/>
```

Add two buttons:

```text
拍照
从相册选择
```

Keep the existing one-image limit and preview behavior.

- [ ] **Step 3: Adjust Android accept-type detection**

In `MainActivity.kt`, update `isImageOnly` so empty accept types plus capture still count as image when the frontend uses `accept="image/*"` but WebView reports empty/odd values on a specific device:

```kotlin
val acceptTypes = fileChooserParams.acceptTypes.filter { it.isNotBlank() }
val isImageOnly = acceptTypes.isEmpty() || acceptTypes.all {
    it == "image/*" || it.startsWith("image/")
}
```

Only apply the camera path when `isCaptureEnabled && isImageOnly`.

- [ ] **Step 4: Add lightweight diagnostics for真机验证**

In `onShowFileChooser()`, add temporary `Log.d` lines for:

- accept types
- mode
- `isCaptureEnabled`
- selected branch: camera, single image picker, multiple image picker, generic chooser

Remove or lower verbosity after M9 verification.

- [ ] **Step 5: Verify Android build**

Run in `/Users/binyu/Projects/StudyAgentApp`:

```bash
./gradlew :app:assembleDebug
```

Expected: debug APK builds.

- [ ] **Step 6: Verify on M9**

On the actual M9 device:

- Tap `拍照`, camera opens.
- Take a photo, return to chat, preview appears.
- Send image-only message, user bubble shows the image.
- Tap `从相册选择`, album picker opens.
- Select an image, preview appears.

---

### Task 3: Add Explicit New Conversation Flow

**Files:**
- Modify: `/Users/binyu/Projects/StudyAgent/frontend/src/views/StudentChat.vue`
- Test manually via browser/App.

- [ ] **Step 1: Add `startNewConversation()`**

In `StudentChat.vue`, add a function that:

- sets `currentConversationId.value = null`
- clears `messages.value`
- resets `guidanceStage.value = 'initial_guidance'`
- calls `resetPendingImage()`
- calls `clearLocalAttachmentUrls()`
- calls `resetRecommendations()`
- keeps the currently selected subject

- [ ] **Step 2: Add a visible button**

In the left conversation panel header, add:

```html
<button class="primary-button" :disabled="sending" @click="startNewConversation">新建对话</button>
```

Keep `刷新` as a secondary action.

- [ ] **Step 3: Guard subject switching**

When `form.subject` changes while `currentConversationId` is not null and messages exist, show a small confirmation using Element Plus:

```text
切换学科建议新建对话，避免不同学科上下文混在一起。
```

Acceptance can call `startNewConversation()` and then apply the new subject.

- [ ] **Step 4: Verify**

Manual checks:

- New conversation clears old messages.
- First send after new conversation creates a new backend conversation.
- Existing history can still be reopened.
- Subject switching no longer silently continues the old conversation.

---

### Task 4: Add Hybrid Image Understanding with MinerU OCR Fallback Chain

**Files:**
- Modify: `/Users/binyu/Projects/StudyAgent/backend/services/mineru_service.py`
- Modify: `/Users/binyu/Projects/StudyAgent/backend/services/chat_image_understanding_service.py`
- Modify: `/Users/binyu/Projects/StudyAgent/backend/routers/chat.py`
- Modify if needed: `/Users/binyu/Projects/StudyAgent/backend/config.py`
- Modify: `/Users/binyu/Projects/StudyAgent/frontend/src/views/StudentChat.vue`
- Test: `/Users/binyu/Projects/StudyAgent/tests/test_chat_stream.py`
- Test: `/Users/binyu/Projects/StudyAgent/tests/test_mineru_service.py`

- [ ] **Step 1: Define the hybrid recognition order**

Use this order for chat images:

1. Try MinerU OCR first for printed题干、公式、表格、选项文字.
2. If MinerU succeeds with enough usable text, use it as `filter_text` and the base `prompt_summary`.
3. If MinerU fails, times out, or returns too little text, fall back to current `llm_service.extract_image_text()`.
4. If OCR text is still weak, call `llm_service.summarize_academic_image()` to capture图形、电路、实验装置、坐标系等视觉结构.
5. If all paths are low confidence, short-circuit with a friendly "识别不清，请重拍或补充题干" prompt.

Do not remove the LLM multimodal path. MinerU improves text extraction; LLM remains useful for non-text visual reasoning.

- [ ] **Step 2: Add MinerU image OCR adapter**

In `backend/services/mineru_service.py`, add a small chat-image adapter rather than changing PDF ingest behavior.

Preferred implementation:

- Create `ocr_image_via_pdf(image_path: str, *, task_id: int, document_id: int) -> PDFParseResult`.
- Convert the image into a temporary single-page PDF under `task_artifact_path/chat-image-ocr/<task_id>/`.
- Call existing `parse_pdf()` so GPU preflight, runtime env, timeout, malformed-output checks, and provenance remain centralized.
- Return the parsed `PDFParseResult`.

Acceptance detail:

- PDF/DOCX ingestion behavior must remain unchanged.
- Any temporary files must stay under `task_artifact_path`, not beside the uploaded chat attachment.
- MinerU errors should be raised as existing `MineruError` subclasses where possible.

- [ ] **Step 3: Add short timeout and backend selection config**

In `backend/config.py`, add conservative chat-image OCR settings:

```text
CHAT_IMAGE_OCR_BACKEND=hybrid
CHAT_IMAGE_OCR_TIMEOUT_SECONDS=8
CHAT_IMAGE_MINERU_MIN_TEXT_CHARS=10
```

Supported backend values:

- `hybrid`: MinerU first, then LLM OCR/multimodal fallback.
- `mineru`: MinerU only, then low-confidence short-circuit if weak.
- `llm`: current behavior, useful when MinerU runtime is unavailable.

The default should be `hybrid` only if MinerU runtime is available in the deployment environment; otherwise choose `llm` or fail open to LLM at runtime.

- [ ] **Step 4: Integrate MinerU into `ChatImageUnderstandingService`**

In `backend/services/chat_image_understanding_service.py`, update `understand()` to:

- Save/use the existing attachment path when available, or accept an optional `image_path`.
- Run MinerU OCR with `asyncio.wait_for(..., timeout=CHAT_IMAGE_OCR_TIMEOUT_SECONDS)` or an equivalent thread timeout wrapper.
- Normalize MinerU text with existing `normalize_text()`.
- Assess confidence using the existing `_assess_ocr_confidence()` and `_looks_sufficient_for_direct_use()`.
- Return `ImageUnderstandingResult(source="mineru_ocr", confidence_level="high"|"medium")` when usable.
- Fall back to current LLM OCR and multimodal summary if MinerU is weak or unavailable.

Do not let MinerU exceptions bubble up to the student chat request unless the configured backend is explicitly `mineru`.

- [ ] **Step 5: Preserve structured source metadata**

In `backend/routers/chat.py`, update OCR status mapping:

```python
{
    "mineru_ocr": "mineru_ocr",
    "ocr": "llm_ocr",
    "multimodal": "multimodal_fallback",
    "failed": "failed",
}
```

Also pass the stored attachment path into `chat_image_understanding_service.understand()` so MinerU can read the exact uploaded image without duplicating bytes unnecessarily.

- [ ] **Step 6: Add tests for hybrid behavior**

In `tests/test_chat_stream.py`, cover:

- MinerU OCR succeeds: `source == "mineru_ocr"`, assistant uses recognized text, attachment `ocr_status == "mineru_ocr"`.
- MinerU raises/transient failure: service falls back to LLM OCR and still answers.
- MinerU returns short/weak text and LLM multimodal succeeds: `source == "multimodal"`.
- All recognition paths fail: short-circuit with image-low-confidence text.

In `tests/test_mineru_service.py`, cover the new adapter at the unit boundary by monkeypatching the PDF parse command/output rather than requiring a real MinerU install.

- [ ] **Step 7: Keep low-confidence image reply clear**

Use the existing image tests in `test_chat_stream.py` as the pattern. Monkeypatch `chat_image_understanding_service.understand()` to return:

```python
ImageUnderstandingResult(
    filter_text="",
    prompt_summary="",
    ocr_raw_text="",
    confidence_level="low",
    source="failed",
    must_short_circuit=True,
)
```

Assert the assistant reply asks the student to add the text or retake the photo, and does not pretend the image was understood.

- [ ] **Step 8: Keep low-confidence short-circuit**

Preserve the current `must_short_circuit` branch in `backend/routers/chat.py`, but ensure the text is student-friendly and actionable:

```text
这张图片里的题目我没有识别清楚。你可以重新拍一张更清晰的照片，或者把题干关键文字发出来；我会继续引导你分析。
```

- [ ] **Step 9: Surface OCR state in UI**

When the assistant response contains the low-confidence text, the UI already displays it as a normal assistant message. Do not add a separate badge unless backend exposes attachment OCR status in `ChatMessageAttachment`; avoid schema expansion unless needed.

- [ ] **Step 10: Verify**

Run:

```bash
.venv/bin/python -m pytest tests/test_chat_stream.py -q
.venv/bin/python -m pytest tests/test_mineru_service.py -q
```

Expected: hybrid image recognition, fallback, and low-confidence paths are covered.

---

### Task 5: Add Constrained Basic Knowledge Mode

**Files:**
- Modify: `/Users/binyu/Projects/StudyAgent/backend/services/socratic_service.py`
- Modify if needed: `/Users/binyu/Projects/StudyAgent/backend/models/schemas.py`
- Modify if needed: `/Users/binyu/Projects/StudyAgent/frontend/src/views/StudentChat.vue`
- Test: `/Users/binyu/Projects/StudyAgent/tests/test_socratic.py`
- Test: `/Users/binyu/Projects/StudyAgent/tests/test_filter.py`

- [ ] **Step 1: Define product rule**

Use this rule:

- Concept/definition questions can receive a concise explanation plus one check question.
- Exercise-solving questions must stay in Socratic guidance and must not reveal the final answer.
- If uncertain, prefer Socratic guidance.

- [ ] **Step 2: Add backend mode only if classification is deterministic**

Prefer a simple non-LLM classifier in `socratic_service.py`:

Concept keywords:

```text
什么是、是什么意思、区别、概念、定义、原理、为什么
```

Exercise-solving signals:

```text
求、计算、证明、答案、选项、如图、第几题、解方程
```

If both appear, treat as exercise-solving.

- [ ] **Step 3: Add tests**

In `test_socratic.py`, add:

- `什么是惯性` allows concise explanation mode.
- `这道题答案是多少` stays guided.
- `求这个函数最大值` stays guided.
- Image-related turns stay guided unless OCR clearly contains only a concept question.

- [ ] **Step 4: Implement prompt change**

For concept mode, prompt should require:

```text
先用 2-4 句解释基础概念，再问 1 个检查理解的问题。不要代写题目最终答案；如果问题其实是具体习题，转为引导式提问。
```

Do not bypass `filter_service.validate_answer()`.

- [ ] **Step 5: Verify**

Run:

```bash
.venv/bin/python -m pytest tests/test_socratic.py tests/test_filter.py -q
```

Expected: concept mode works without weakening answer-safety tests.

---

### Task 6: Deployment and Real-Device Acceptance

**Files:**
- Modify if useful: `/Users/binyu/Projects/StudyAgent/P0_端到端联调与真机平板测试手把手指南.md`

- [ ] **Step 1: Run backend/frontend checks**

Run:

```bash
.venv/bin/python -m pytest tests/test_chat_stream.py tests/test_socratic.py tests/test_filter.py -q
.venv/bin/python -m compileall backend tests locustfile.py
cd frontend && npm run build
```

- [ ] **Step 2: Restart deployed services**

If using compose deployment, run:

```bash
sg docker -c '/usr/bin/docker compose restart backend worker nginx'
curl -fsS http://127.0.0.1:8002/health
```

- [ ] **Step 3: Install Android debug build**

Run in `/Users/binyu/Projects/StudyAgentApp`:

```bash
./gradlew :app:assembleDebug
```

Install the debug APK on M9.

- [ ] **Step 4: Acceptance checklist**

Verify on actual student device:

- Login succeeds and persists after App restart.
- `新建对话` creates a clean conversation.
- Switching subjects does not mix previous subject context.
- Text-only question streams a visible assistant reply.
- Simulated backend overload shows friendly text, not HTML.
- `拍照` opens camera and returns preview.
- `从相册选择` opens gallery and returns preview.
- Blurry image gets a clear "识别不清" recovery prompt.
- Clear printed题干 image enters MinerU OCR and gets a guided response.
- Graph/electric-circuit/experiment images can still use LLM multimodal fallback when MinerU text alone is insufficient.
- Basic concept question gets short explanation plus check question.
- Concrete exercise question still does not receive final answer.

---

## Commit Slices

1. `frontend: make chat failures and conversations recoverable`
2. `android: support explicit camera and gallery WebView uploads`
3. `backend: add hybrid MinerU and LLM image understanding`
4. `socratic: add constrained concept explanation mode`
5. `docs: record student app acceptance checklist`

Use the project Lore commit format from `AGENTS.md` for each commit.

---

## Risks and Constraints

- Do not weaken `filter_service.validate_answer()` or the no-final-answer guard.
- Do not broaden PDF/RAG behavior; this plan does not touch document ingestion.
- Do not rely on browser-only validation for camera; the App WebView and M9 device are required.
- Do not let MinerU latency make chat feel hung; use short timeouts and fall back to LLM/image-low-confidence text.
- Do not expose raw provider errors, HTML upstream errors, or stack traces to students.
- Do not add storage permissions unless真机验证 proves they are required.
