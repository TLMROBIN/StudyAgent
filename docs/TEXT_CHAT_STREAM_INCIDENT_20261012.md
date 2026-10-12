# 2026-10-12 文字答疑空回复与模型延迟

## 故障与修复

庄锐生当日 7 次纯文字问答全部返回空回复兜底，3 次带图问答通过 OCR + 本地固定引导显示内容。全校同期 20 条助手记录中 14 条为空回复兜底。

`asyncio.wait_for(anext(llm_stream), 15)` 的超时会取消正在生成的异步任务，关闭模型流，且 CancelledError 绕过模型备用链。真实容器请求复现：15 秒超时后流直接结束；不取消等待则 25.64 秒收到首字。

改为持有一个 pending read task，通过 `asyncio.wait` 发送心跳并继续等待同一个 task；断连、外部取消、流结束时清理 task 与生成器。回归覆盖慢首字、后续片段等待、断连清理和消息持久化。

## 延迟拆解

服务器真实模型流测试，统一短物理引导 Prompt，输出预算 800 token。结果为单次样本，不能作为 P95 或稳定 SLA。

| 模型与参数 | 首事件 | 正文首字 | 完整流 | 隐藏推理 token |
|---|---:|---:|---:|---:|
| 302AI GLM-5.3-flash 默认 | 29.02 秒 | 46.92 秒 | 49.13 秒 | 691 |
| 同路由 thinking disabled | 1.04 秒 | 1.04 秒 | 3.65 秒 | 0 |
| 302AI DeepSeek-v4-flash 默认 | 1.82 秒 | 无正文 | 8.45 秒 | 800，预算耗尽 |
| 同路由 thinking disabled | 19.29 秒 | 19.30 秒 | 19.66 秒 | 0 |

主要原因是隐藏推理在可见输出前消耗时间和输出预算，同时供应商首事件延迟仍有波动。关闭推理不能消除上游排队或网络波动。未获得供应商内部排队指标，不能进一步断言具体上游原因。

文本请求仅在已测路由添加 `thinking: {type: disabled}`：302AI + glm-5.3-flash，以及 deepseek-v4-flash。视觉请求不添加该参数。GLM 官方文档称 5.3-FLASH 强制思考，不能关闭；302AI 当前路由实测接受该参数并返回零推理 token，因此不推广到其他 GLM 供应商。DeepSeek 官方支持该开关。应持续观察回答质量；速度样本不代表科学正确性全面验证。

- GLM 文档：https://docs.z.ai/guides/capabilities/thinking-mode
- DeepSeek 文档：https://api-docs.deepseek.com/guides/thinking_mode/

## 检索与其他等待

服务器不存在 BGE-M3 本地缓存，Hugging Face 连接超时。原实现每次检索重新尝试加载，且同请求出现多次失败。修复为进程内只尝试一次，保留既有 hash fallback；恢复模型文件后需重启进程。不声称已恢复语义 embedding，需要另行补齐模型文件并验证检索质量。

建议回复调用在 done 之后执行，超时会延后消息保存，但不是主回答首字等待的原因。现有 2 秒超时保留。

## 验证口径

测试与部署证据见当次交付。真实端到端验证只使用既有压测学生，不重放或修改庄锐生的会话。部署必须运行 `REQUIRE_REMOTE_HEAD_MATCH=1 bash scripts/studyagent_deploy_verify.sh` 并核对 HEAD、服务和 HTTP 状态。
