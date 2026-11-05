# 会话连续性与题图修复验收

## 改动

- 滚动上下文改为最近 **8 条消息**（不是 8 轮）；额外保留受长度限制的学生原始任务及最近补充/纠正，明确作为数据处理。
- 提示模型区分整体任务与局部练习，学生纠正时回查原材料，不能把模型假设当学生经历，不能声称学生从未提供当前窗口之外的材料；仍保留最终步骤由学生完成的约束。
- 新图片识别摘要、置信度和不确定处持久化到既有 JSON 字段；文字追问恢复首图及最近题图材料。旧附件缺少记录时按需识别最近题图并保存，失败不阻断文字追问。历史结构化材料不注入印刷参考答案。
- 按账号保存上次会话指针，恢复后台请求/草稿后再恢复上次会话；主动新建保存空指针，归档当前会话清除指针。
- 发送后把本地图片地址替换为鉴权附件地址；重进时重新取图；图片加载/解码失败提供重试。已卸载页面的异步加载不再创建新 blob。
- 可解码且不超过 8MB 的普通图片保留原始字节；超限图片仍压缩，HEIC 仍按原兼容路径转换。原比例完整预览、主动放大滚动及独立底部关闭按钮，首问提示发送前检查完整题图。
- 工具箱及独立 Android 壳升级 1.0.3 / versionCode 4：对外层 FrameLayout 应用系统栏、刘海、键盘 Insets，约束 WebView 实际布局范围。

## 自动验证

- 定向：`tests/test_filter.py tests/test_socratic.py tests/test_chat_stream.py`，165 passed。
- 新增：原题超出窗口时仍保留、精确八条历史、任务补充/纠正与长度限制、作文短回答沿用整篇评析目标、图片识别置信度与参考答案隔离、图片追问重用识别结果且不重复 OCR、账号指针隔离/主动新建/存储不可用。
- 前端：从仓库根目录运行 `node --experimental-strip-types --test frontend/tests/*.test.ts`，18 个文件通过。
- Vite 生产构建、Python compileall、git diff --check 通过。
- 全量后端：684 passed / 2 failed。对修改前 f08b59b 的独立 worktree 重跑这两项，同样失败：`test_understand_uses_paddleocr_backend_before_llm`、`test_chat_entrypoints_reject_unsupported_subject_before_side_effects`。未将既有 OCR mock/权限前置校验问题扩展进本次修复。

## 浏览器实际交互

本机生产构建 + 隔离模拟 API，测试用户和作文/图片均为合成数据，没有向学生记录写入测试内容。

- 从答疑切到意见反馈再返回：原题和消息恢复，图片成功重新鉴权加载，blob 地址已重新创建。
- 刷新：原会话和图片恢复。
- 主动新问题再刷新：保持新问题，不重新打开旧会话。
- 600×1000 测试图：上传预处理前后字节逐个相同；预览比例 0.6，四角边框完整。
- 1024×600 平板及 1440×900 桌面：默认完整题图，底部关闭按钮可见且可点击。
- 截图：`/tmp/studyagent-preview-tablet-final.png`、`/tmp/studyagent-preview-desktop-final.png`。测试浏览器空间已关闭。

## Android 验证与安装包

使用 android-emulator-qa 技能及 SSO 项目既有验收脚本。两份 Debug APK 构建成功，API30 工具箱覆盖安装成功，10 项原生选择器回归通过；不清除应用数据，不操作 USB 真机。

- AVD StudyAgent_M9_API30 / emulator-5580，1920×1130，Android 11 / WebView 83。
- 实际进入工具箱首页、点击统一认证，UI 树显示 WebView 位于 `[0,24][1920,1130]`，顶部状态栏区域已排除；本模拟器没有显示底部导航栏，不能据此判定 M9 三键导航验收通过。
- 统一登录页在旧 WebView 下原有样式缺失/对比度问题仍可复现。本轮未修改认证服务或主题；未完成登录后的真实 Android 业务全流程。
- 证据：`/Users/binyu/Projects/SSO/logs/acceptance/20261105-083211/`；本地 UI 树 `/tmp/studyagent-insets-qa.xml`、`/tmp/studyagent-login-qa.xml`。
- 安装包：`/Users/binyu/Projects/SSO/android-toolbox/releases/20261105/`。原生避让修复需要安装新版 APK；网页更新不能替代升级。
- 工具箱 SHA-256：`63953c4598cf5483dd43c0eb9945fb0b8b5f50a2aa844ddd138db21265b0f81e`。
- 独立壳 SHA-256：`81a404438d4ee689700af022347097677c7d4a85a31e025f9ae08c0a70bb0d2e`。
- 下载路径分别为 `/platform/downloads/yubo-toolbox-1.0.3-system-bars-20261105.apk`、`/platform/downloads/studyagent-1.0.3-system-bars-20261105.apk`，保留旧包。
- SSO 工作区原有多个未提交改动；未把这些已有改动混入 StudyAgent 提交。独立壳本身不在 Git 仓库中。

## 剩余边界

M9 厂商相机/实际 .pho、三键导航、真机键盘，以及真实 LLM 的长作文连续教学效果仍需抽测；提示与构造回归通过不等于保证所有模型输出永远不漂移。此次不创建跨会话学生画像，也不改写既有聊天文本。
