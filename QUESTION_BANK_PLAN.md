# 物理"必刷题库"功能开发方案

> 目标：从组卷网批量下载的 Word 试卷中，通过 AI 评分、去重、分层筛选，构建高质量物理题库，并集成到 StudyAgent 智能体中，实现智能推荐与引导式讲解。

## 一、架构决策

**采用"离线构建 + 在线集成"混合架构：**

| 部分 | 形态 | 原因 |
|------|------|------|
| 题库构建流水线 | 独立 CLI 工具 (`tools/question_builder/`) | 一次性/低频批处理，运行时间长，与用户交互无关 |
| 题目推荐与讲解 | 集成到 StudyAgent 主服务 | 复用现有 RAG、苏格拉底引导、用户体系、Celery |

```
                    离线阶段                              在线阶段
 ┌─────────────────────────────────┐    ┌─────────────────────────────────────┐
 │  tools/question_builder/        │    │  StudyAgent 主服务                   │
 │                                 │    │                                     │
 │  组卷网Word                      │    │  /api/questions/bank    题库浏览     │
 │    ↓                            │    │  /api/questions/recommend 智能推荐   │
 │  parse_word.py  解析拆题         │    │  /api/questions/{id}/explain 讲解   │
 │    ↓                            │    │  /api/questions/stats   做题统计     │
 │  ai_scorer.py   AI评分标注       │    │                                     │
 │    ↓                            │    │  socratic_service ← 引导式讲解      │
 │  dedup.py       去重聚类         │    │  rag_service      ← 相似题检索     │
 │    ↓                            │    │  stats_service    ← 学生薄弱点      │
 │  export.py      导入StudyAgent   │    │                                     │
 └────────────┬────────────────────┘    └─────────────────────────────────────┘
              │                                          ↑
              │  写入 KnowledgeDocument                   │
              │      + KnowledgeChunk                    │
              │      + QuestionMeta (新表)                │
              └──────────────────────────────────────────┘
```

---

## 二、离线工具：题库构建流水线

### 2.1 目录结构

```
tools/question_builder/
├── __init__.py
├── cli.py                 # 主入口，typer CLI
├── parse_word.py          # Word 解析与拆题
├── ai_scorer.py           # AI 评分与标注
├── dedup.py               # embedding 去重聚类
├── export_to_db.py        # 导出到 StudyAgent 数据库
├── models.py              # 中间数据结构 (Pydantic)
├── config.yaml            # 配置文件模板
├── requirements.txt       # 独立依赖
└── README.md              # 使用说明
```

### 2.2 中间数据结构

```python
# tools/question_builder/models.py

class RawQuestion(BaseModel):
    """从 Word 解析出的原始题目"""
    source_file: str               # 来源文件名
    question_index: int            # 在文件中的序号
    question_type: str             # 选择题/填空题/计算题/实验题
    question_text: str             # 题干文本（含 LaTeX 公式）
    options: list[str] | None      # 选择题选项
    answer_text: str | None        # 参考答案
    explanation_text: str | None   # 解析
    image_refs: list[str]          # 关联图片文件名
    source_exam: str | None        # 题源（如"2024全国甲卷"）
    raw_difficulty: str | None     # 组卷网原始难度标签
    raw_knowledge_tags: list[str]  # 组卷网原始知识点标签

class ScoredQuestion(RawQuestion):
    """AI 评分后的题目"""
    # --- 知识点标注 ---
    knowledge_primary: str         # 一级知识点（力学/电磁学/光学/热学/原子物理）
    knowledge_secondary: str       # 二级知识点（牛顿定律/动量守恒/...）
    knowledge_tertiary: str | None # 三级知识点（选填）
    ability_tags: list[str]        # 考察能力（建模/计算/图像分析/实验设计/定性分析）

    # --- 质量评估 ---
    quality_score: float           # 题目质量 1-10
    quality_reason: str            # 评分理由（一句话）
    difficulty_ai: int             # AI 评估难度 1-5（基于高考标准）
    exam_style_score: float        # 高考风格契合度 1-10
    is_outdated: bool              # 是否涉及旧课标已删内容
    outdated_reason: str | None    # 过时原因

    # --- 去重 ---
    embedding: list[float] | None  # 向量（去重用，不持久化到DB）
    cluster_id: str | None         # 聚类后的组ID
    is_cluster_best: bool          # 是否为该组最优题
```

