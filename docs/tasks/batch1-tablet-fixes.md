# 第一批修复任务单：平板端体验（4 项）

> 交付对象：AI 编码代理。所有成因均已核实到具体行号（基于 main @ b1731fc），生产数据佐证来自远端库 `data/studyagent.db`。
> 按任务 A → B → C → D 顺序执行，每个任务独立 commit。

---

## 全局约束（先读）

1. **最小可逆 diff**，严禁顺手 refactor / cleanup / 格式化无关代码。
2. 测试必须用项目 venv：`.venv/bin/python -m pytest ...`，不要假设 PATH 里有 pytest。
3. 前端没有 vitest/jest。**前端行为断言写在 `tests/` 下的 Python 测试里**（读源码文本做断言），参考 `tests/test_student_chat_frontend.py` 的既有模式。
4. 每个任务完成后必须跑该任务的"验收命令"小节，全部通过才算完成；最后跑一次全量：
   ```bash
   .venv/bin/python -m pytest -q
   .venv/bin/python -m compileall backend tests locustfile.py
   cd frontend && npm run build
   ```
5. Commit 用 Lore 格式（意图开头 + trailer），每任务一个 commit，只提交任务相关文件。
6. 不修改 `.omx/plans/` 下任何文件。
7. 涉及 `socratic_service.py` / `filter_service.py` 的改动（任务 C）必须额外跑 `tests/test_filter.py`，确认对抗用例零回归。

---

## 任务 A：修复消息内容溢出屏幕（表格 / KaTeX 公式 / 长文本）

**用户反馈原文**（生产库 student_feedback 表）：#81"有时候ai回答时不会换行，导致句子长到屏幕外去了，然后又没有横向滑动的功能"；#79"AI生成公式表格什么的会生成到屏幕外面"；#33 给出了一个英语直接引语/间接引语 markdown 表格全乱的实例。

**已核实成因**（全部在 `frontend/src/styles.css`）：

1. **表格单元格强制不换行**：764–771 行 `.message-body th, .message-body td { ... white-space: nowrap; }` —— 长单元格内容不折行，直接把表格撑出气泡。
2. **表格滚动容器失效**：755–762 行 `.message-body table { display: block; width: max-content; max-width: 100%; overflow-x: auto; }` 本身是标准模式，但 `.message-body` 是 `display: grid`（693–700 行），**grid 子项默认 `min-width: auto`**，表格无法收缩到容器宽度以内，`max-width: 100%` + `overflow-x: auto` 实际不生效。
3. **行内公式无溢出处理**：只有 `.katex-display`（850–855 行）有 `overflow-x: auto`，行内 `.katex` 只有 `max-width: 100%`（702–709 行），KaTeX 行内公式不可折行，长公式直接溢出。
4. **溢出后不可达**：`.chat-stream { overflow-x: hidden }`（624 行）把溢出内容直接裁掉且无横向滚动 —— 对应反馈"无横向滑动功能，看不到"。**此行为保留**（页面不应横向滚，滚动应发生在表格/公式内部），前三项修好后它不再是问题。

**改动**（仅 `frontend/src/styles.css`，纯 CSS，不动 richText.ts 渲染逻辑）：

1. 在 `.message-body` 规则块（693 行附近）之后新增：
   ```css
   .message-body > * {
     min-width: 0;
     max-width: 100%;
   }
   ```
2. 修改 764–771 行的 `th, td` 规则：`white-space: nowrap` 改为
   ```css
   white-space: normal;
   word-break: break-word;
   ```
3. 新增行内 KaTeX 溢出处理（放在 `.katex-display` 规则附近，注意选择器要排除 display 公式内部的 `.katex`，否则会给块级公式套两层滚动）：
   ```css
   .message-body :not(.katex-display) > .katex {
     display: inline-block;
     max-width: 100%;
     overflow-x: auto;
     overflow-y: hidden;
     vertical-align: middle;
   }
   ```
4. 不要改 `.bubble` 的 `max-width: min(78%, 720px)`（639 行），不要改 `.chat-stream` 的 overflow。

**新增测试**：建 `tests/test_tablet_layout_frontend.py`，按源码断言模式：

