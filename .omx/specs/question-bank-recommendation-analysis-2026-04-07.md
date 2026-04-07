# 题库推荐链路优化方案（仅方案，不执行）

日期：2026-04-07

## 一、结论

当前最应该统一的核心不是 PDF 解析器本身，而是“题库后处理层”：

- PDF：继续让 MinerU 只负责解析，StudyAgent 负责切题 / 标签化 / 图片绑定 / 入库元数据
- DOCX：不要另走一套逻辑，也进入同一个题库后处理层
- 教材：继续保留结构化入库、标签化、图片管理，但不参与题目推荐
- 题目推荐：只从 `exercise / question_set` 里找
- 无图题：
  - 不是低质量
  - 不会因为没图而少推荐
  - 只有用户问题明显是图题需求时，含图题才额外加分

---

## 二、当前实现的关键判断

### 1. 推荐入口方向基本正确
当前 `backend/services/rag_service.py` 中：

- `QUESTION_RESOURCE_TYPES = {exercise, question_set}`
- `recommend_questions()` 先筛候选，再用 `_is_question_row()` 过滤
- `_recommendation_profile()` 已把推荐资源限制在题库类

这意味着：

- 教材 chunk 当前不会进入题目推荐主链路
- 推荐质量的核心不在教材解析，而在题库文档是否被切成高质量 `question_item`

### 2. 当前真正影响推荐质量的 4 个点

#### A. 是否稳定切成 `question_item`
理想状态下每道题都有：

- `question_number`
- `question_text`
- `answer_text`
- `explanation_text`

如果没切出来，退化为普通段落 chunk，推荐质量会波动。

#### B. 图片是否绑定到题目，而不是只绑定到文档
当前已经有：

- `asset_refs`
- `contains_images`
- `image_count`

但本质仍偏“近邻绑定”，还不是强语义的“这张图属于第 N 题”。

#### C. 元数据是否足够支撑检索与展示
当前已具备：

- `chunk_kind`
- `question_number`
- `question_text`
- `answer_text`
- `explanation_text`
- `asset_refs`

PDF 还额外有：

- `page_start/page_end/source_pages`
- `source_block_types`
- `structure_path`
- `parser_backend`

但 DOCX 路径还没有完全对齐到这一层级。

#### D. 是否错误惩罚无图题
当前逻辑下：

- `question_item` 有正向加分
- 有答案/解析有小幅加分
- 只有 query 明显是图题需求时，含图题才额外加分

因此当前并不会系统性打压无图题，这个方向应保留。

---

## 三、建议目标形态：统一“题目单元模型”

无论来源是 PDF 还是 DOCX，最终都落成同一种题目对象：

- `chunk_kind = question_item`
- `question_number`
- `question_text`
- `answer_text`
- `explanation_text`
- `asset_refs`
- `contains_images`
- `image_count`
- `chapter / section / tags`
- `structure_path`
- `source_locator`
- `parser_backend`
- `quality_flags`

也就是说：

- 解析器只负责抽文本 / 块 / 图片
- 题库后处理层负责把它整理成可推荐的题目单元

这才是长期可维护结构。

---

## 四、PDF / DOCX 的职责分工

### 1. PDF
- MinerU 只接管 PDF 解析
- 不让 MinerU 接管切题
- 切题、答案配对、标签、图片绑定，仍由 StudyAgent 自己做

原因：

- 不把题库逻辑绑死在某个解析器上
- 后续 DOCX 可复用同一套题库后处理规则
- 以后替换解析器也无需重写推荐链路

### 2. DOCX
DOCX 不应停留在“抽纯文本 + 简单图片标记”层面，而应：

- 先转成统一 block 序列
  - 段落
  - 表格行
  - 图片占位
- 再喂给同一个题库后处理器
- 最终产出与 PDF 一致的 `question_item`

这样 PDF / DOCX 在题目识别、答案配对、标签化、图片绑定、推荐上才能统一。

---

## 五、图片策略

### 1. 无图题 ≠ 低质量
以下题目都可能是正常高质量题：

- 纯文字选择题
- 纯文字填空题
- 纯文字计算题
- 纯文字证明题

不能因为 `asset_refs = []` 就判定低质量。

