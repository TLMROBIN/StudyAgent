# 学生反馈修复：第一、第二批（2026-10-25）

## 行为变更

- 模型流异常或空回复明确显示“模型调用失败，请联系管理员老师或稍后再试。”，不保存为导师回答、不缓存、不发放积分。已输出部分文字后失败不拼接另一模型回答；尚未输出时仍可调用真实备用模型。教学阶段 fallback_walkthrough 与输出安全校验保持不变。
- 图片识别服务失败单独返回 502；低置信度但识别服务成功时仍允许说明图片质量问题。
- 流式界面仅在用户靠近底部时跟随，向上翻阅保持位置；提供回到最新回复。停止立即恢复输入，隔离旧请求回调，取消退避重试；图片识别断连取消协程，PaddleOCR 独占请求锁的工作进程可终止，排队取消不影响其他学生。
- 显式引入 ElMessageBox 样式，限制确认框宽度。
- 统一改密入口使用原有 OIDC PKCE/state 流程及 Keycloak UPDATE_PASSWORD 动作；OIDC 开启时禁止本地改密接口写入不同步密码。
- 持久化会话置顶，仅本人且未删除的会话可以操作。最后交流时间由消息插入事件更新，排序为置顶、最后交流时间、ID。管理操作不改变最后交流时间。
- 页内鉴权图片预览、放大缩小、失败重试，不依赖弹窗。
- 答疑与意见反馈均提供不限扩展名的文件选择入口，但按内容签名及浏览器实际解码验证图片。PNG/JPEG/GIF/WebP 即使名为 .pho 也可识别；无法解码的专有格式明确拒绝。保留浏览器可解码 HEIC 的原有路径。

## 本地验证

- 636 项 pytest 全量通过（CHAT_IMAGE_OCR_TIMEOUT_SECONDS=45，与既有测试基线一致）；14 个前端测试文件通过，含真实 ReadableStream 的错误/取消/重试测试。
- Vite 生产构建、compileall 和 git diff --check 通过。
- tsc --noEmit 仍报告既有第三方类型、subjectPieChart、viewportHeight 错误；对 HEAD 创建独立源码基线比较，标准化路径后诊断完全相同，本次未新增诊断。不能称完整 TypeScript 检查通过。
- 原生浏览器在隔离的本地模拟 API 环境验证：置顶顺序与刷新、确认框（420px 白色背景）、页内图片放大、模型失败提示与草稿、停止即时恢复输入、PNG 内容的 .pho 文件转成 JPEG。
- 滚动实测：继续生成时 scrollHeight 从 2669 增至 2913，用户上翻后的 scrollTop 保持 518.5。

## 发布与迁移

- 新迁移 20261025_0022：增加 is_pinned、last_message_at，并根据历史消息 MAX(created_at) 回填。兼容启动时 runtime schema 已添加字段的情况；不改写历史消息正文。
- 发布前使用 SQLite backup API 备份，在备份副本上演练升级并校验旧列数据、integrity_check、foreign_key_check。
- 后端及 worker 只需 restart；前端必须重新构建 nginx bundle。
- 发布后运行 REQUIRE_REMOTE_HEAD_MATCH=1 bash scripts/studyagent_deploy_verify.sh；另核验统一入口的脚本/CSS、OIDC 改密重定向和新 API。
- 回滚优先恢复旧应用版本，新增字段可保留；不得用旧库覆盖发布后的新学生消息。

## 保留的验收边界

旧平板/手心输入法、实际专有 .pho 样本属于第三批，尚未真机验收。模拟故障浏览器测试不代表真实模型故障注入；不为验证而破坏线上模型配置。未操作任何学生的真实密码。