```python
from pathlib import Path

def test_message_table_cells_wrap_instead_of_nowrap():
    css = Path("frontend/src/styles.css").read_text()
    start = css.index(".message-body th,")
    end = css.index("}", start)
    cell_rule = css[start:end]
    assert "white-space: normal" in cell_rule
    assert "nowrap" not in cell_rule

def test_message_body_children_can_shrink_inside_grid():
    css = Path("frontend/src/styles.css").read_text()
    assert ".message-body > *" in css

def test_inline_katex_has_overflow_handling():
    css = Path("frontend/src/styles.css").read_text()
    assert ":not(.katex-display) > .katex" in css
```

**验收命令**：
```bash
.venv/bin/python -m pytest tests/test_tablet_layout_frontend.py tests/test_student_chat_frontend.py -q
cd frontend && npm run build
```

**手工验收口径**（写进 PR 描述，不要求自动化）：DevTools 模拟 768×1024 与 1024×768，向 AI 索要①一个 6 列 markdown 表格、②一条长 `$$...$$` 公式、③一条长行内 `$...$` 公式。三者均不得撑破气泡；表格与公式可在自身内部横向滚动。

---

## 任务 B：修复软键盘遮挡输入框

**用户反馈原文**：#44"键盘会挡住打字框，看不到自己打字打的是什么"；#60"键盘还是遮挡输入框"（7/1，说明之前修过但没修好）；#46 学生自己提出了"在键盘上显示文字"的 workaround 诉求。

**已核实成因**：

1. `frontend/index.html` 第 5 行：`<meta name="viewport" content="width=device-width, initial-scale=1.0" />` —— 缺 `interactive-widget=resizes-content`。Android Chrome/WebView 108+ 默认键盘**覆盖**内容而不压缩布局视口，这是"还是遮挡"的直接原因（学生平板以 Android 为主）。
2. 基础设施其实已存在一半：`frontend/src/utils/viewportHeight.ts` 已监听 `visualViewport` 的 resize/scroll，把 `min(innerHeight, visualViewport.height)` 写入 CSS 变量 `--app-viewport-height`；`styles.css` 16 行 `--student-panel-height` 也消费了它。**但**多处消费点只用了 `min-height: var(--app-viewport-height)`（styles.css 32、53、139、145 行）——`min-height` 只约束下限，变量变小时容器不会实际收缩，键盘弹出后布局纹丝不动。
3. `frontend/src/views/StudentChat.vue` 1263–1271 行的 `<el-input type="textarea">` 没有任何 `@focus` 处理，无 `scrollIntoView` 兜底（iPad Safari 不支持 interactive-widget，必须有这条兜底）。

**改动**：

1. `frontend/index.html` 第 5 行改为：
   ```html
   <meta name="viewport" content="width=device-width, initial-scale=1.0, interactive-widget=resizes-content" />
   ```
2. `frontend/src/views/StudentChat.vue`：
   - 给聊天输入框加 `ref="messageInputRef"` 和 `@focus="handleMessageInputFocus"`；
   - 新增 handler（键盘弹出动画需要时间，必须延迟）：
     ```ts
     function handleMessageInputFocus() {
       window.setTimeout(() => {
         const el = messageInputRef.value?.$el as HTMLElement | undefined
         el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
       }, 300)
     }
     ```
     （`el-input` 组件实例的根元素取法以 Element Plus 2.11 实际 API 为准，必要时用 `document.querySelector('.chat-message-input')` 兜底。）
3. `frontend/src/styles.css`：排查学生聊天页容器链（`.app-root--shell` → `.app-main` → `.student-page-grid` → `.chat-panel`），确保键盘弹出、`--app-viewport-height` 变小时**聊天面板实际收缩**：把学生页作用域内相关的 `min-height: var(--app-viewport-height)` 改为 `height: var(--app-viewport-height)`（或补 `max-height`）。**只动学生聊天页作用域的选择器**，不得影响 Login/Admin 等其他页面（styles.css 103 行已有一处 `height: var(...)`，先确认它挂在哪个选择器上，顺着现有模式改）。

**新增测试**（追加到 `tests/test_tablet_layout_frontend.py`）：

```python
def test_viewport_meta_declares_interactive_widget():
    html = Path("frontend/index.html").read_text()
    assert "interactive-widget=resizes-content" in html

def test_chat_input_scrolls_into_view_on_focus():
    source = Path("frontend/src/views/StudentChat.vue").read_text()
    assert "handleMessageInputFocus" in source
    assert "scrollIntoView" in source
```

**验收命令**：
```bash
.venv/bin/python -m pytest tests/test_tablet_layout_frontend.py -q
cd frontend && npm run build
```

