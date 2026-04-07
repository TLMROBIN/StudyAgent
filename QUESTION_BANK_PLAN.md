# StudyAgent 题库功能扩展计划（修订版）

> 目标：在现有 StudyAgent 工程上增量建设“物理题库”，形成“批量导题 -> AI 标注与去重 -> 教师审核发布 -> 学生推荐练习 -> AI 引导讲解 -> 做题数据回流”的闭环。
>
> **Phase 1 rollout note**：当前 question-bank recommendation 实施以 `QUESTION_BANK_RECOMMENDATION_PHASE1.md` 和 `.omx/plans/` 下的 PRD / test spec 为准。该轮只覆盖 `exercise/question_set`、PDF/DOCX 对齐、`question_item` 优先推荐与显式 legacy fallback；`quality_score`、历史 backfill、TXT、教材推荐扩展仍属于后续阶段。

## 一、评估结论

原计划方向是对的，但如果按“单独造一套解析/导库/讲解链路”推进，会和当前项目已有能力重复，后期维护成本会明显上升。结合现有代码结构，建议做以下调整：

1. **保留离线构建，但只做“题库增强”**  
   现有项目已经具备 `docx/pdf/txt` 提取、题目拆块、OMML 转 LaTeX、图片抽取、向量写入、题目推荐、SSE 聊天等基础能力。离线工具应复用这些能力，只补 AI 标注、去重、回填题库元数据，不要再单独实现一套 `parse_word.py` 和 `export_to_db.py`。

2. **以 `KnowledgeDocument + KnowledgeChunk` 为主干，不重建存储体系**  
   题目正文、答案、解析、图片引用、向量索引，继续以现有知识库体系为准；新增 `QuestionMeta` 和 `PracticeRecord` 只承载题库特有的索引、审核和做题行为。

3. **一份源文件对应一个 `KnowledgeDocument`，不要整批只建一个 Document**  
   当前图片资源路径、导入任务、删除逻辑、资产目录都围绕单个 `document_id` 设计。整批题目强行塞到一个文档里，会破坏现有资产管理和追溯能力。

4. **学生侧默认只看“已发布题目”，教师侧需要审核入口**  
   AI 评分和去重不应直接决定学生可见性。至少要有 `review_status / published` 概念，否则低质量、过时或误判重复题会直接暴露给学生。

5. **题目讲解应复用现有 SSE 聊天编排，而不是另起一套生成链路**  
   当前 `chat` 路由已经接了过滤、对话记录、缓存、指标、苏格拉底引导。题库讲解应在此基础上封装“带题目上下文的讲解入口”。

结论：**该功能可做，且适合在当前项目上增量扩展；但需要从“独立子系统”调整为“复用现有主链路 + 增加题库特有元数据和工作流”。**

---

## 二、现有项目基础能力映射

| 现有能力 | 代码位置 | 可直接复用 |
|------|------|------|
| Word/PDF/TXT 内容提取 | `backend/services/rag_service.py` | 是 |
| Word 图片抽取与资源访问 | `backend/services/rag_service.py` + `backend/routers/knowledge.py` | 是 |
| Word 题目拆块、答案/解析配对 | `backend/services/rag_service.py` | 是 |
| OMML -> LaTeX 转换 | `backend/services/rag_service.py` | 是 |
| 向量入库与检索 | `backend/services/vector_store_service.py` | 是 |
| 题目推荐基础逻辑 | `backend/services/rag_service.py` + `backend/routers/chat.py` | 是 |
| 苏格拉底引导 prompt | `backend/services/socratic_service.py` | 是 |
| 学生端题目推荐卡片 UI | `frontend/src/views/StudentChat.vue` | 是 |
| 教师端知识库管理入口 | `frontend/src/views/KnowledgeManage.vue` | 是 |

因此，题库能力的合理做法不是“重写文档导入链路”，而是：