### 2.3 模块详细设计

#### 2.3.1 parse_word.py — Word 解析拆题

**输入**：一个目录，包含组卷网下载的 `.docx` 文件

**处理逻辑**：

1. 遍历目录中所有 `.docx` 文件
2. 使用 `python-docx` 读取文档内容
3. 复用 StudyAgent 已有的 OMML→LaTeX 转换逻辑（`rag_service.py` 中的 `OMML_OPERATOR_MAP` 等）
4. 按题号正则拆分（适配常见格式）：

```python
QUESTION_SPLIT_PATTERNS = [
    # "1." "2." ... 或 "1、" "2、"
    re.compile(r"(?=(?:^|\n)\s*(\d{1,2})[.、．]\s)"),
    # "一、选择题" 大题标记（作为题型分隔）
    re.compile(r"(?=(?:^|\n)\s*[一二三四五六七八九十]+[、.．]\s*(?:选择|填空|计算|实验|解答))"),
]
```

5. 从文件名或文档头部提取题源信息（如 "2024年全国甲卷物理"）
6. 识别答案区域（常见标记："答案"、"解析"、"参考答案"）
7. 提取图片，保存到 `output/assets/` 并建立引用关系
8. 提取组卷网附带的元信息（难度、知识点标签，通常在题目末尾或旁注）

**输出**：`list[RawQuestion]`，序列化为 JSON 中间文件

**关键细节**：
- 物理题大量依赖公式和图片，解析时需保留 `[[asset:xxx.png]]` 标记
- 组卷网的格式可能因下载设置不同而有差异，需要用户提供样本后做适配
- 第一版先处理纯文本+公式，图片作为附件关联，不做 OCR

#### 2.3.2 ai_scorer.py — AI 评分与标注

**输入**：`list[RawQuestion]`

**处理逻辑**：

1. 构造评分 prompt（见下方）
2. 批量调用 AI API，每道题独立评分
3. 支持断点续跑（已评分的题目跳过）
4. 结果写入 `list[ScoredQuestion]`

**AI 评分 Prompt**：

```
你是一位具有20年教龄的高中物理特级教师和高考命题研究专家。
请对以下物理题目进行专业分析，严格按 JSON 格式输出。

## 题目信息
{question_text}
题源：{source_exam or "未知"}
组卷网难度：{raw_difficulty or "未标注"}
组卷网知识点：{raw_knowledge_tags or "未标注"}

## 分析要求

请输出以下 JSON（不要输出其他内容）：
{
  "knowledge_primary": "一级知识点，取值：力学/电磁学/光学/热学/原子物理/综合",
  "knowledge_secondary": "二级知识点，如：牛顿运动定律/动量守恒/电磁感应/...",
  "knowledge_tertiary": "三级知识点（可选，null 表示无需细分）",
  "ability_tags": ["考察能力列表，取值范围：物理建模/数学计算/图像分析/实验设计/定性分析/信息提取/多过程分析"],
  "question_type": "选择题/填空题/计算题/实验题",
  "difficulty_ai": 3,  // 1-5，1=送分题，3=中档题，5=压轴题（以高考为锚点）
  "quality_score": 8.0,  // 1-10，评分标准见下
  "quality_reason": "一句话说明评分理由",
  "exam_style_score": 7.5,  // 1-10，与近5年高考物理命题风格的契合度
  "is_outdated": false,
  "outdated_reason": null  // 若过时，说明原因（如涉及旧课标已删内容）
}

## 质量评分标准（quality_score）
- 9-10分：高考真题或与高考真题同等水平的原创题，考查核心物理思想
- 7-8分：优秀的模拟题，有较好的区分度，贴近高考命题方向
- 5-6分：中规中矩，考点明确但缺乏新意或区分度
- 3-4分：偏题怪题，或纯计算堆砌，物理思想含量低
- 1-2分：有知识性错误、题意不清、或已完全不符合当前课标

## 过时判定依据
以下内容在2019版课标中已调整或删除，涉及则标记为过时：
- 动量定理（已从必修移至选修，但仍在高考范围，不算过时）
- 热力学第二定律的具体计算（仅需定性理解）
- 光的偏振的定量计算
- 其他明显不符合新高考命题趋势的内容
```

