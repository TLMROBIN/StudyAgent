---
name: "StudyAgent"
description: "面向高中生自主答疑的耐心、可信、触控优先学习界面"
colors:
  primary: "#0f766e"
  primary-vivid: "#0f9c90"
  primary-action: "#108a7f"
  secondary: "#db6b2c"
  neutral-bg: "#f2efe6"
  study-bg: "#f4f7f6"
  study-surface: "#ffffff"
  assistant-surface: "#f7f9f8"
  surface: "#fffaf0"
  surface-panel: "#fffcf6d1"
  surface-panel-strong: "#fff8eaf5"
  ink: "#18212f"
  muted: "#617287"
  line: "#18212f1f"
  dark-surface: "#13212deb"
  on-dark: "#fff8ed"
  danger: "#dc2626"
  danger-deep: "#b91c1c"
  success-soft: "#d7efdc"
typography:
  display:
    fontFamily: "IBM Plex Sans, Noto Sans SC, sans-serif"
    fontSize: "34px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.03em"
  headline:
    fontFamily: "IBM Plex Sans, Noto Sans SC, sans-serif"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-0.03em"
  title:
    fontFamily: "Noto Sans SC, IBM Plex Sans, sans-serif"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: 1.35
  body:
    fontFamily: "Noto Sans SC, IBM Plex Sans, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.7
  label:
    fontFamily: "Noto Sans SC, IBM Plex Sans, sans-serif"
    fontSize: "13px"
    fontWeight: 700
    lineHeight: 1.35
  mono:
    fontFamily: "IBM Plex Mono, SFMono-Regular, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.55
rounded:
  control: "8px"
  prompt: "12px"
  bubble: "14px"
  study-surface: "16px"
  note: "16px"
  row: "18px"
  panel: "28px"
  feature: "32px"
  pill: "999px"
spacing:
  xs: "6px"
  sm: "8px"
  md: "12px"
  control: "14px"
  lg: "18px"
  panel: "22px"
  page: "28px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-dark}"
    typography: "{typography.body}"
    rounded: "{rounded.pill}"
    padding: "12px 18px"
    height: "48px"
  button-ghost:
    backgroundColor: "{colors.line}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.pill}"
    padding: "12px 18px"
  input:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.control}"
    padding: "11px 12px"
    height: "44px"
  panel:
    backgroundColor: "{colors.surface-panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.panel}"
    padding: "22px"
  nav-active:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-dark}"
    typography: "{typography.body}"
    rounded: "{rounded.row}"
    padding: "14px 16px"
  suggested-reply:
    backgroundColor: "#0f766e0e"
    textColor: "{colors.primary}"
    typography: "{typography.label}"
    rounded: "{rounded.prompt}"
    padding: "9px 13px"
  chat-user:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.on-dark}"
    typography: "{typography.body}"
    rounded: "{rounded.bubble}"
    padding: "16px 18px"
  chat-assistant:
    backgroundColor: "{colors.assistant-surface}"
    textColor: "{colors.ink}"
    typography: "{typography.body}"
    rounded: "{rounded.bubble}"
    padding: "16px 18px"
---

# Design System: StudyAgent

## Overview

**Creative North Star: "安静的解题书桌"**

StudyAgent 的视觉系统像一张在课后安静展开的解题书桌：学生一坐下就知道从哪里开始，注意力自然落在问题、思路和下一步，而不是装饰或技术本身。整体气质平静、清晰、可信，以触控友好的操作尺度支持电脑和平板上的连续思考。学生答疑页关闭环境柔光和后台式多栏，把空间留给题目、思路与下一步。

系统采用温和浅色环境、深墨蓝黑结构面与沉静松绿操作色。柔光负责营造专注感，清楚的层级和状态负责建立信任；校订橙只在提示、眉题和需要注意的内容中出现。界面可以温和，但不能低幼；可以有层次，但不能像培训机构营销页或冰冷企业后台。

**Key Characteristics:**

- 触控优先，关键操作目标以 44–48px 为基线。
- 学生答疑界面宽松清晰，教师与管理界面允许更高信息密度。
- 学生答疑进入单任务模式：实色学习底、单一聊天表面、按需历史抽屉。
- 沉静松绿只用于主要行动、当前选择和积极状态。
- 环境柔光建立氛围，边界与间距建立结构。
- 动效只解释状态变化，并尊重减少动态效果设置。

## Colors

这是一套由沉静松绿、校订橙、深墨蓝黑和柔和象牙白组成的克制学习色板。

### Primary