- 继续沿用现有 `knowledge` 导入主干；
- 增加一层“题库增强 / 审核 / 发布 / 练习记录”；
- 逐步把当前 `/api/chat/recommendations` 的题目推荐能力，迁移或委托到专门的题库服务。

---

## 三、架构决策

### 3.1 总体方案

采用 **“共享主链路 + 离线增强”** 架构：

```text
                 导入/构建阶段                               在线使用阶段
┌────────────────────────────────────┐   ┌────────────────────────────────────┐
│  现有 Knowledge 导入主链路          │   │  StudyAgent 在线服务               │
│                                    │   │                                    │
│  DOCX / PDF / TXT                  │   │  /api/questions/...                │
│    ↓                               │   │    - 题库浏览 / 推荐 / 做题 / 讲解  │
│  rag_service.extract_content()     │   │                                    │
│    ↓                               │   │  QuestionBankService               │
│  rag_service.prepare_document_     │   │    - 多维筛选                       │
│  chunks()                          │   │    - 个性化推荐                     │
│    ↓                               │   │    - 薄弱点统计                     │
│  KnowledgeChunk + ChromaDB         │   │                                    │
│    ↓                               │   │  chat / socratic / rag             │
│  question_builder CLI              │   │    - 讲解复用现有 SSE 链路          │
│    - AI 标注                       │   │                                    │
│    - 去重聚类                      │   │  PracticeRecord                     │
│    - 回填 QuestionMeta             │   │    - 做题行为回流                   │
└────────────────────────────────────┘   └────────────────────────────────────┘
```

### 3.2 关键设计原则

1. **题目正文、图片、向量仍以 `KnowledgeChunk` 为真源**
2. **题库查询、审核、推荐以 `QuestionMeta` 为索引层**
3. **学生可见性受 `review_status` 和发布状态控制**
4. **题库讲解复用 `chat` 现有对话、SSE、指标、缓存能力**
5. **先增强现有页面，再决定是否拆独立页面**

---

## 四、范围重构

### 4.1 MVP 范围

首期建议只做以下闭环：

1. 教师可批量导入组卷网 Word 题目，系统自动拆题、抽图、入库
2. 离线工具对题目做 AI 标注、去重、质量筛选，并写入 `QuestionMeta`
3. 教师端可筛选、审核、发布题目
4. 学生端可按知识点/难度获得推荐题
5. 学生做题后可记录自评结果
6. 学生可从题目进入 AI 引导讲解

### 4.2 暂缓到后续迭代

以下内容建议放到第二阶段之后：

1. 完整独立的 `PracticeMode.vue`
2. 复杂的自适应推荐模型
3. 教师端人工合并重复题的高级工作台
4. 题目来源批次管理看板
5. 多学科一起扩展

这样可以先把“数据打通”和“学生可用”做出来，而不是一开始把工作量铺得过大。

---

## 五、数据模型设计

### 5.1 新增表：`QuestionMeta`

该表用于承接题库特有的查询、审核、推荐和去重字段。

```python
# backend/models/question.py

class QuestionReviewStatus(str, Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    PUBLISHED = "published"
    REJECTED = "rejected"


class QuestionMeta(TimestampMixin, Base):
    __tablename__ = "question_meta"

    id: Mapped[int] = mapped_column(primary_key=True)
    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_chunks.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        index=True,
    )
    subject: Mapped[str] = mapped_column(String(32), index=True)

    # 题目分类
    question_type: Mapped[str] = mapped_column(String(16), index=True)
    knowledge_primary: Mapped[str] = mapped_column(String(32), index=True)
    knowledge_secondary: Mapped[str] = mapped_column(String(64), index=True)
    knowledge_tertiary: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ability_tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    # AI 评估
    difficulty_ai: Mapped[int] = mapped_column(Integer, index=True)
    quality_score: Mapped[float] = mapped_column(index=True)
    quality_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exam_style_score: Mapped[float] = mapped_column(default=0.0)
    is_outdated: Mapped[bool] = mapped_column(default=False, index=True)
    outdated_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_exam: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # 去重
    normalized_stem_hash: Mapped[str] = mapped_column(String(64), index=True)
    cluster_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_cluster_best: Mapped[bool] = mapped_column(default=True, index=True)

    # 审核发布
    review_status: Mapped[QuestionReviewStatus] = mapped_column(
        SqlEnum(QuestionReviewStatus),
        default=QuestionReviewStatus.DRAFT,
        index=True,
    )
    review_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # 关联
    chunk: Mapped["KnowledgeChunk"] = relationship()
    document: Mapped["KnowledgeDocument"] = relationship()
```