**API 调用策略**：
- 优先使用廉价模型（如 Claude Haiku 或 DeepSeek）
- 并发控制：最多 5 个并发请求，避免触发限流
- 单题超时 30s，失败自动重试 2 次
- 支持配置不同的 API provider（OpenAI 兼容接口）

**配置示例** (`config.yaml`)：

```yaml
scorer:
  api_base_url: "https://api.anthropic.com/v1"  # 或其他兼容接口
  api_key: "${SCORER_API_KEY}"                   # 环境变量引用
  model: "claude-haiku-4-5-20251001"
  max_concurrency: 5
  timeout_seconds: 30
  retry_count: 2

  # 质量筛选阈值
  quality_threshold: 6.0        # 低于此分的题目标记为低质量
  exam_style_threshold: 5.0     # 低于此分的题目标记为偏离高考风格
```

#### 2.3.3 dedup.py — 去重聚类

**输入**：`list[ScoredQuestion]`

**处理逻辑**：

1. 对每道题的 `question_text` 生成 embedding 向量
   - 使用 StudyAgent 已有的 `embed_service`（bge-m3 或 bge-small-zh-v1.5）
   - 或使用外部 API（如 Voyage、OpenAI embedding）
2. 在同一 `knowledge_secondary`（二级知识点）内计算两两余弦相似度
3. 相似度 > 0.85 的题目归入同一聚类
4. 每个聚类中选 `quality_score` 最高的题目标记为 `is_cluster_best = True`
5. 其余题目保留但标记 `is_cluster_best = False`（不删除，教师可手动调整）

**去重粒度说明**：
- 不跨知识点去重（牛顿定律的题和动量的题即使文本相似也不算重复）
- 同一知识点下，换数字/换情境但解法完全相同的算重复
- 聚类算法使用 DBSCAN（eps=0.15 对应相似度 0.85），无需预设簇数

#### 2.3.4 export_to_db.py — 导入 StudyAgent

**输入**：`list[ScoredQuestion]`（已去重标注）

**处理逻辑**：

1. 连接 StudyAgent 的数据库（SQLite 或 PostgreSQL）
2. 为整批导入创建一个 `KnowledgeDocument`（resource_type = `question_set`）
3. 每道题创建一个 `KnowledgeChunk`，将 AI 评分结果写入 `metadata_json`
4. 同时写入新表 `QuestionMeta`（见第三节数据模型）
5. 调用 `embed_service` 生成向量，写入 ChromaDB

**metadata_json 扩展字段**：

```json
{
  "chunk_kind": "question_item",
  "question_number": "3",
  "question_text": "...",
  "answer_text": "...",
  "explanation_text": "...",
  "contains_images": true,
  "image_count": 1,
  "asset_refs": ["img_003.png"],

  "quality_score": 8.5,
  "difficulty_ai": 3,
  "exam_style_score": 7.5,
  "is_outdated": false,
  "outdated_reason": null,
  "knowledge_primary": "力学",
  "knowledge_secondary": "牛顿运动定律",
  "knowledge_tertiary": "超重与失重",
  "ability_tags": ["物理建模", "数学计算"],
  "source_exam": "2024年全国甲卷",
  "cluster_id": "newton_003",
  "is_cluster_best": true,
  "quality_reason": "经典超重失重情境，考查受力分析与牛顿第二定律的综合应用"
}
```

