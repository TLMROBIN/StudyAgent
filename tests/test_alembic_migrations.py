"""Alembic 迁移升级/回滚测试（针对临时 SQLite 数据库）。

运行方式（最小依赖：alembic + sqlalchemy + pydantic-settings + pytest；
需要 Python >= 3.11，因为 backend.models 使用 datetime.UTC）::

    pytest tests/test_alembic_migrations.py -v

依赖与隔离说明
--------------
- ``backend/alembic/env.py`` 只 import 了 ``backend.config`` / ``backend.database`` /
  ``backend.models``（纯 sqlalchemy + pydantic-settings 依赖链），没有
  sentence-transformers / chromadb 等重型模块，因此不需要 monkeypatch 屏蔽
  重型 import。若未来 env.py 的 import 链变重，请在 fixture 里先向
  ``sys.modules`` 注入 stub 再触发 alembic 命令。
- env.py 通过 ``backend.config.get_settings()``（lru_cache）取数据库 URL，
  ``Settings.database_url`` 的环境变量别名是 ``DATABASE_URL``。fixture 通过
  monkeypatch 设置 DATABASE_URL 指向临时库并清空 lru_cache，保证每个测试
  用例拿到独立的 SQLite 文件（环境变量优先级高于 .env 文件，不受本机
  .env 影响）。

已知迁移风险（详见 docs/MIGRATION_RISKS.md）
------------------------------------------
- ``20260401_0001`` 的 upgrade 是 ``Base.metadata.create_all``：它按"当前代码
  的模型"建全量 schema，而不是 2026-04-01 时点的 schema。因此在全新数据库上
  依序执行迁移时，0002~0013 的 ``add_column`` / ``create_table`` 会因为对象
  已存在而失败（KNOWN_BROKEN_UPGRADES_ON_FRESH_DB）。生产实际依赖
  ``backend/main.py`` 启动时的 ``create_all + apply_runtime_schema_updates``
  兜底，alembic 链对全新库是断的。
- ``20260621_0014`` 的 downgrade 是 ``pass``（修复型迁移，设计上不可逆）。
- ``20260401_0001`` 的 downgrade 是 ``Base.metadata.drop_all``，会把"当前
  模型"的所有表（包括本应属于后续迁移的表）一次性删光。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
SCRIPT_LOCATION = REPO_ROOT / "backend" / "alembic"

# 迁移风险清单 1：在"全新空库"上按链执行时必定失败的 revision。
# 根因：0001 的 create_all 已按当前模型把这些迁移要新增的列/表建出来了。
# 如果修复了 0001（改为写死 2026-04-01 时点 schema）或给这些迁移补上
# inspector 守卫，请同步更新本集合与 docs/MIGRATION_RISKS.md。
KNOWN_BROKEN_UPGRADES_ON_FRESH_DB = {
    "20260412_0002",  # duplicate column: knowledge_chunks.is_disabled
    "20260413_0003",  # table chat_message_attachments already exists
    "20260414_0004",  # table llm_provider_configs already exists
    "20260523_0005",  # duplicate column: conversations.deleted_by_student_at
    "20260523_0006",  # duplicate column: messages.llm_model_key
    "20260523_0007",  # table llm_quota_policies / llm_usage_events already exists
    "20260525_0008",  # table notifications already exists
    "20260528_0009",  # duplicate column: llm_model_configs.vision_understanding_priority
    "20260618_0010",  # table student_feedback / student_feedback_bans already exists
    "20260621_0011",  # duplicate column: messages.assets
    "20260621_0012",  # table student_error_events / student_skill_profiles already exists
    "20260621_0013",  # table release_notes / release_note_read_states already exists
}

# 迁移风险清单 2：downgrade 为 no-op（不可逆）的 revision。
KNOWN_NOOP_DOWNGRADES = {
    "20260621_0014",  # repair-only migration，downgrade 显式 pass
}

# upgrade 到 head 后必须存在的关键业务表。
KEY_TABLES = {
    "users",
    "classrooms",
    "conversations",
    "messages",
    "chat_message_attachments",
    "knowledge_documents",
    "knowledge_chunks",
    "llm_provider_configs",
    "notifications",
    "student_feedback",
    "student_feedback_attachments",
    "release_notes",
    "agent_roles",
    "agent_role_revisions",
}


# ---------------------------------------------------------------------------
# fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def alembic_ctx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """返回 (alembic Config, sqlite 文件路径)，并把 env.py 的 DB URL 指向临时库。"""

    db_path = tmp_path / "migration_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    # env.py 里 settings = get_settings() 是 lru_cache 的，必须清缓存才能
    # 让本测试的 DATABASE_URL 生效（跨用例复用同一进程时尤其重要）。
    import backend.config as backend_config

    backend_config.get_settings.cache_clear()

    cfg = Config(str(ALEMBIC_INI))
    # ini 里 script_location 是相对 cwd 的相对路径，这里改成绝对路径，
    # 使测试不依赖 pytest 的启动目录。
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))

    yield cfg, db_path

    backend_config.get_settings.cache_clear()


def _script(cfg: Config) -> ScriptDirectory:
    return ScriptDirectory.from_config(cfg)


def _revision_order(cfg: Config) -> list[str]:
    """base -> head 顺序的 revision id 列表。"""
    walked = list(_script(cfg).walk_revisions("base", "heads"))
    return [rev.revision for rev in reversed(walked)]


def _current_revision(db_path: Path) -> str | None:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            if not inspect(conn).has_table("alembic_version"):
                return None
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def _table_names(db_path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _column_names(db_path: Path, table_name: str) -> set[str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return {column["name"] for column in inspect(engine).get_columns(table_name)}
    finally:
        engine.dispose()


def _stepwise_upgrade_to_head(cfg: Config) -> tuple[list[str], dict[str, str]]:
    """从当前状态逐个 revision upgrade 到 head。

    某一步失败时记录该 revision 及错误首行，然后 ``stamp`` 到该 revision 继续。
    stamp 跳过是安全的：失败原因都是"目标对象已被 0001 的 create_all 建出"，
    即目标 schema 已经满足。
    """
    failures: dict[str, str] = {}
    order = _revision_order(cfg)
    for rev in order:
        try:
            command.upgrade(cfg, rev)
        except Exception as exc:  # noqa: BLE001 - 故意收集为风险清单而非让测试崩溃
            failures[rev] = str(exc).splitlines()[0]
            command.stamp(cfg, rev)
    return order, failures


def _stepwise_downgrade_to_base(cfg: Config) -> dict[str, str]:
    """从 head 逐个 revision downgrade 到 base，返回 {失败revision: 错误首行}。"""
    script = _script(cfg)
    failures: dict[str, str] = {}
    for rev in _revision_order(cfg)[::-1]:
        target = script.get_revision(rev).down_revision or "base"
        try:
            command.downgrade(cfg, target)
        except Exception as exc:  # noqa: BLE001 - 收集不可逆迁移清单
            failures[rev] = str(exc).splitlines()[0]
            command.stamp(cfg, target)
    return failures


def _noop_downgrades(cfg: Config) -> set[str]:
    """静态分析：downgrade 函数体只有 pass/docstring 的 revision（不可逆）。"""
    noop: set[str] = set()
    for rev in _script(cfg).walk_revisions():
        tree = ast.parse(Path(rev.path).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
                body = [
                    stmt
                    for stmt in node.body
                    if not (
                        isinstance(stmt, ast.Expr)
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)
                    )
                ]
                if all(isinstance(stmt, ast.Pass) for stmt in body):
                    noop.add(rev.revision)
    return noop


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_migration_linearity(alembic_ctx) -> None:
    """迁移链必须是一条无分叉、无合并的单链。"""
    cfg, _ = alembic_ctx
    script = _script(cfg)

    heads = script.get_heads()
    assert len(heads) == 1, f"迁移链出现多个 head：{heads}"

    revisions = list(script.walk_revisions())
    assert revisions, "versions/ 目录下没有发现迁移"

    roots = [rev.revision for rev in revisions if rev.down_revision is None]
    assert roots == ["20260401_0001"], f"迁移链根节点异常：{roots}"

    down_revisions: list[str] = []
    for rev in revisions:
        assert not isinstance(rev.down_revision, tuple), (
            f"{rev.revision} 是 merge revision（down_revision 为元组），链不再线性"
        )
        if rev.down_revision is not None:
            down_revisions.append(rev.down_revision)
    duplicated = {d for d in down_revisions if down_revisions.count(d) > 1}
    assert not duplicated, f"以下 revision 被多个迁移作为 down_revision（分叉点）：{duplicated}"

    # 单链时 walk(base->head) 应覆盖所有迁移文件
    assert len(_revision_order(cfg)) == len(revisions)


def test_upgrade_head(alembic_ctx) -> None:
    """空库逐步 upgrade 到 head：关键表齐全，且失败步与已知风险清单一致。

    注意：直接 ``alembic upgrade head`` 在全新库上会在 0002 处失败（见模块
    docstring）。本测试逐步执行并对失败步 stamp 跳过，一方面验证最终 schema
    可用，另一方面把"哪些迁移在全新库上跑不动"固化为断言（迁移风险清单）。
    """
    cfg, db_path = alembic_ctx

    order, failures = _stepwise_upgrade_to_head(cfg)

    assert _current_revision(db_path) == order[-1], "未能推进到 head"

    missing = KEY_TABLES - _table_names(db_path)
    assert not missing, f"head 状态缺少关键表：{sorted(missing)}"
    assert "understanding_json" in _column_names(db_path, "chat_message_attachments")
    assert "active_practice" in _column_names(db_path, "conversations")
    assert "agent_role_revision_id" in _column_names(db_path, "messages")
    assert "agent_role_snapshot" in _column_names(db_path, "messages")

    assert set(failures) == KNOWN_BROKEN_UPGRADES_ON_FRESH_DB, (
        "全新库上失败的迁移与已知风险清单不一致。\n"
        f"实际失败：{sorted(failures)}\n"
        f"已知清单：{sorted(KNOWN_BROKEN_UPGRADES_ON_FRESH_DB)}\n"
        "若你修复/引入了迁移，请同步更新 KNOWN_BROKEN_UPGRADES_ON_FRESH_DB "
        "和 docs/MIGRATION_RISKS.md。\n"
        + "\n".join(f"  {rev}: {msg}" for rev, msg in sorted(failures.items()))
    )


def test_full_downgrade(alembic_ctx) -> None:
    """head -> base 逐步 downgrade：全部可执行，并固化 no-op（不可逆）清单。"""
    cfg, db_path = alembic_ctx

    _stepwise_upgrade_to_head(cfg)
    failures = _stepwise_downgrade_to_base(cfg)

    # 目前所有 downgrade 都能执行成功（0014 是 no-op，但执行不报错）。
    assert failures == {}, (
        "以下迁移 downgrade 失败（不可逆迁移清单，需记入 docs/MIGRATION_RISKS.md）：\n"
        + "\n".join(f"  {rev}: {msg}" for rev, msg in sorted(failures.items()))
    )

    assert _current_revision(db_path) is None, "downgrade base 后 alembic_version 仍有版本号"

    leftover = _table_names(db_path) - {"alembic_version"}
    assert not leftover, f"downgrade 到 base 后仍残留业务表：{sorted(leftover)}"

    # "执行成功"不等于"真的回滚了"：0014 的 downgrade 是 pass。
    # 静态固化 no-op downgrade 清单，防止未来悄悄新增不可逆迁移。
    noop = _noop_downgrades(cfg)
    assert noop == KNOWN_NOOP_DOWNGRADES, (
        "downgrade 为 no-op（不可逆）的迁移清单发生变化。\n"
        f"实际：{sorted(noop)}\n已知：{sorted(KNOWN_NOOP_DOWNGRADES)}\n"
        "请同步更新 KNOWN_NOOP_DOWNGRADES 和 docs/MIGRATION_RISKS.md。"
    )


def test_upgrade_downgrade_upgrade(alembic_ctx) -> None:
    """head -> 回退一步 -> 再 upgrade，验证最近一个迁移可往返。"""
    cfg, db_path = alembic_ctx

    order, _ = _stepwise_upgrade_to_head(cfg)
    head = order[-1]
    prev = order[-2]

    assert "understanding_json" in _column_names(db_path, "chat_message_attachments")
    assert "active_practice" in _column_names(db_path, "conversations")
    assert "agent_role_revision_id" in _column_names(db_path, "messages")

    command.downgrade(cfg, "-1")
    assert _current_revision(db_path) == prev
    assert "understanding_json" in _column_names(db_path, "chat_message_attachments")
    assert "agent_role_revision_id" not in _column_names(db_path, "messages"), "回退一步后角色消息列应被删除"
    assert "agent_roles" not in _table_names(db_path), "回退一步后角色表应被删除"

    command.upgrade(cfg, "head")
    assert _current_revision(db_path) == head
    assert "agent_role_revision_id" in _column_names(db_path, "messages"), "重新 upgrade 后角色消息列应被重建"
    assert "agent_roles" in _table_names(db_path), "重新 upgrade 后角色表应被重建"