### 5.2 为什么要加 `review_status`

这是原计划里缺少但在当前项目里非常关键的一层：

1. AI 评分不是绝对可靠，必须允许教师兜底
2. 重复题、过时题、缺答案题，不应直接进入学生侧
3. 教师审核是质量控制点，也是后续题库治理入口

建议默认规则：

- 新导入题目：`draft`
- AI 评分完成且满足基础条件：`pending_review`
- 教师确认可用：`published`
- 教师认为不适合：`rejected`

### 5.3 新增表：`PracticeRecord`

```python
class PracticeRecord(TimestampMixin, Base):
    __tablename__ = "practice_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    question_meta_id: Mapped[int] = mapped_column(
        ForeignKey("question_meta.id", ondelete="CASCADE"),
        index=True,
    )
    is_correct: Mapped[bool | None] = mapped_column(nullable=True)
    confidence: Mapped[int | None] = mapped_column(nullable=True)  # 1-5
    time_spent_seconds: Mapped[int | None] = mapped_column(nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="practice")  # practice/chat/explain
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
```

### 5.4 聚合统计先不落在 `QuestionMeta`

原计划里的 `times_recommended`、`times_practiced`、`avg_student_score` 在首期不建议直接加到 `QuestionMeta`：

1. 当前开发环境包含 SQLite，频繁回写聚合字段会增加锁冲突风险
2. 这些值可以从 `PracticeRecord` 按需聚合
3. 后续如有性能瓶颈，再引入异步汇总表或物化指标

### 5.5 迁移策略

当前工程仍有 `Base.metadata.create_all()` 和 `apply_runtime_schema_updates()` 的兼容逻辑，因此新增表不能只写一句 `alembic revision --autogenerate` 就结束。

> 注：本节描述的是更完整题库索引层落地路线。当前 Phase 1 recommendation rollout **不要求** 历史 `KnowledgeChunk` 立即 backfill，也不依赖 `QuestionMeta` 先落地才可交付。

建议做法：

1. 新增 `backend/models/question.py`
2. 在 `backend/models/__init__.py` 和 `backend/main.py` 中导入新模型
3. 新增一份**手写 Alembic migration**
4. 对现有数据库补一段 backfill 任务，把历史 `question_item` 类型的 `KnowledgeChunk` 补成 `QuestionMeta`

说明：

- `autogenerate` 可以辅助生成草稿
- 但最终迁移文件要手工检查，尤其是 SQLite 兼容性和枚举字段

---

## 六、离线构建方案

### 6.1 目录结构调整

建议保留 `tools/question_builder/`，但职责改为“增强而不是重建”：

```text
tools/question_builder/
├── __init__.py
├── cli.py                    # Typer 入口
├── ai_scorer.py              # AI 标注
├── dedup.py                  # 去重聚类
├── repository.py             # 回写 QuestionMeta / 触发 backfill
├── models.py                 # 中间结构
├── config.yaml.example
└── README.md
```

模块职责边界建议明确如下：

- `repository.py`
  - 仅供离线 CLI 调用
  - 负责把 AI 标注、去重结果 `upsert` 到 `QuestionMeta`
  - 直接操作数据库，但不承载 HTTP 接口语义
- `backend/services/question_import_service.py`
  - 属于在线服务层
  - 负责“从已存在的 `KnowledgeChunk` 批量生成 / 重建 `QuestionMeta`”
  - 服务教师端 `rebuild-meta` 接口，以及历史数据 backfill

