# Alembic 迁移风险清单

> 生成于 2026-07-03，由 `tests/test_alembic_migrations.py` 对临时 SQLite 空库实测得出。
> 该测试将下列清单固化为断言：新增/修复迁移后清单变化会导致测试失败，请同步更新两处。

## 风险 1（高）：全新空库无法直接 `alembic upgrade head`

`20260401_0001` 的 `upgrade()` 是 `Base.metadata.create_all(bind)`，它按**当前代码模型**建出全量
schema（包含后续迁移才应新增的表和列），而不是 2026-04-01 时点的 schema。因此在空库上按链执行时，
以下 12 个 revision 会因"对象已存在"而失败：

| Revision | 失败原因 |
|---|---|
| `20260412_0002` | duplicate column `knowledge_chunks.is_disabled` |
| `20260413_0003` | table `chat_message_attachments` already exists |
| `20260414_0004` | table `llm_provider_configs` already exists |
| `20260523_0005` | duplicate column `conversations.deleted_by_student_at` |
| `20260523_0006` | duplicate column `messages.llm_model_key` |
| `20260523_0007` | table `llm_quota_policies` / `llm_usage_events` already exists |
| `20260525_0008` | table `notifications` already exists |
| `20260528_0009` | duplicate column `llm_model_configs.vision_understanding_priority` |
| `20260618_0010` | table `student_feedback` / `student_feedback_bans` already exists |
| `20260621_0011` | duplicate column `messages.assets` |
| `20260621_0012` | table `student_error_events` / `student_skill_profiles` already exists |
| `20260621_0013` | table `release_notes` / `release_note_read_states` already exists |

`20260621_0014`、`20260622_0015`、`20260701_0016` 因带 inspector 守卫（先检查表/列是否存在）可正常通过。

**影响**：新环境部署无法用 alembic 初始化数据库；目前实际依赖 `backend/main.py` 启动时的
`Base.metadata.create_all + apply_runtime_schema_updates()` 兜底，alembic 版本表与真实 schema
可能长期不一致（漂移）。

**建议**：
- 短期：新环境初始化用「启动应用建表 + `alembic stamp head`」，不要跑 `upgrade head`；
- 长期：把 0001 改写为写死的 2026-04-01 时点 schema（不引用 `Base.metadata`），或给 0002–0013
  全部补上与 0014–0016 相同的 inspector 守卫。

## 风险 2（中）：不可逆迁移

| Revision | 说明 |
|---|---|
| `20260621_0014` | `downgrade()` 为 `pass`（repair-only 迁移，设计上不可逆）。回退跨过该版本时 `messages.assets` 列不会被移除——实际无害，因为该列本属于 0011。 |

## 风险 3（中）：`20260401_0001` 的 downgrade 是 `drop_all`

`downgrade()` 为 `Base.metadata.drop_all(bind)`，会按**当前模型**把所有表（包括本应属于后续迁移
的表）一次性删光。若在中间版本（如 0005）误执行 `downgrade base`，0001 会连 0006+ 的表也一起删掉
（如果存在）。回退到 base 等价于清库，执行前必须先备份。

## 实测结论（SQLite）

- head → base 逐步 downgrade：**全部执行成功**（含 0014 的 no-op）；base 状态仅剩 `alembic_version` 表。
- head → 回退一步 → 再 upgrade（0016 往返）：正常。
- 迁移链线性：单 head、单根、无分叉、无 merge revision。

注意：以上在 SQLite 验证；Postgres 上 `ALTER TABLE DROP COLUMN`、索引命名等行为可能有差异，
上线回滚前应在与生产一致的 Postgres 中演练。