- **沉静松绿** (#0f766e): 主要行动、当前导航、学生消息和积极状态的核心颜色。
- **明亮松绿** (#0f9c90): 仅作为主要渐变的亮端，帮助大面积青绿保持层次。
- **行动松绿** (#108a7f): 主要按钮渐变的终点，不作为新的独立强调色扩散使用。

### Secondary

- **校订橙** (#db6b2c): 眉题、提醒和需要复核的信息；不与主色争夺主要行动权。

### Neutral

- **柔和象牙白** (#fffaf0): 助手消息、阅读表面和浅色内容底。
- **学习底色** (#f2efe6): 页面环境底色，与浅色表面形成轻微层级。
- **答疑学习底** (#f4f7f6): 学生单任务答疑页的安静实色环境。
- **答疑工作面** (#ffffff): 学生会话、输入与历史抽屉的清晰主表面。
- **助手消息面** (#f7f9f8): 助手引导的低对比阅读表面。
- **半透明面板** (#fffcf6d1): 通用面板，允许环境柔光轻微透出。
- **强调面板** (#fff8eaf5): 指标块和需要更强分组的浅色表面。
- **深墨蓝黑** (#18212f): 正文、标题和主要图形。
- **深色结构面** (#13212deb): 登录主叙事区和需要稳定锚点的深色区域。
- **次级蓝灰** (#617287): 辅助文字、元数据和非主导标签。
- **轻墨分隔线** (#18212f1f): 边界、分隔与低强调描边。
- **暗面文字** (#fff8ed): 深色结构面上的正文与标题。

### Semantic

- **错误红** (#dc2626): 删除、失败和明确危险动作。
- **深错误红** (#b91c1c): 错误动作的悬停或聚焦状态。
- **成功浅绿** (#d7efdc): 建议回复和完成状态的轻量底色。

### Named Rules

**The One-Green Voice Rule.** 沉静松绿是唯一主要操作色；明亮松绿和行动松绿只能作为同一颜色家族的层次，不得发展成第二套主色。

**The Orange Annotation Rule.** 校订橙用于提醒和批注语义，不用于主要提交按钮，也不用于大面积背景。

## Typography

**Display Font:** IBM Plex Sans（回退到 Noto Sans SC 与 sans-serif）  
**Body Font:** Noto Sans SC（回退到 IBM Plex Sans 与 sans-serif）  
**Label/Mono Font:** IBM Plex Mono（回退到 SFMono-Regular 与 monospace）

**Character:** 一套清楚、稳定、不炫技的无衬线系统。中文正文以可读性为先，IBM Plex 负责标题的结构感和代码/技术内容的辨识度。

### Hierarchy

- **Display** (700, 34px, 1.2, -0.03em): 登录页核心标题和少量高层级叙事标题。
- **Headline** (700, 24px, 1.25, -0.03em): 页面与面板标题；低高度平板场景可降到 20px。
- **Title** (700, 20px, 1.35): 分组标题、对话标题和重要卡片标题。
- **Body** (400, 16px, 1.7): 学生对话、说明文本和主要阅读内容；连续说明控制在约 65–75ch。
- **Label** (700, 13px, 1.35): 状态、操作标签与建议回复；12px 只用于元数据和眉题。
- **Mono** (400, 13px, 1.55): 代码块、配置值和需要等宽对齐的技术内容。

### Named Rules

**The Calm Type Rule.** 产品界面只使用这一套无衬线家族；标题最紧不得超过 -0.03em，按钮和正文不使用展示字体或夸张字距。

## Elevation

StudyAgent 使用环境式分层：背景的青绿与橙色柔光建立空间气氛，面板主要靠色差、边界和间距分组，较大的阴影只用于页面级或高关注表面。普通行、卡片和聊天气泡保持低浮起感，避免整页漂浮。

### Shadow Vocabulary

- **环境面板** (`0 24px 60px rgba(24, 33, 47, 0.12)`): 登录区和顶层面板的柔和环境阴影。
- **确认浮层** (`0 20px 50px rgba(24, 33, 47, 0.18)`): 删除确认等必须脱离页面流的浮层。
- **输入焦点** (`0 0 0 3px rgba(15, 118, 110, 0.10)`): 输入框与可交互控件的清晰焦点环。
- **答疑工作面** (`0 8px 24px rgba(24, 33, 47, 0.06)`): 仅用于把全屏学习表面从实色环境中轻轻分离。

### Named Rules

**The Ambient Layer Rule.** 阴影只说明层级，不作为每张卡片的装饰；同一元素不要同时使用宽大阴影和高对比描边。

## Components

组件的共同手感是触控友好、克制明确：圆角帮助识别可操作区域，状态变化清楚但不喧闹。

### Buttons

- **Shape:** 主要与次要动作使用全圆角胶囊形；标准内边距 12px 18px，关键提交按钮最小高度 48px。
- **Primary:** 沉静松绿到行动松绿的 135° 轻渐变，白色文字，无装饰阴影。
- **Hover / Focus:** 悬停保持同一色相并轻微提亮；键盘焦点使用 3px 松绿焦点环，触控按下用轻微明度变化反馈。
- **Ghost / Danger:** Ghost 使用轻墨 8% 底色；Danger 使用错误红，并保持明确文字标签。

### Chips

- **Style:** 建议回复为极浅松绿底、松绿字、1px 实线和 12px 圆角，最小高度 44px，内边距 9px 13px。
- **State:** 悬停与焦点提高边界和底色对比；禁用态降低透明度，但仍保留可读文字。

### Cards / Containers

- **Corner Style:** 学生答疑工作面 16px、消息气泡 14px、建议回复 12px；通用后台面板仍可使用 28px，32px 只保留给登录页特征表面。
- **Background:** 普通内容使用半透明浅面板或柔和象牙白，深色表面只用于结构锚点。
- **Shadow Strategy:** 普通卡片无阴影；顶层面板按 Elevation 使用环境阴影。
- **Border:** 1px 轻墨分隔线，只用于界定结构。
- **Internal Padding:** 普通行 14px 16px，卡片 18px，顶层面板 22px。

### Inputs / Fields

- **Style:** 柔和白色底、1px 轻墨边界、8px 圆角；新触控输入目标高度至少 44px。
- **Focus:** 边界切换为 55% 松绿，并增加 3px、10% 松绿焦点环。
- **Error / Disabled:** 错误同时显示文字与红色状态；禁用态降低透明度并保留标签，不只依赖颜色。

### Navigation

- **Style:** 深墨侧栏承载角色导航，默认项使用 8% 白色底；学生当前任务使用纯松绿，不混入校订橙。
- **Typography:** 16px 正文标签配 12px 高权重短标签；收起时保留可识别短标签和完整 title/aria-label。
- **Responsive:** 侧栏宽度在 288px、224px、188px 间适配，并提供 92px、76px、68px 的收起状态。

### Chat Bubbles

- **Student:** 纯松绿、白色文字、14px 圆角和 16px 18px 内边距，靠右对齐。
- **Assistant:** #f7f9f8、深墨文字、轻墨边界与 14px 圆角，靠左对齐。
- **Reading:** 行高 1.7，最大宽度为 78% 或 720px；公式和长内容允许水平滚动，不裁切。

### Completion and Reflection

- **Explicit outcomes:** “取消”只关闭流程且不写入完成状态；“跳过反思并完成”与“提交反思并完成”必须是两个独立动作。
- **Reversible state:** 已完成会话始终提供“恢复继续思考”，让学生能够纠正误操作或重新进入问题。
- **Failure safety:** 保存失败时保留学生输入；保存成功但列表刷新失败时，必须明确说明状态已经保存，不能反向报告为提交失败。

### Student Session Resilience

- **Capability honesty:** 学科列表由后端返回，并明确区分“校本资料与题库可用”“校本资料可用”和“通用答疑”，不能让可选学科暗示能力完全相同。
- **Draft continuity:** 未发送文字按学生、学科与会话保存在当前标签页；发送失败恢复输入，发送成功或主动停止后清理草稿。
- **Stable recovery copy:** 技术错误只进入日志；学生端使用稳定中文说明发生了什么、下一步能做什么，并明确草稿是否保留。
- **Single announcement channel:** 流式分块不逐字触发屏幕阅读器；开始、完成、停止和失败通过一个原子 live region 播报。
- **Progressive session tools:** 学生会话顶栏最多同时呈现四项操作；引导设置、修改密码与情境化完成动作收进“更多”。

### Panels and Data

- **Student surfaces:** 留出阅读与触控空间，用对话、建议回复和明确下一步维持思考节奏。
- **Teacher/admin surfaces:** 可使用指标块、表格行和图表，但仍沿用相同颜色、圆角和状态语法。

## Do's and Don'ts

### Do:

- **Do** 让主要触控操作达到 44–48px，并在平板横屏和低高度视口中验证。
- **Do** 用 #0f766e 表示主要行动、当前选择与积极状态，用 #db6b2c 表示提醒和批注。
- **Do** 保持学生消息与助手消息的左右位置、颜色和语义一致。
- **Do** 用文字、图标或形状补充成功、错误和选中状态，不只依赖颜色。
- **Do** 让公式、长题干、表格和图片在缩放后仍可阅读或滚动查看。
- **Do** 尊重减少动态效果设置，让通知滚动等运动能够停止或降级。
- **Do** 让系统自动选择答疑服务，把历史、角色等次要决策放进按需入口。

### Don't:

- **Don't** 把界面做成培训机构营销页：不要用夸张口号、强推销感或“提分神器”制造焦虑。
- **Don't** 做低幼游戏化：不要堆叠卡通元素或过度奖励动画，不让高中生感觉被当成小学生。
- **Don't** 让学生端像冰冷企业后台：不要让密集表格和技术术语主导答疑体验。
- **Don't** 用渐变文字、彩色侧边条或重复装饰卡片制造视觉层级。
- **Don't** 为普通行和卡片添加宽大阴影；阴影只服务于真实层级。
- **Don't** 扩散新的主色或让校订橙承担主要提交动作。
- **Don't** 在学生主流程中暴露模型名、额度或内部引导阶段。