也就是说：

- `repository.py` 关注“离线增强结果如何写回”
- `question_import_service.py` 关注“系统内已有题目如何补齐题库元数据”

明确不建议首期再新增：

- `parse_word.py`
- `export_to_db.py`
- 独立 `requirements.txt`

原因：

1. 解析和导库逻辑已经在 `rag_service` 内实现
2. 再做一套会造成双轨逻辑
3. 长期维护时，Word 解析 bug 会出现两边修一边忘的问题

### 6.2 输入模式

离线工具支持两种输入方式：

1. **按目录导入源文件**
   - 工具内部调用现有导入能力，为每个文件创建一个 `KnowledgeDocument`
2. **对已有 `document_id` 做题库增强**
   - 适合老师已通过知识库页面上传过文件，再补题库标注

推荐 CLI：

```bash
# 对已有文档做题库增强
python -m tools.question_builder.cli enrich-document \
  --document-id 12 \
  --config ./tools/question_builder/config.yaml

# 对一个目录做导入 + 题库增强
python -m tools.question_builder.cli pipeline \
  --input-dir ./raw_word_files \
  --subject 物理 \
  --resource-type question_set \
  --config ./tools/question_builder/config.yaml
```

### 6.3 AI 标注输入对象

AI 标注不直接处理原始 Word，而是处理已经拆好的题目块：

```python
class QuestionDraft(BaseModel):
    document_id: int
    chunk_id: int
    question_number: str | None
    question_text: str
    answer_text: str | None
    explanation_text: str | None
    contains_images: bool
    source_exam: str | None
```

这样能保证：

1. 题干、答案、解析、图片引用和最终存储结构一致
2. AI 标注与现有解析结果严格对齐
3. 后续修解析时，题库增强工具天然受益

### 6.4 AI 标注输出

```python
class ScoredQuestion(QuestionDraft):
    question_type: str
    knowledge_primary: str
    knowledge_secondary: str
    knowledge_tertiary: str | None
    ability_tags: list[str]
    difficulty_ai: int
    quality_score: float
    quality_reason: str
    exam_style_score: float
    is_outdated: bool
    outdated_reason: str | None
    normalized_stem_hash: str
```

### 6.5 Prompt 设计建议

原计划的 prompt 方向可继续用，但需要补两个约束：

1. **使用受控标签集**
   - 一级、二级知识点不要全让模型自由发挥
   - 建议把物理知识点树放在配置文件中，prompt 中要求模型“从给定候选中选择”

2. **缺失信息显式返回 null**
   - 避免模型编造题源、知识点或题型

建议补一份 `taxonomy.physics.yaml`，供离线工具读取。

### 6.6 去重策略优化

原计划中的“同知识点内两两计算相似度 + DBSCAN”在题量上来后会变成 `O(n^2)`，不适合长期使用。

建议改为两级去重：

1. **精确去重**
   - 对标准化后的题干计算 `normalized_stem_hash`
   - hash 相同直接归为同题

`normalized_stem_hash` 的标准化规则建议在实现时固定为同一套纯函数，避免不同批次生成不一致。推荐流程：

1. 去除多余空白并做标点归一化
   - 连续空格、换行、制表压缩为空格
   - 全角/半角标点统一
2. LaTeX 公式保留结构但归一化空格
   - 去掉命令间无意义空格
   - 保留公式骨架，不把整段公式直接删除
3. 数值替换为占位符 `<NUM>`
   - 包括整数、小数、分数、科学计数法
   - 例如 `10m/s` 归一为 `<NUM>m/s`
4. 对归一化结果计算 `SHA-256`
5. 取前 16 字节对应的 hex 作为 `normalized_stem_hash`

这样做的目的，是把“同题换数值”“同题换排版”的情况优先归并，同时避免把公式结构完全抹平。

