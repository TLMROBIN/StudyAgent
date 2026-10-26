# 平板输入、对话空间与照片反馈修复

## 已确认的问题

- 159.62 `/studyagent/` 和 `/api/` 网关路由没有 `client_max_body_size`，默认限制阻断照片。Nginx error.log 在 2026-10-26 21:54:01/04/06/07 与 21:56:18 记录反馈 POST 的 3460507 / 3862713 字节请求被拒绝，后端没有收到请求。
- 网页已有 visualViewport 高度同步，但未跟随焦点平移偏移；普通反馈页也没有在视口变化后把当前输入框滚入可见区。
- Android 工具箱壳在 edge-to-edge 模式下仅处理 systemBars，未处理 IME inset；Manifest 未声明 adjustResize。
- 回到最新按钮位于 flex 对话容器中，额外占用一整行。

## 修复

- 黑色侧栏提供历史、新问题、通知；学生默认折叠，仍可手动展开。通知按需抽屉显示。
- 对话区无外框，精简标题和输入区；回到最新为阅读区右下角 44px 按钮，不挤占对话高度。
- 网页适配 visualViewport.height / offsetTop，反馈页聚焦输入框随视口变化滚动。
- 反馈图片复用解码、缩放、压缩和大小校验；上传失败保留文字、图片，对 413 显示明确提示。
- 网关修复脚本 `scripts/fix_gateway_upload_limit.py`：只调整 StudyAgent 两个路由至 64m；备份、nginx -t 后 reload，异常恢复原文件。需要 159.62 的 sudo 权限，应用部署门禁不能替代此步骤。

## APK 配套改动

SSO 仓库 `android-toolbox/app/src/main/java/cn/yubo/toolbox/MainActivity.kt`：底部 padding 改为 maxOf(systemBars.bottom, ime.bottom)。
`android-toolbox/app/src/main/AndroidManifest.xml`：MainActivity 加 `android:windowSoftInputMode="adjustResize"`。
保留 SSO 原有未提交工作，本轮不代为提交其其他改动。

已用 Android Studio JBR + Android SDK 构建 `:app:assembleDebug`。本地安装包位于 StudyAgent `artifacts/tablet-repair-20261026/yubo-toolbox-keyboard-fix.apk`，仍为工具箱原包名与版本，需安装到平板后生效；不覆盖旧包名的 StudyAgentAPP。没有连接真机，未声称原生/QQ 输入法真机验收。

## 验证边界

- 前端 Node 测试 16 个文件通过，含视口缩小、偏移、恢复、监听器清理的行为测试；反馈后端 7 项通过；compileall 与生产 Vite 构建通过。
- 浏览器本地模拟学生数据：1024×768 阅读区 582px；1024×340 输入底边 280px；反馈输入框底边 315px，在 340px 视口内。截图位于本机 artifacts/tablet-repair-20261026。
- 图片选择测试使用问题报告附图，处理成 JPEG 并显示预览；没有在生产环境替学生提交测试反馈。
- TypeScript 全量检查仍有现有 subjectPieChart 和依赖声明错误，不能报告全量类型检查通过。
- 部署运行证据见本轮脚本输出；网关 sudo 执行和新版 APK 真机安装为独立验收步骤。