### 2.4 CLI 使用流程

```bash
# 1. 解析 Word 文件
python -m tools.question_builder.cli parse \
  --input-dir ./raw_word_files/ \
  --output ./parsed/questions.json

# 2. AI 评分标注
python -m tools.question_builder.cli score \
  --input ./parsed/questions.json \
  --output ./scored/questions.json \
  --config ./tools/question_builder/config.yaml

# 3. 去重聚类
python -m tools.question_builder.cli dedup \
  --input ./scored/questions.json \
  --output ./deduped/questions.json \
  --similarity-threshold 0.85

# 4. 导入 StudyAgent
python -m tools.question_builder.cli export \
  --input ./deduped/questions.json \
  --subject 物理 \
  --db-url sqlite:///data/studyagent.db

# 或一键执行全流程
python -m tools.question_builder.cli pipeline \
  --input-dir ./raw_word_files/ \
  --subject 物理 \
  --config ./tools/question_builder/config.yaml
```

---

## 三、数据模型扩展

### 3.1 新增表：QuestionMeta

在现有 `KnowledgeChunk` 之上新增一张专门的题目元数据表，用于支持高效的题库查询和推荐。

```python
# backend/models/question.py

class QuestionMeta(TimestampMixin, Base):
    """题目元数据表，与 KnowledgeChunk 一对一关联"""
    __tablename__ = "question_meta"

    id: Mapped[int] = mapped_column(primary_key=True)
    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_chunks.id", ondelete="CASCADE"),
        unique=True, index=True
    )
    subject: Mapped[str] = mapped_column(String(32), index=True)

    # --- 知识点分类 ---
    knowledge_primary: Mapped[str] = mapped_column(String(32), index=True)
    knowledge_secondary: Mapped[str] = mapped_column(String(64), index=True)
    knowledge_tertiary: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ability_tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    # --- 题型与难度 ---
    question_type: Mapped[str] = mapped_column(String(16), index=True)  # 选择/填空/计算/实验
    difficulty_ai: Mapped[int] = mapped_column(Integer, index=True)     # 1-5
    source_exam: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)

    # --- 质量评估 ---
    quality_score: Mapped[float] = mapped_column(default=0.0, index=True)
    quality_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exam_style_score: Mapped[float] = mapped_column(default=0.0)
    is_outdated: Mapped[bool] = mapped_column(default=False, index=True)
    outdated_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- 去重 ---
    cluster_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_cluster_best: Mapped[bool] = mapped_column(default=True, index=True)

    # --- 使用统计（在线阶段填充） ---
    times_recommended: Mapped[int] = mapped_column(Integer, default=0)
    times_practiced: Mapped[int] = mapped_column(Integer, default=0)
    avg_student_score: Mapped[float | None] = mapped_column(nullable=True)

    # --- 关联 ---
    chunk: Mapped["KnowledgeChunk"] = relationship()
```

**为什么不只用 metadata_json？**
- `metadata_json` 是 JSON 字段，无法高效建索引和做复合查询
- 题库浏览需要按 `knowledge_primary + difficulty_ai + quality_score` 排序筛选
- 推荐算法需要按 `is_cluster_best + is_outdated + quality_score` 过滤
- 独立表可以添加使用统计字段，不污染知识库的通用结构

### 3.2 新增表：PracticeRecord

记录学生做题行为，用于个性化推荐和薄弱点分析。

```python
# backend/models/question.py

class PracticeRecord(TimestampMixin, Base):
    """学生做题记录"""
    __tablename__ = "practice_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    question_meta_id: Mapped[int] = mapped_column(
        ForeignKey("question_meta.id", ondelete="CASCADE"), index=True
    )
    # 做题结果
    is_correct: Mapped[bool | None] = mapped_column(nullable=True)  # 学生自评或教师批改
    confidence: Mapped[int | None] = mapped_column(nullable=True)   # 学生自评信心 1-5
    time_spent_seconds: Mapped[int | None] = mapped_column(nullable=True)
    # 关联到对话（如果学生通过智能体讲解了这道题）
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True
    )
```