2. **近似去重**
   - 在 `knowledge_secondary + question_type` 分组内
   - 对题干向量做 top-k 邻居检索
   - 相似度超过阈值再入同一 cluster

3. **边界案例进入待审核**
   - 相似度处于灰区时，不自动淘汰，交给教师确认

### 6.7 写回策略

离线工具回写时做两件事：

1. 写入 / 更新 `QuestionMeta`
2. 同步一小部分兼容字段到 `KnowledgeChunk.metadata_json`

兼容字段建议保留：

> Phase 1 说明：下面的 JSON 是长期题库兼容字段示意，不是当前 recommendation rollout 的最小必需集合。当前实现约束请以 `QUESTION_BANK_RECOMMENDATION_PHASE1.md` 为准，尤其是 **不引入 `quality_score`**、并且仅把真正被现有推荐/展示链路消费的字段视为必须字段。

```json
{
  "chunk_kind": "question_item",
  "question_number": "3",
  "question_text": "...",
  "answer_text": "...",
  "explanation_text": "...",
  "contains_images": true,
  "asset_refs": [],
  "image_count": 1,
  "question_type": "计算题",
  "knowledge_primary": "力学",
  "knowledge_secondary": "牛顿运动定律",
  "difficulty_ai": 3,
  "quality_score": 8.5
}
```

这样做的目的不是让 `metadata_json` 成为主表，而是兼容当前已有接口和前端展示，避免一次性改动过大。

---

## 七、在线功能设计

### 7.1 API 分层建议

建议把题库 API 分成“教师管理”和“学生练习”两组，避免权限边界不清。

#### 教师 / 管理员

```text
GET  /api/questions/admin/bank
GET  /api/questions/admin/{question_meta_id}
POST /api/questions/admin/{question_meta_id}/review
POST /api/questions/admin/rebuild-meta
GET  /api/questions/admin/knowledge-tree
```

#### 学生 / 通用

```text
POST /api/questions/recommend
GET  /api/questions/{question_meta_id}
POST /api/questions/{question_meta_id}/practice
GET  /api/questions/me/stats
POST /api/questions/{question_meta_id}/explain/stream
```

### 7.2 题库浏览（教师侧）

`GET /api/questions/admin/bank`

筛选项建议：

- `subject`
- `knowledge_primary`
- `knowledge_secondary`
- `difficulty_min / difficulty_max`
- `quality_min`
- `question_type`
- `review_status`
- `hide_outdated`
- `hide_duplicates`
- `source_exam`
- `page / page_size`

默认筛选建议：

- `hide_outdated=true`
- `hide_duplicates=true`
- `review_status in (pending_review, published)`

### 7.3 智能推荐（学生侧）

`POST /api/questions/recommend`

请求体建议：

```python
class QuestionPracticeRecommendRequest(BaseModel):
    subject: str = "物理"
    knowledge_secondary: str | None = None
    difficulty: int | None = Field(default=None, ge=1, le=5)
    count: int = Field(default=5, ge=1, le=20)
    exclude_practiced: bool = True
```

说明：

1. 学生请求中**不要显式传 `student_id`**
2. 推荐逻辑默认使用当前登录学生
3. 如果教师想代某个学生查看推荐，单独做教师专用接口，并校验班级归属

### 7.4 推荐算法

首期推荐算法建议从可解释的规则模型起步：

```text
候选集过滤：
  1. review_status = published
  2. is_outdated = false
  3. is_cluster_best = true
  4. quality_score >= threshold
  5. subject / grade / 知识点 / 难度匹配

排序得分：
  语义相似度 * 0.30
  质量分     * 0.25
  薄弱点因子 * 0.25
  难度匹配   * 0.10
  新鲜度     * 0.10
```

冷启动策略：

- 无做题记录时，按 `knowledge_secondary` 均匀抽样
- 优先推荐高质量、标准难度题

### 7.5 做题记录

`POST /api/questions/{id}/practice`

建议记录：