### 2. 只有“本该有图却丢图”才算质量问题
建议新增内部判断 `image_expectation`，根据题干信号判断：

- 如图
- 下图
- 图示
- 图中
- 根据图像
- 看图
- 电路图
- 受力图
- 几何图形
- 装置图

将题目分为：

- `not_needed`：不依赖图
- `optional`：有图更好但不是必须
- `required`：题干明确依赖图

只有 `required` 且 `asset_refs` 为空，才算图文不一致的质量缺陷。

### 3. 推荐时不因没图而少推
推荐排序应保持：

- 默认：无图题和有图题同权
- 用户问题带明显图题意图时：
  - 含图题加分
  - 无图题不必强扣分，只是相对排后

---

## 六、建议调整的内部元数据契约

### 必留字段
- `chunk_kind`
- `question_number`
- `question_text`
- `answer_text`
- `explanation_text`
- `asset_refs`
- `contains_images`
- `image_count`

### 建议新增字段
- `source_format`: `pdf | docx`
- `source_locator`: PDF 页码 / DOCX block 索引范围
- `structure_path`: `[chapter, section, ...]`
- `question_type`: 选择 / 填空 / 计算 / 实验 / 材料 / 综合
- `image_expectation`: `not_needed | optional | required`
- `image_binding_status`: `bound | missing_required | none_needed`
- `quality_flags`: 记录异常点
- `quality_score`: 内部质量评分
- `parser_backend`: `mineru-pdf | native-docx`
- `question_uid`: 文档内稳定题目 ID

---

## 七、低质量题库入库的定义

### 低质量信号
- 没切出题号，但一段里混了多道题
- `question_text` 过短或明显残缺
- 答案区和题干区错配
- 多道题并到一个 chunk
- 题干写“如图所示”但没有图
- 图片存在，但绑定到了错误题目
- 解析内容串到下一题

### 非低质量信号
- 没图
- 没解析
- 没答案（如果原文就没有）
- 题目很短但结构完整
- 纯文字题

---

## 八、当前最值得改的 3 个点

### 优先级 1：统一题库后处理层
避免长期演化成：

- PDF 一套
- DOCX 一套
- 推荐器再做一层补丁兜底

### 优先级 2：推荐入口收紧到“真正题目单元”
当前 `_is_question_row()` 还允许：

- `chunk_kind is None`
- `chunk_kind == ""`

这意味着一些没切好的题库段落也可能混进推荐候选。更理想的目标是：

- 优先只推荐 `question_item`
- 普通段落题库 chunk 不直接参与推荐，或至少显著降权

### 优先级 3：把图片绑定正确性显式建模
当前有图片，但缺少：

- 是否应该有图
- 是否绑对了图

补上之后：

- 题目展示更稳
- 推荐结果更可信
- “题目 + 对应图片” 输出更准

---

## 九、明确不做的范围

本轮方案继续排除：

- 不改前端大交互
- 不做已入库文档重跑
- 不改 TXT 路径
- 不让 MinerU 接管切题
- 教材不参与题目推荐
- 不把教材题干误识别问题当作当前主目标

补充说明：DOCX 仅在题库路径上与 PDF 对齐，不做整套通用文档路径重构。

---

## 十、建议实施顺序

### 第一步：统一题库后处理契约
先定义 PDF / DOCX 共用 question-item 元数据。

### 第二步：DOCX 对齐到同一题库后处理器
不要再让 DOCX 走弱结构路径。

### 第三步：推荐入口收紧
让题目推荐优先只吃高质量 `question_item`。

### 第四步：补图题判断
引入：

- `image_expectation`
- `image_binding_status`

### 第五步：补回归测试
至少覆盖：

- 无图纯文字题
- 如图题 + 成功绑图
- 如图题 + 丢图
- DOCX 题库图题
- PDF 题库图题
- 教材不参与推荐

---

## 十一、一句话结论

当前最该做的，不是继续折腾教材解析，而是把 PDF + DOCX 的“题库后处理层”统一起来。这样才能稳定实现：

- 题库推荐只从题库找
- 题目有结构
- 题目有标签
- 题目和图片能准确绑定
- 无图题不被误杀
- 图题需求时又能优先命中含图题