### 3.3 数据库迁移

```bash
# 生成迁移文件
cd backend
alembic revision --autogenerate -m "add question_meta and practice_records tables"
alembic upgrade head
```

---

## 四、在线功能：集成到 StudyAgent

### 4.1 新增 API 路由

```python
# backend/routers/questions.py

router = APIRouter(prefix="/api/questions", tags=["questions"])
```

#### 4.1.1 题库浏览

```
GET /api/questions/bank
    Query参数：
        subject: str = "物理"
        knowledge_primary: str | None       # 一级知识点筛选
        knowledge_secondary: str | None     # 二级知识点筛选
        difficulty_min: int = 1             # 难度下限
        difficulty_max: int = 5             # 难度上限
        quality_min: float = 6.0            # 质量下限
        question_type: str | None           # 题型筛选
        hide_outdated: bool = True          # 默认隐藏过时题
        hide_duplicates: bool = True        # 默认只显示聚类最优题
        sort_by: str = "quality_score"      # 排序字段
        page: int = 1
        page_size: int = 20
    返回：
        Page[QuestionBankItem]
```

#### 4.1.2 智能推荐

```
POST /api/questions/recommend
    Body：
        subject: str = "物理"
        student_id: int | None              # 可选，传入则基于薄弱点推荐
        knowledge_secondary: str | None     # 指定知识点
        difficulty: int | None              # 指定难度
        count: int = 5                      # 推荐数量
        exclude_practiced: bool = True      # 排除已做过的题
    返回：
        list[QuestionRecommendation]

    推荐算法：
        1. 基础过滤：is_cluster_best=True, is_outdated=False, quality_score>=6
        2. 若传入 student_id：
           a. 查询 PracticeRecord，统计各 knowledge_secondary 的正确率
           b. 正确率最低的知识点优先推荐（薄弱点补强）
           c. 已做过且答对的题排除
        3. 若指定知识点/难度：按指定条件筛选
        4. 在候选集中按 quality_score * 0.6 + exam_style_score * 0.4 加权排序
        5. 返回 top-N
```

#### 4.1.3 题目讲解（对接苏格拉底引导）

```
POST /api/questions/{question_meta_id}/explain
    Body：
        student_message: str                # 学生的具体疑问（可选）
        conversation_id: int | None         # 续接已有对话
    返回：
        StreamingResponse (SSE)             # 复用现有 chat 的 SSE 流式响应

    处理流程：
        1. 从 QuestionMeta → KnowledgeChunk 获取题目文本和答案
        2. 构造增强 prompt，注入题目上下文：
           "学生正在练习以下物理题，请用苏格拉底式引导帮助理解：
            【题目】{question_text}
            【知识点】{knowledge_secondary}
            【学生疑问】{student_message or '请帮我分析这道题的解题思路'}"
        3. 调用 socratic_service 生成引导式回答
        4. 创建/续接 Conversation 记录
```

#### 4.1.4 做题记录

```
POST /api/questions/{question_meta_id}/practice
    Body：
        is_correct: bool | None             # 自评对错
        confidence: int | None              # 自评信心 1-5
        time_spent_seconds: int | None
    返回：
        PracticeRecordRead

GET /api/questions/practice-stats
    Query参数：
        student_id: int
    返回：
        PracticeStatsRead:
            total_practiced: int
            correct_rate: float
            by_knowledge: list[KnowledgeAccuracy]  # 各知识点正确率
            weak_points: list[str]                  # 薄弱知识点（正确率<60%）
            recent_trend: list[DailyPractice]       # 近7天做题曲线
```

### 4.2 新增 Service