- `is_correct`
- `confidence`
- `time_spent_seconds`
- `source`

注意：

1. 学生只能为自己写记录
2. 题目必须是 `published`
3. 若学生通过题目发起 AI 讲解，可把 `conversation_id` 回写到 `PracticeRecord`

### 7.6 题目讲解

`POST /api/questions/{id}/explain/stream`

这里不要重写一套“直接调用 LLM 的 SSE 路由”，应复用现有聊天链路。建议处理方式：

1. 取出 `QuestionMeta + KnowledgeChunk`
2. 构造一条包装后的学生问题，例如：

```text
请围绕下面这道题继续引导我，不要直接给答案：

【题目】
...

【知识点】
牛顿运动定律

【我的困惑】
我不知道该从哪个受力开始分析
```

3. 内部委托给现有 `chat` 编排或共享的聊天 service
4. 继续使用现有：
   - 过滤逻辑
   - 对话落库
   - SSE 输出
   - 指标与缓存

### 7.7 解析/答案暴露控制

学生侧不能在列表接口中默认拿到答案和解析全文。

建议规则：

1. 教师 / 管理员：可查看完整答案解析
2. 学生：
   - 推荐列表只给题目正文
   - 主动点击“查看解析”时才返回
   - 查看解析动作可以顺手记一条行为日志或 practice 事件

这点和现有 `/api/chat/recommendations` 的权限处理方式一致，应继续沿用。

---

## 八、前端改造策略

### 8.1 先扩展现有页面，不急着新建整套页面

首期推荐优先做：

1. **扩展 `KnowledgeManage.vue`**
   - 增加“题库”筛选模式或标签页
   - 支持按知识点、质量、审核状态筛题
   - 支持审核发布

2. **扩展 `StudentChat.vue`**
   - 在现有推荐卡片上增加：
     - 去练这题
     - AI 讲解
     - 查看解析

这样能最快落地，而且与现有交互最一致。

### 8.2 独立页面作为第二阶段

等后端稳定后，再新增：

```text
frontend/src/views/PracticeMode.vue
frontend/src/views/QuestionBankManage.vue
```

独立页面适合在以下条件满足后再做：

1. 推荐与做题链路已稳定
2. 教师审核工作量明显增多
3. 现有页面扩展开始显得拥挤

---

## 九、实施路线图

### Phase 0：样本与口径确认（0.5-1 天）

目标：在动代码前，先把题目格式和知识点口径固定。

输出：

1. 2-3 份组卷网 Word 样本
2. 物理知识点树配置
3. AI provider 与预算确认

### Phase 1：后端数据基础（1-2 天）

目标：新增题库元数据与做题记录能力。

输出：

1. `backend/models/question.py`
2. `QuestionMeta` / `PracticeRecord` / `QuestionReviewStatus`
3. 手写 Alembic migration
4. backfill 命令，把历史 `question_item` chunk 生成 `QuestionMeta`

验收：

1. 新老数据库都能启动
2. backfill 后历史题目可被查询

### Phase 2：离线增强工具（1.5-2 天）

目标：打通 AI 标注、去重、回填元数据。

输出：

1. `tools/question_builder/cli.py`
2. `ai_scorer.py`
3. `dedup.py`
4. `repository.py`
5. `config.yaml.example`

验收：

1. 能对样本文档完成标注
2. 能写入 `QuestionMeta`
3. 重跑时具备幂等性

### Phase 3：教师审核与题库查询（1-2 天）

目标：把题库从“数据存在”推进到“可治理”。

输出：

1. `backend/services/question_bank_service.py`
2. `backend/routers/questions.py`
3. 教师侧查询和审核接口
4. `KnowledgeManage.vue` 扩展

验收：

1. 教师能按知识点/难度/质量筛选
2. 教师能发布/拒绝题目

### Phase 4：学生练习闭环（1-2 天）

目标：让学生真正能用。

输出：