**手工验收口径**：DevTools 设备模拟无法模拟软键盘，PR 描述中注明需真机验收：Android 平板 Chrome/WebView 聚焦输入框后，输入框与"发送问题"按钮完整可见；iPad Safari 同样验证（走 scrollIntoView 兜底路径）。

---

## 任务 C：限制 AI 单轮反问数量

**用户反馈原文**：#69"ai一次性提的问题太多了，可以设置它一次仅提出一两个问题引导吗？回答问题打字打的力竭了"。
**生产数据佐证**：38518 条 AI 回复中 34%（约 1.3 万条）含 ≥4 个问号，3455 条含 ≥8 个。

**已核实成因**：`backend/services/socratic_service.py` 20–25 行 `base_prompt` 只说"优先用问题引导学生思考"，整个 system prompt 组装（81–89 行 `system_sections`）没有任何单轮问题数量上限。

**改动**（注意 CLAUDE.md 约束：引导参数来自 `AgentConfig.guidance_params`，不要硬编码）：

1. `backend/services/socratic_service.py`：
   - `build_prompt()`（63 行起）新增可选参数 `guidance_params: dict | None = None`（追加在参数列表末尾，保持向后兼容）；
   - 在 `system_sections` 列表（81–89 行）追加一条：
     ```python
     max_questions = 2
     if guidance_params:
         raw = guidance_params.get("max_questions_per_turn")
         if isinstance(raw, int) and 1 <= raw <= 3:
             max_questions = raw
     system_sections.append(
         f"每次回复最多提出 {max_questions} 个引导问题，且必须聚焦同一个思考点；"
         "不要在一条回复中并列多个问题让学生逐一回答，需要追问时分多轮进行。"
     )
     ```
2. `backend/routers/chat.py`：1130 行附近已取 `active_config`，1136 行附近调用 `build_prompt` 处传入 `guidance_params=active_config.guidance_params if active_config else None`。**先全仓搜索 `build_prompt(` 的所有调用点**（chat.py 内可能有图片分支等多处），逐一传入；测试代码里的调用点靠默认值兼容，不强制改。
3. `backend/main.py` 42 行默认配置 `guidance_params={"fallback_after_turns": 3, "max_guidance_turns": 4}` 增加 `"max_questions_per_turn": 2`。
4. **不要**在这一批里动 `infer_stage()` 的阶段阈值、`infer_question_type()`、filter 规则 —— 那是第二批的范围。

**新增测试**（追加到 `tests/test_socratic.py`，先读该文件头部看现有 build_prompt 调用姿势）：

```python
def test_prompt_limits_questions_per_turn_default():
    package = socratic_service.build_prompt("什么是加速度", "物理", [], "", "")
    system_text = package.messages[0]["content"]
    assert "最多提出 2 个引导问题" in system_text

def test_prompt_limits_questions_per_turn_configurable():
    package = socratic_service.build_prompt(
        "什么是加速度", "物理", [], "", "",
        guidance_params={"max_questions_per_turn": 1},
    )
    assert "最多提出 1 个引导问题" in package.messages[0]["content"]
```
（`messages[0]` 是否为 system 消息、system_sections 如何拼接，以 `build_prompt` 实际实现为准调整断言方式。）

**验收命令**（socratic 改动必须过对抗集）：
```bash
.venv/bin/python -m pytest tests/test_socratic.py tests/test_filter.py tests/test_chat_stream.py -q
.venv/bin/python -m compileall backend
```

---

## 任务 D：消除消息前多出的空行

**用户反馈原文**：#75"每次发信息的时候，发出去都会隔了一行才是那个文字"；#63 同类。

**已核实成因**（两部分，注意第二部分尚未完全定位）：

1. **AI 消息首部空行 —— 已用生产库证实**：66%（25300/38518）的 assistant 消息内容以 `\n` 开头。链路：LLM 输出自带前导换行（`ThinkingContentFilter`，`backend/services/llm_service.py` 58 行起，剥掉 `<think>...</think>` 后残留其后的 `\n\n`）→ 前端 `richText.ts` 的 `collapseSoftLineBreaks()`（390–435 行）**保留首部空行**（405–408 行：空行会 push `''`，且没有在返回前剔除首尾空行）→ 段落间空行被渲染成空隙/`<br>` → 气泡顶部空一行。
2. **学生消息首部空行 —— 成因未定位**：生产库中 49406 条学生消息**零条**带首行空白，且发送前有 `form.message.trim()`（`StudentChat.vue` 991 行）。即库和发送链路都是干净的，学生看到的空行是**纯渲染层现象**，需先复现再修（见改动第 4 步）。

