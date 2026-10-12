# 学生深度推理操作

Mode: Operate。扩展既有学生答疑页面，保留 PRODUCT.md 与 DESIGN.md 的颜色、字体、触控尺度及单任务布局。

## Direction contract

THESIS：学生按题目复杂程度自主选择推理深度，并能看到等待状态。分析不能提前泄露标准解。

OWN-WORLD：复用现有表单标签、次级蓝灰、44px 触控目标和原生 details；不添加额外卡片、图标或装饰。

STORY：默认快速答疑；复杂题可勾选深度推理，旁边说明等待更久。助手气泡中的推理区域默认折叠，学生主动展开；显示简洁实时进度以及受安全校验的公开学习思路摘要，继续追问由导师正文承载。不转发内部推理原文。

FIRST VIEWPORT：开关紧邻输入框的发送操作区。折叠行位于助手回复标签与正文之间。展开区域高度最多 140px，可滚动，不挤满对话框。

FORM：既有表面内的窄扩展，不进行概念或视觉身份替换。

FINISH：unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance。

## 完成验证（2026-10-12）

本机 Chrome API fixture 在 1280×900、768×1024、390×844 三视口完成折叠与展开渲染检查，无横向溢出。推理区默认折叠，开关与折叠行触控高度至少 44px，展开正文最多 140px 并允许滚动。原生浏览器控制超时；以上证据来自 Chrome fixture，评审 disposition 为 ship，仅覆盖该本机界面范围，不代表线上真实模型或真机平板验收。复用既有颜色、字体和单任务布局，PRODUCT.md、DESIGN.md 与 .impeccable/design.json 保持不变。