1. 推荐接口
2. 做题记录接口
3. 我的练习统计接口
4. 题目讲解 SSE 接口
5. `StudentChat.vue` 扩展

验收：

1. 学生能拿到推荐题
2. 做题后能形成记录
3. 可从题目进入 AI 讲解

### Phase 5：独立刷题模式与算法调优（后续迭代）

输出：

1. `PracticeMode.vue`
2. 更精细的薄弱点推荐
3. 重复题人工治理工作台
4. 统计看板增强

---

## 十、测试与验收

### 10.1 自动化测试

至少新增以下测试：

1. `tests/test_question_builder.py`
   - AI 标注结果解析
   - 去重逻辑
   - 幂等回写

2. `tests/test_question_bank_service.py`
   - 题库筛选
   - 推荐排序
   - 薄弱点统计

3. `tests/test_question_routes.py`
   - 教师审核权限
   - 学生推荐权限
   - 题目讲解入口

4. 扩展现有：
   - `tests/test_rag.py`
   - `tests/test_chat_stream.py`
   - `tests/test_knowledge_management.py`

### 10.2 样本验收口径

建议首批用 200-500 题做验收，关注以下指标：

1. 题目拆分成功率
2. 答案/解析匹配率
3. 图片引用保留率
4. AI 知识点标注准确率
5. 重复题误杀率
6. 学生推荐命中感知

### 10.3 发布验收标准

题库功能首期可视为“通过”的标准：

1. 3 份样本 Word 文件可稳定导入
2. 教师可完成审核发布
3. 学生只能看到 `published` 题目
4. 学生练习记录可形成统计
5. 题目讲解能走现有 SSE 聊天链路

---

## 十一、风险与应对

| 风险 | 影响 | 应对 |
|------|------|------|
| 组卷网 Word 格式不统一 | 拆题失败 | 先用样本建回归测试集，所有解析改动跑回归 |
| AI 标签漂移 | 知识点不稳定 | 使用受控知识点树，不允许自由发挥标签 |
| 重复题误判 | 好题被隐藏 | 两级去重 + 灰区转人工审核 |
| 迁移不稳 | 老库升级失败 | 手写 migration + backfill + SQLite 验证 |
| 学生提前看到答案 | 影响教学 | 学生接口默认不返回答案全文 |
| 另起一套导入逻辑 | 维护成本上升 | 强制复用现有 `rag_service` 和 `knowledge` 主链路 |

---

## 十二、文件清单建议

### 新增文件

```text
backend/
├── models/
│   └── question.py
├── routers/
│   └── questions.py
└── services/
    ├── question_bank_service.py
    └── question_import_service.py

tools/
└── question_builder/
    ├── __init__.py
    ├── cli.py
    ├── ai_scorer.py
    ├── dedup.py
    ├── repository.py
    ├── models.py
    ├── config.yaml.example
    └── README.md

tests/
├── test_question_builder.py
├── test_question_bank_service.py
└── test_question_routes.py
```

### 修改文件

```text
backend/main.py
backend/models/__init__.py
backend/models/schemas.py
backend/routers/chat.py
backend/services/rag_service.py
frontend/src/utils/api.ts
frontend/src/router/index.ts
frontend/src/views/KnowledgeManage.vue
frontend/src/views/StudentChat.vue
backend/alembic/versions/xxxx_add_question_bank_tables.py
```

---

## 十三、建议的下一步

按优先级建议如下：

1. **先确认样本和标签树**
   - 没有样本，解析适配和 AI 标注都容易空转

2. **先做数据层与 backfill**
   - 没有 `QuestionMeta`，后续查询、审核、推荐都会变成在 JSON 字段上硬扛

3. **MVP 前端先扩展现有页**
   - 比直接上新页面更稳，联调成本也更低

4. **题目讲解务必复用现有聊天编排**
   - 这是避免系统出现“双聊天链路”的关键

如果按这个修订版推进，题库功能会和现有项目保持同一套解析、存储、权限和对话体系，后续扩展成本会低很多。
