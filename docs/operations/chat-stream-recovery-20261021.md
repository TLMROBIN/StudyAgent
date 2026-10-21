# 2026-10-21 答疑连接池修复与运行恢复

## 故障与代码修复

线上模型 HTTP 连接池耗尽，主备模型各等待 30 秒后才返回固定引导。旧代码提前结束外层异步生成器时，未逐层关闭内层生成器；断连取消也可能打断底层 HTTP 响应关闭。新增确定性关闭及取消保护，在安全改写后先关闭模型流再发送 done 和生成候选回复。取消路径不计入完整回答耗时。

回归包含 12 种分层提前关闭/取消组合、真实单连接 TCP 池连续 8 次提前停止，以及安全改写在 done 前释放连接。定向 114 项通过；全量 619 项通过；compileall 通过。本机 `.env` 的 `CHAT_IMAGE_OCR_TIMEOUT_SECONDS=0` 会使一个既有 OCR 测试立即超时；全量使用 `CHAT_IMAGE_OCR_TIMEOUT_SECONDS=45 .venv/bin/python -m pytest -q`，未改本机或生产 OCR 配置。

## 159.63 机器上的 GPU 启动恢复

本节记录机器专属状态，不适用于其他机器。重启 backend/worker 时发现既有 GPU 环境问题：CDI 规范引用的 `/run/nvidia-persistenced/socket` 已不存在，且宿主用户态库 580.178.04 与已加载内核驱动 580.173.02 不一致。普通 restart 和按原配置 recreate 均无法启动。

没有重启宿主机、改宿主驱动或修改其他 GPU 服务。采取应用范围内恢复：

- 保存原容器快照：`studyagent-backend:pre-recreate-20261021`、`studyagent-worker:pre-recreate-20261021`。代码仍由项目目录挂载，以 Git HEAD 为准。
- 从仍正常运行的 `vllm-qwen-chat` 读取匹配内核驱动的 NVIDIA 用户态文件，保存至项目 `.codex-backups/gpu-driver-580.173.02/`。只读取该服务，未重启或修改它。
- 为 backend/worker 使用 `runc`、`gpus: !reset []`、`NVIDIA_VISIBLE_DEVICES=void`，显式映射现有 GPU 设备及所需驱动文件。保留原业务环境和所有数据卷。
- 将机器专属设置保存至服务器已有且被 Git 忽略的 `docker-compose.override.yml`。原文件备份为 `.codex-backups/docker-compose.override.before-gpu-recovery-20261021.yml`。
- 独立容器验证 CUDA 两设备可见、cuda:1 张量计算成功；服务恢复后 health 确认 bge-m3 在 cuda:1 加载、PDF GPU preflight ready。重载 Nginx 使其解析重建后的后端地址。

**上述驱动目录和镜像是当前部署依赖，不可当作普通临时备份清理。** 后续维护宿主 NVIDIA 驱动时，应先统一内核与用户态版本、重建 CDI 规范、在独立容器验证，再移除该机器的局部恢复配置。直接恢复旧 override 会再次遇到启动故障。不得仅凭 `nvidia-smi` 文件存在判断 GPU 能用。

再次部署前先检查解析后的 Compose GPU 配置，并以独立容器验证当前驱动路径；避免先停止唯一在用的服务再发现启动故障。

## 证据口径

发布需运行 `REQUIRE_REMOTE_HEAD_MATCH=1 bash scripts/studyagent_deploy_verify.sh`，并另行通过学校 `/studyagent/api/chat/stream` 检查真实模型回复及停止后的连接释放。health/Git 一致不能替代模型验证；API 测量也不代表学生平板视觉验收。

本次使用既有“压测学生”测试，不代替庄锐生发送请求，也不补造或删除其历史消息。时间统计和测试输出保存在操作端 `artifacts/latency-audit-20261021/`。