```python
# backend/services/question_bank_service.py

class QuestionBankService:
    """题库查询与推荐服务"""

    def query_bank(self, db, filters, sort_by, page, page_size) -> Page:
        """分页查询题库，支持多维筛选"""

    def recommend(self, db, student_id, knowledge, difficulty, count) -> list:
        """智能推荐算法"""

    def get_weak_points(self, db, student_id) -> list[str]:
        """分析学生薄弱知识点"""

    def record_practice(self, db, student_id, question_meta_id, result) -> PracticeRecord:
        """记录做题结果"""

    def get_practice_stats(self, db, student_id) -> PracticeStats:
        """获取做题统计"""

    def get_knowledge_tree(self, db, subject) -> dict:
        """获取知识点树形结构（用于前端筛选器）"""
        # 从 QuestionMeta 聚合出：
        # { "力学": { "牛顿运动定律": 45, "动量守恒": 32, ... }, ... }
```

### 4.3 前端新增页面

#### 教师端：题库管理

```
frontend/src/views/QuestionBankManage.vue
```

- 左侧：知识点树形导航（一级→二级→三级）
- 右侧：题目列表，支持筛选（难度、题型、质量分、题源）
- 每道题显示：题目文本、质量评分（星标）、难度标签、知识点标签、题源
- 支持操作：标记为"必刷"、移除、手动调整难度/知识点、查看聚类中的重复题
- 顶部统计：总题数、各难度分布、各知识点覆盖情况

#### 学生端：刷题模式

```
frontend/src/views/PracticeMode.vue
```

- 入口：首页新增"刷题"入口卡片
- 推荐模式：系统基于薄弱点自动推荐 5 题
- 自选模式：按知识点+难度自选
- 做题界面：
  - 显示题目（含图片、公式）
  - "我会了" / "不确定" / "不会" 三按钮（简单自评）
  - "查看解析" 按钮 → 展开答案
  - "AI 讲解" 按钮 → 跳转到对话界面，自动携带题目上下文
- 做题统计面板：今日已做、正确率、薄弱知识点雷达图

---

## 五、推荐算法详细设计

### 5.1 多因子评分模型

对题库中每道题计算一个"推荐分"：

```
推荐分 = W1 * 质量因子 + W2 * 薄弱点因子 + W3 * 新鲜度因子 + W4 * 难度匹配因子

其中：
  质量因子 = quality_score / 10 * 0.6 + exam_style_score / 10 * 0.4
  薄弱点因子 = 1 - 该知识点正确率（正确率越低，推荐越优先）
  新鲜度因子 = 该题是否已做过？已做过=0, 未做过=1
  难度匹配因子 = 1 - |difficulty_ai - 学生当前水平| / 4
                 （学生水平根据近期正确率推算）

默认权重：
  W1=0.3, W2=0.35, W3=0.2, W4=0.15
```

### 5.2 学生水平自动评估

```
学生物理水平(1-5) = 加权平均(最近50道题的难度 * 是否答对)

示例：
  最近做了10道难度3的题，对了8道 → 水平≈3.5，推荐难度3-4的题
  最近做了10道难度2的题，对了5道 → 水平≈2，推荐难度1-2的题
```

---

## 六、实施路线图

### Phase 1：离线工具（预计 3-4 天）

| 步骤 | 任务 | 前置条件 |
|------|------|---------|
| 1.1 | 用户提供 2-3 个组卷网 Word 样本文件 | - |
| 1.2 | 开发 `parse_word.py`，适配实际格式 | 1.1 |
| 1.3 | 开发 `ai_scorer.py`，调通 AI 评分 | 确认可用的 API |
| 1.4 | 开发 `dedup.py`，embedding 去重 | - |
| 1.5 | 开发 `export_to_db.py`，写入数据库 | Phase 2.1 |
| 1.6 | 开发 `cli.py`，串联全流程 | 1.2-1.5 |

### Phase 2：数据模型与后端 API（预计 2-3 天）

| 步骤 | 任务 | 前置条件 |
|------|------|---------|
| 2.1 | 新增 `QuestionMeta`、`PracticeRecord` 模型，生成迁移 | - |
| 2.2 | 开发 `question_bank_service.py` | 2.1 |
| 2.3 | 开发 `/api/questions/` 路由（bank、recommend、explain、practice） | 2.2 |
| 2.4 | 对接 `socratic_service` 实现引导式讲解 | 2.3 |