**改动**：

1. **后端流式输出侧剥前导空白**（主修复，让新数据从源头干净）：
   在 `backend/routers/chat.py` 的 SSE chunk 产出循环里（1563 行附近有对 `segments` 逐段校验/emit 的循环，以实际代码为准找到"往客户端 emit chunk"的位置），加"首个可见字符前抑制空白"状态：
   ```python
   emitted_visible = False  # 循环外初始化
   # 循环内，emit 前：
   if not emitted_visible:
       segment = segment.lstrip()
       if not segment:
           continue
       emitted_visible = True
   ```
   同时在**持久化 assistant Message 内容之前**（1630 行附近写 `conversation.guidance_stage` 的同一段落里找到 content 落库处）对最终文本做 `.strip()`。
   注意检查 `request_replay_service`（SSE 断连重放）缓存的内容与落库内容一致，避免重放出旧的带空行版本。
2. **前端渲染层兜底（同时治愈全部历史存量数据）**：`frontend/src/utils/richText.ts` 的 `collapseSoftLineBreaks()`（390 行）在 `return normalizedLines.join('\n')` 之前剔除首尾空行：
   ```ts
   while (normalizedLines[0] === '') normalizedLines.shift()
   while (normalizedLines[normalizedLines.length - 1] === '') normalizedLines.pop()
   ```
3. **前端流式拼接兜底**：`StudentChat.vue` 的 chunk 事件处理（1038–1044 行）中，assistant 气泡内容为空时对新 chunk 左 trim：
   ```ts
   if (event === 'chunk') {
     const last = messages.value[messages.value.length - 1]
     if (last && last.role === 'assistant' && typeof data.content === 'string') {
       last.content += last.content ? data.content : data.content.replace(/^\s+/, '')
       queueScrollToBottom()
     }
   }
   ```
4. **学生侧空行复现与定位**（必做，结论写进 PR）：本地起前端，发送"加速度是什么"，检查用户气泡渲染出的实际 DOM。候选成因：`.bubble-role` 标签（"学生"）与 `.message-body` 之间的间距在平板上被感知为空行；或历史会话加载路径的内容差异。**若复现**，按实际 DOM 定位修复；**若无法复现**，在 PR 里写明"学生侧按渲染层兜底（第 2 步）覆盖，未复现独立成因"，不要臆测性改动。

**新增测试**：

- `tests/test_llm_service.py` 或 `tests/test_chat_stream.py`（看哪个更贴近 SSE emit 层，跟随现有测试模式）：模拟 provider 输出 `"\n\n你好"` / `"<think>x</think>\n\n你好"`，断言：SSE 首个 chunk 不以空白开头；落库的 assistant content 不以 `\n` 开头。
- `tests/test_tablet_layout_frontend.py` 追加源码断言：
  ```python
  def test_rich_text_strips_leading_blank_lines():
      source = Path("frontend/src/utils/richText.ts").read_text()
      start = source.index("function collapseSoftLineBreaks")
      end = source.index("function renderMarkdownBlocks", start)
      block = source[start:end]
      assert "normalizedLines.shift()" in block
      assert "normalizedLines.pop()" in block
  ```

**验收命令**：
```bash
.venv/bin/python -m pytest tests/test_chat_stream.py tests/test_llm_service.py tests/test_tablet_layout_frontend.py -q
cd frontend && npm run build
```

---

## 完成后的整体验收与部署备注

1. 全量回归：
   ```bash
   .venv/bin/python -m pytest -q
   .venv/bin/python -m compileall backend tests locustfile.py
   cd frontend && npm run build
   ```
2. 部署（供人工操作参考，代理不执行）：后端/worker 挂载宿主机代码，`docker compose restart backend worker nginx` 即可；**前端改动需要重新构建前端镜像/产物**（styles.css、index.html、StudentChat.vue、richText.ts 均属前端构建物）。
3. 四个 commit 的 Lore trailer 中，`Tested:` 写实际跑过的命令，`Not-tested:` 必须如实写"真机软键盘行为未验证"（任务 B）和"学生侧空行未复现"（任务 D，如适用）。