### Phase 3：前端页面（预计 2-3 天）

| 步骤 | 任务 | 前置条件 |
|------|------|---------|
| 3.1 | 开发 `QuestionBankManage.vue`（教师端题库管理） | 2.3 |
| 3.2 | 开发 `PracticeMode.vue`（学生端刷题界面） | 2.3 |
| 3.3 | 扩展 `AdminDashboard.vue`，增加题库统计面板 | 2.3 |
| 3.4 | 扩展学生画像，增加做题数据维度 | 2.3 |

### Phase 4：调优与迭代（持续）

| 步骤 | 任务 |
|------|------|
| 4.1 | 首批导入 200-500 题，人工抽检 AI 评分准确率 |
| 4.2 | 根据抽检结果调整评分 prompt 和阈值 |
| 4.3 | 收集学生使用数据，优化推荐算法权重 |
| 4.4 | 持续导入新题，扩充题库 |

---

## 七、成本估算

### 一次性成本（以 2000 道题为例）

| 项目 | 用量 | 单价 | 总计 |
|------|------|------|------|
| AI 评分（Claude Haiku） | 2000 题 × ~500 token/题 | ~$0.25/百万 input token | ~$0.5 |
| Embedding（本地 bge-m3） | 2000 题 | 本地运行 | $0 |
| **合计** | | | **< $1** |

### 持续运营成本

| 项目 | 频率 | 成本 |
|------|------|------|
| 新题评分 | 每月 100-200 题 | < $0.1/月 |
| 学生 AI 讲解 | 复用现有 LLM 配额 | 已包含 |

---

## 八、风险与应对

| 风险 | 影响 | 应对措施 |
|------|------|---------|
| Word 格式差异大 | 解析失败率高 | 先用 3 个样本适配，设计容错解析 |
| AI 评分准确率不够 | 好题被漏选/差题混入 | 首批人工抽检 10%，迭代 prompt |
| 物理公式/图片丢失 | 部分题目不可用 | 第一版保留图片引用，标记含图题 |
| 组卷网题源标注缺失 | 无法判断题目来源 | 从文件名/文档标题提取，缺失的标记为"未知" |
| 学生不愿自评对错 | 推荐算法冷启动 | 默认按知识点均匀推荐，积累数据后切换 |

---

## 九、文件清单（新增/修改）

### 新增文件

```
tools/
└── question_builder/
    ├── __init__.py
    ├── cli.py
    ├── parse_word.py
    ├── ai_scorer.py
    ├── dedup.py
    ├── export_to_db.py
    ├── models.py
    ├── config.yaml
    └── requirements.txt

backend/
├── models/
│   └── question.py                    # QuestionMeta, PracticeRecord
├── routers/
│   └── questions.py                   # /api/questions/* 路由
└── services/
    └── question_bank_service.py       # 题库查询、推荐、统计

frontend/src/
└── views/
    ├── QuestionBankManage.vue          # 教师端题库管理
    └── PracticeMode.vue               # 学生端刷题模式

alembic/versions/
└── xxxx_add_question_bank_tables.py   # 数据库迁移
```

### 修改文件

```
backend/main.py                        # 注册 questions router
backend/models/__init__.py             # 导入新模型
frontend/src/router/index.ts           # 添加新页面路由
frontend/src/views/AdminDashboard.vue  # 增加题库统计面板
frontend/src/views/StudentChat.vue     # 增加"去刷题"入口
```

---

## 十、下一步行动

1. **请提供 2-3 个组卷网下载的 Word 样本文件**，放到项目目录下，用于适配解析逻辑
2. **确认 AI API 选择**：使用 Claude API、DeepSeek、还是现有的 MiniMax/Qwen 做评分？
3. 确认后，从 Phase 1（离线工具）开始开发
