from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.models.conversation import GuidanceStage
from backend.subjects import SUBJECT_SET


class SubjectTeachingMode(StrEnum):
    MATH_PROBLEM = "math_problem"
    MATH_WORD_PROBLEM = "math_word_problem"
    MATH_CONCEPT = "math_concept"
    ENGLISH_GRAMMAR = "english_grammar"
    ENGLISH_WRITING = "english_writing"
    ENGLISH_VOCABULARY = "english_vocabulary"
    CHINESE_READING = "chinese_reading"
    CHINESE_WRITING = "chinese_writing"
    CHINESE_CLASSICAL = "chinese_classical"
    CHINESE_MATERIAL = "chinese_material"
    MATH_ERROR_REVIEW = "math_error_review"
    MATH_MULTI_SOLUTION = "math_multi_solution"
    ENGLISH_READING = "english_reading"
    CHEM_EQUATION = "chem_equation"
    CHEM_EXPERIMENT = "chem_experiment"
    CHEM_REPRESENTATION = "chem_representation"
    BIO_EXPERIMENT_DESIGN = "bio_experiment_design"
    BIO_PROCESS_CHAIN = "bio_process_chain"
    BIO_STRUCTURE_FUNCTION = "bio_structure_function"
    HIST_SOURCE = "hist_source"
    HIST_SPACETIME = "hist_spacetime"
    HIST_CAUSATION = "hist_causation"
    GEO_CHART = "geo_chart"
    GEO_PROCESS = "geo_process"
    GEO_LOCATION = "geo_location"
    POL_MATERIAL_PRINCIPLE = "pol_material_principle"
    POL_DEBATE = "pol_debate"
    MATH_FUNCTION = "math_function"
    MATH_GEOMETRY = "math_geometry"


@dataclass(frozen=True)
class SubjectGuidanceStrategy:
    subject: str
    teaching_mode: SubjectTeachingMode
    prompt_section: str
    fallback_text: str
    matched_by_trigger: bool = True  # False = 命中末位兜底规则（低置信，可供意图分类兜底升级）


@dataclass(frozen=True)
class SubjectRule:
    """声明式学科引导规则。triggers 为空元组时作为该学科兜底规则（须放末位）。"""

    mode: SubjectTeachingMode
    triggers: tuple[str, ...]
    prompt_section: str
    fallback_by_stage: dict[GuidanceStage, str]


# 九科全部走声明式注册表。每科最后一条规则 triggers=() 作为兜底，命中失败时仍返回它。
# 规则顺序即优先级（先命中先胜），迁移自原手写分支时保持原判定顺序。
SUBJECT_STRATEGY_RULES: dict[str, tuple[SubjectRule, ...]] = {
    "数学": (
        SubjectRule(
            mode=SubjectTeachingMode.MATH_WORD_PROBLEM,
            triggers=("应用题", "设x", "设 x", "列方程", "数量关系", "行程", "工程", "浓度", "利润", "相遇"),
            prompt_section=(
                "数学专项引导策略：应用题必须先做建模，不直接替学生列方程。"
                "请按“识别量→说关系→转方程”推进，让学生自己说出数量关系，再把自然语言转成数学语言。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先别急着列式。我们按数量关系三步来：识别量→说关系→转方程。你先把题目里变化的量和不变的量各圈出来。",
                GuidanceStage.HINT: "现在把每个量之间的关系说成一句普通话，再把这句话翻译成方程。你先试着写出最核心的数量关系。",
                GuidanceStage.FALLBACK: "我们收束一下：先设未知数，再列数量关系句，最后转成方程。最后一步求解和检验单位请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.MATH_ERROR_REVIEW,
            triggers=("错了", "错因", "又错", "总是错", "老是错", "为什么错", "错哪"),
            prompt_section=(
                "数学错因归类模式：引导学生把错误归到概念错、计算错还是审题错，"
                "不直接指出错在哪一步，让学生自己定位并说明错误原因。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先别看正确解法。你先回看自己的过程，判断这次是概念没吃透、计算失误，还是审题偏了？把可疑的那一步先圈出来。",
                GuidanceStage.HINT: "把你圈出的那一步和课本定义或例题对照一下：条件用全了吗？公式适用范围对吗？你先说说这一步的依据是什么。",
                GuidanceStage.FALLBACK: "我们按概念→计算→审题三类逐一排查：先复述考查的概念，再重算可疑步骤，最后回读题干核对条件。错因结论和订正请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.MATH_MULTI_SOLUTION,
            triggers=("还有别的方法", "另一种解法", "一题多解", "多种解法", "其他方法", "别的解法"),
            prompt_section=(
                "数学一题多解模式：引导学生比较不同方法的适用条件和优劣，"
                "不直接演示第二种解法，让学生自己尝试从另一个知识点切入。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先不急着给你另一种解法。你现在这种方法用到了哪个知识点？换一个角度想想，这道题还能不能从函数、几何或方程的另一条路切入？",
                GuidanceStage.HINT: "选定新角度后，先写出这条路里最关键的第一步，再比较它和原方法用到的条件有什么差异。你先把新思路的第一步写出来。",
                GuidanceStage.FALLBACK: "我们按知识点定位→新路径第一步→两种方法比较来收束。第二种解法的完整过程和优劣结论请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.MATH_CONCEPT,
            triggers=("什么是", "是什么意思", "为什么", "概念", "定义", "原理", "区别"),
            prompt_section=(
                "数学概念直觉模式：先用生活类比或图形直觉建立意义，再引入定义、公式或符号；"
                "不要只让学生背结论，要追问这个定义解决了什么问题。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先不背公式。你先说说这个概念想解决什么问题，再举一个最简单的例子来检验理解。",
                GuidanceStage.HINT: "把定义拆成条件和结论两部分，再用你举的例子逐条对照。你先说说例子满足了定义里的哪个条件。",
                GuidanceStage.FALLBACK: "我们按“解决什么问题→定义条件→典型例子→易混点”梳理。最后用一句自己的话概括这个概念，请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.MATH_FUNCTION,
            triggers=("函数", "单调", "最值", "图像", "导数", "零点"),
            prompt_section=(
                "数学函数专项模式：图像优先。先引导学生画出或想象函数图像，标出定义域、关键点和变化趋势，"
                "再把图像特征翻译成代数条件；不直接给出单调区间或最值结论。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先别急着求导或代公式。你先画出（或想象）这个函数的大致图像，说说定义域和图像的整体走势是什么？",
                GuidanceStage.HINT: "把图像特征翻译成代数语言：单调性对应导数或差值的符号，最值对应端点或极值点。你先写出对应的代数条件。",
                GuidanceStage.FALLBACK: "我们按“定义域→图像趋势→代数条件→求解验证”推进。最后的区间或最值结果请你自己算出，并回到图像上验证。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.MATH_GEOMETRY,
            triggers=("几何", "证明", "辅助线", "相似", "全等", "圆锥曲线", "向量"),
            prompt_section=(
                "数学几何证明模式：先引导学生把已知条件标注到图形上，从结论倒推需要的中间量，"
                "再考虑辅助线或坐标系；不直接给出证明步骤或辅助线做法。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先别急着写证明。你先把已知条件全部标到图上，再从要证的结论倒推：它需要哪个中间结论支撑？",
                GuidanceStage.HINT: "看看缺的那个中间结论：现有图形里的条件够不够用？如果不够，想想添一条什么样的辅助线（或建坐标系）能把条件连起来。你先说出你的候选方案。",
                GuidanceStage.FALLBACK: "我们按“标条件→倒推结论→补辅助线或建系→串联证明”梳理链条。最后一步的完整书写请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.MATH_PROBLEM,
            triggers=(),
            prompt_section=(
                "数学专项引导策略：先定位知识点和已知条件，再让学生选择方法。"
                "不要直接给计算结果；如果学生要同类题，优先给变式训练。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先把题目条件、目标结论和可能用到的知识点分开写。你先判断这题更像函数、几何、方程还是概率问题？",
                GuidanceStage.HINT: "从你定位的知识点出发，写出它对应的核心公式或性质，再看题目条件能代入哪些。你先完成这一步对应。",
                GuidanceStage.FALLBACK: "我们按“条件梳理→知识点定位→列式推进”拆解。最后的计算结果和检验请你自己完成。",
            },
        ),
    ),
    "英语": (
        SubjectRule(
            mode=SubjectTeachingMode.ENGLISH_VOCABULARY,
            triggers=("词汇DNA", "词汇 DNA", "存入词汇", "记单词", "背单词", "复习词汇", "单词"),
            prompt_section=(
                "英语词汇DNA模式：围绕词义、词性、例句和复习触发点帮助学生记词。"
                "只在学生明确要求时记录词汇；不要承诺外部定时提醒。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先把这个词拆成词义、词性和一个你能自己造的例句。你先说说它在这句话里是什么词性？",
                GuidanceStage.HINT: "再看它的常见搭配和近义词差别，试着在你的例句里替换一个搭配。你先说一个它的高频搭配。",
                GuidanceStage.FALLBACK: "我们按“词义→词性→搭配→自造句”闭环整理。最后那句自造例句和记忆触发点请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.ENGLISH_WRITING,
            triggers=("作文", "写作", "批改", "句式", "邮件", "段落", "改这篇"),
            prompt_section=(
                "英语写作专项：采用AI外教三维批改法，从语法、用词、逻辑中只抓最关键的2-3处。"
                "不要代写整篇作文，用追问引导学生自己升级句式。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "我们先不重写整篇。你先选一句最想提升的句子，我只从语法、用词、逻辑里挑最关键的一点让你自己改。",
                GuidanceStage.HINT: "对照我指出的那个维度，想想有没有更贴切的词或更清晰的句式可以替换。你先给出你的修改版本。",
                GuidanceStage.FALLBACK: "我们按“定位问题维度→方向提示→你来改写”推进。这句话的最终改写请你自己完成，改完我再帮你检查。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.ENGLISH_READING,
            triggers=("阅读理解", "七选五", "完形", "推断题", "细节题", "主旨题"),
            prompt_section=(
                "英语阅读理解模式：引导学生先定位题目对应的关键句，再用选项排除法逐项验证，"
                "不直接给出正确选项，让学生自己说明每个选项的取舍依据。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先别急着选。你先回原文定位到和这题相关的那一句，再看哪个选项和原文意思最贴、哪些明显能排除。你先排掉一个最不可能的选项。",
                GuidanceStage.HINT: "把剩下的选项和定位句逐词比对：哪个选项偷换了范围、程度或对象？你先指出一处偷换。",
                GuidanceStage.FALLBACK: "我们按“回原文定位→逐项排除→比对剩余选项”收束。最后的选择和理由请你自己给出。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.ENGLISH_GRAMMAR,
            triggers=(),
            prompt_section=(
                "英语语法追问教练：不要一次性改完所有错误。先定位最核心的1-2个语法点，"
                "用追问让学生自己发现主谓、时态、从句或搭配问题。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先看一个核心点：这句话的主语是谁，谓语动词应该跟主语保持什么形式？你先试着改第一处。",
                GuidanceStage.HINT: "再检查时态和从句：主句和从句的时间关系一致吗？连接词选对了吗？你先说说这句话的时间线。",
                GuidanceStage.FALLBACK: "我们按“主谓一致→时态→从句和搭配”逐层排查。最后一处的修改请你自己完成，并说明理由。",
            },
        ),
    ),
    "语文": (
        SubjectRule(
            mode=SubjectTeachingMode.CHINESE_MATERIAL,
            triggers=("素材库", "存入素材", "素材", "名言", "事例", "好句"),
            prompt_section=(
                "语文素材库模式：帮助学生按主题、人物、适用文体整理素材，并在写作前主动匹配。"
                "不要替学生写成完整作文。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先给这条素材打两个标签：它适合哪个主题？更适合议论文、记叙文还是说明文？",
                GuidanceStage.HINT: "再想想这条素材的独特角度：它和常见用法有什么不同？你先说一个别人不常用的切入点。",
                GuidanceStage.FALLBACK: "我们按“主题标签→文体适配→独特角度→一句概括”整理。最后那句素材点评请你自己写出来。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.CHINESE_CLASSICAL,
            triggers=("文言文", "古诗", "古诗词", "诗词", "苏轼", "李白", "杜甫", "翻译", "作者心情"),
            prompt_section=(
                "文言文复活模式：先还原作者处境、情感触发点和关键字词，再进入翻译或赏析。"
                "让学生从“这个人在什么情境中说这句话”开始理解。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先别逐字翻译。你先判断作者当时处在什么处境、最强烈的情绪是什么，再看关键词为什么这样写。",
                GuidanceStage.HINT: "抓住最能体现情绪的那个字或那句话，想想它的字面义和语境义差在哪里。你先解释这个关键词。",
                GuidanceStage.FALLBACK: "我们按“还原处境→定位情感→关键字词→整句理解”推进。最后的完整翻译或赏析请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.CHINESE_WRITING,
            triggers=("作文", "写作", "议论文", "记叙文", "说明文", "爆破思路", "提纲", "辩论"),
            prompt_section=(
                "语文写作教练：按“爆破思路→检验逻辑→学生自己写→AI首读→精准提升”推进。"
                "不代写作文，只激活学生自己的观点和材料。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先做思路爆破：围绕题目写出3个你真实相信的观点，再选一个最有证据支撑的观点展开。",
                GuidanceStage.HINT: "检验你选的观点：它能用什么事例或名言支撑？会不会被反例推翻？你先给出一个最有力的论据。",
                GuidanceStage.FALLBACK: "我们按“爆破观点→检验逻辑→搭提纲”收束。提纲和正文请你自己写，写完我来做首读反馈。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.CHINESE_READING,
            triggers=(),
            prompt_section=(
                "阅读理解拆解模式：从出题人视角分析题目，先判断考点，再回到文本找依据。"
                "不要直接给标准答案，训练学生识别常见失分坑。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先从出题人视角看：这题到底在考概括、赏析、作用，还是人物/情感理解？你先选一个考点。",
                GuidanceStage.HINT: "带着考点回文本：找出与题目直接相关的原句，圈出关键词。你先读出最关键的那一句。",
                GuidanceStage.FALLBACK: "我们按“判考点→回文本找证据→按答题框架组织”梳理。最后的答案组织请你自己完成，我再帮你对照失分点。",
            },
        ),
    ),
    "化学": (
        SubjectRule(
            mode=SubjectTeachingMode.CHEM_EQUATION,
            triggers=("配平", "化学方程式", "离子方程式", "系数", "守恒"),
            prompt_section=(
                "化学方程式配平模式：引导学生用质量守恒（原子个数守恒）和电荷守恒自查，"
                "不直接替学生写出或配平方程式；先让学生确认反应物、生成物，再逐元素核对个数。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先别急着配系数。你先把反应物和生成物的化学式确认清楚，再数一数等号两边每种原子各有几个。",
                GuidanceStage.HINT: "抓住最不平衡的那种原子，先给它配上系数，再顺着调整其他物质。你先说说哪种原子两边数量差得最多？",
                GuidanceStage.FALLBACK: "我们收束一下：逐元素守恒→定最难平的原子→连锁调整→最后查电荷。最后一步的系数核对和检查请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.CHEM_EXPERIMENT,
            triggers=("实验", "现象", "沉淀", "颜色", "气体", "褪色", "检验"),
            prompt_section=(
                "化学实验现象→原理模式：先让学生准确描述观察到的现象，再反推微观层面发生了什么反应；"
                "不直接给出结论，用追问引导学生把现象和原理对应起来。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先把你观察到的现象一条条说清楚：颜色、状态、有没有气体或沉淀。你先描述最明显的一个现象。",
                GuidanceStage.HINT: "把每个现象和一种可能的反应对应起来。你先想想这个颜色变化说明生成了什么？",
                GuidanceStage.FALLBACK: "我们按现象→微观反应→符号表达梳理。最后那步写出反应并解释成因，请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.CHEM_REPRESENTATION,
            triggers=(),
            prompt_section=(
                "化学三重表征模式：引导学生在宏观现象、微观粒子、符号表达三个视角之间切换；"
                "不直接给答案，先问学生这道题涉及哪个层面，再帮他连通另外两个层面。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先判断这道题问的是宏观现象、微观粒子还是符号表达？你先说说题目落在哪个层面。",
                GuidanceStage.HINT: "把你熟悉的那个层面写出来，再试着翻译到另外两个层面。你先从最有把握的一个开始。",
                GuidanceStage.FALLBACK: "我们把宏观、微观、符号三栏都列出来对应。最后的连通和结论请你自己补全。",
            },
        ),
    ),
    "生物": (
        SubjectRule(
            mode=SubjectTeachingMode.BIO_EXPERIMENT_DESIGN,
            triggers=("实验设计", "对照", "变量", "自变量", "假设"),
            prompt_section=(
                "生物对照实验模式：强调单一变量原则，先引导学生找出自变量、因变量和无关变量，"
                "再设计对照组；不直接给出完整方案，让学生自己补全设计。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先别写方案。你先找出这个实验想研究的自变量是什么，因变量又用什么来测量？",
                GuidanceStage.HINT: "围绕单一变量原则想想：除了自变量，其他条件该怎么控制一致？你先说说需要控制哪些无关变量。",
                GuidanceStage.FALLBACK: "我们按自变量→因变量→无关变量→对照组梳理。最后一步把完整方案写出来，请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.BIO_PROCESS_CHAIN,
            triggers=("光合", "呼吸", "遗传", "有丝", "减数", "流程", "传递"),
            prompt_section=(
                "生物过程链推理模式：把生命过程拆成“输入→环节→输出”分段追问，"
                "不直接给出整条流程，让学生逐段说清物质和能量的来龙去脉。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先定位这个过程发生在哪里、分几个阶段。你先说说它的起点输入和终点产物分别是什么？",
                GuidanceStage.HINT: "把过程拆成几个环节，看每一步的物质和能量怎么变化。你先说清第一个环节发生了什么。",
                GuidanceStage.FALLBACK: "我们按输入→各环节→输出把链条连起来。最后一步的完整衔接请你自己补全。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.BIO_STRUCTURE_FUNCTION,
            triggers=(),
            prompt_section=(
                "生物结构与功能对应模式：从“结构决定功能”出发追问对应关系，"
                "不直接给结论，引导学生说明某结构的特点如何支撑其功能。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先看结构：这个结构有什么特点？你先描述它最显著的形态或组成。",
                GuidanceStage.HINT: "把结构特点和它承担的功能连起来想想。你先说说这个特点对完成功能有什么帮助？",
                GuidanceStage.FALLBACK: "我们按结构特点→如何适应功能梳理对应关系。最后的结论请你自己得出。",
            },
        ),
    ),
    "历史": (
        SubjectRule(
            mode=SubjectTeachingMode.HIST_SOURCE,
            triggers=("史料", "材料", "根据材料", "出处"),
            prompt_section=(
                "历史史料研读模式：坚持“论从史出”，先引导学生读懂史料说了什么、谁写的、立场如何，"
                "再据此推论；不直接给出答案或代学生概括观点。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先别急着下结论。你先说说这则史料字面上讲了什么，它出自谁、什么立场？",
                GuidanceStage.HINT: "把史料的关键信息和设问对应起来，注意作者立场会影响可信度。你先提取最关键的一句。",
                GuidanceStage.FALLBACK: "我们按读懂史料→辨立场→据史推论梳理。最后一步的结论请你结合史料自己得出。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.HIST_SPACETIME,
            triggers=("时间", "朝代", "时期", "背景", "阶段特征"),
            prompt_section=(
                "历史时空定位模式：先引导学生确定事件的时间和空间坐标，再回忆该时期的阶段特征，"
                "不直接给出背景结论，让学生自己把史事放回时空框架。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先定位时空：这件事发生在什么时期、什么地方？你先说出你能确定的时间坐标。",
                GuidanceStage.HINT: "回忆这个时期政治、经济、思想上的阶段特征，再看题目落在哪一点。你先说一个该时期的特征。",
                GuidanceStage.FALLBACK: "我们按时间→空间→阶段特征把坐标建起来。最后的判断请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.HIST_CAUSATION,
            triggers=(),
            prompt_section=(
                "历史因果与评价模式：从政治、经济、文化等多角度追问因果链，"
                "不直接给出原因或评价，引导学生分层次、多角度自己组织论述。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先搭角度：分析原因或影响时，可以从政治、经济、文化几个方面切入。你先选一个你最有把握的角度。",
                GuidanceStage.HINT: "把每个角度的因果关系说成一句话，注意区分根本原因和直接原因。你先说出最直接的那个原因。",
                GuidanceStage.FALLBACK: "我们按多角度→分层次把因果链整理清楚。最后的综合评价请你自己得出。",
            },
        ),
    ),
    "地理": (
        SubjectRule(
            mode=SubjectTeachingMode.GEO_CHART,
            triggers=("图", "等高线", "气候图", "统计图", "判读", "如图"),
            prompt_section=(
                "地理图表判读模式：先引导学生看清图名、图例、坐标和单位，再从图中提取信息，"
                "不直接替学生读出结论，让学生自己完成从图到结论的推理。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先别看结论。你先读一读图名、图例和坐标轴，说说这张图在表达什么？",
                GuidanceStage.HINT: "从图里提取关键的数值或变化趋势，再和设问对应起来。你先说出一个最明显的信息。",
                GuidanceStage.FALLBACK: "我们按读图名图例→提取信息→对应设问梳理。最后一步的结论请你自己读出来。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.GEO_PROCESS,
            triggers=("过程", "成因", "形成", "演变", "循环"),
            prompt_section=(
                "地理过程推理模式：把地理过程按时序拆成几个环节分步推理，"
                "不直接给出成因，引导学生说清每一步的地理要素如何变化。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先理清这是一个什么过程、涉及哪些地理要素。你先说说它大致分几个阶段？",
                GuidanceStage.HINT: "按时间顺序把每个环节的变化说清楚，注意要素之间的因果。你先说出第一步发生了什么。",
                GuidanceStage.FALLBACK: "我们按时序把过程各环节串起来。最后一步的成因归纳请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.GEO_LOCATION,
            triggers=(),
            prompt_section=(
                "地理区位分析框架：引导学生从自然要素（地形、气候、水源等）和人文要素"
                "（市场、交通、政策、劳动力等）两栏分别列举，不直接给出结论，让学生自己筛选主导因素。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先搭框架：区位分析从自然要素和人文要素两方面入手。你先列出你想到的自然要素。",
                GuidanceStage.HINT: "把自然和人文要素分两栏列全，再判断哪个是主导因素。你先补充人文方面的要素。",
                GuidanceStage.FALLBACK: "我们把自然、人文两栏列清楚再排主次。最后的主导因素判断请你自己完成。",
            },
        ),
    ),
    "政治": (
        SubjectRule(
            mode=SubjectTeachingMode.POL_MATERIAL_PRINCIPLE,
            triggers=("材料", "结合材料", "体现", "如何体现"),
            prompt_section=(
                "政治材料-原理对应模式：先引导学生从材料中提取关键词和关键句，再匹配对应的学科原理，"
                "不直接给出原理或答案，让学生自己完成材料与原理的对接。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "先读材料：把材料里的关键词和关键句圈出来。你先说说材料主要在讲什么？",
                GuidanceStage.HINT: "把每个关键词对应到一个你学过的原理上。你先试着为最关键的一句话匹配一个原理。",
                GuidanceStage.FALLBACK: "我们按提取关键词→匹配原理→回扣材料梳理。最后一步的组织表述请你自己完成。",
            },
        ),
        SubjectRule(
            mode=SubjectTeachingMode.POL_DEBATE,
            triggers=(),
            prompt_section=(
                "政治辨析模式：引导学生对观点做“合理处+片面处”双向分析，"
                "不直接判定对错，让学生自己分层次说明为什么合理、又为什么片面。"
            ),
            fallback_by_stage={
                GuidanceStage.INITIAL: "辨析题不能简单说对或错。你先想想这个观点有没有合理的成分？先说合理的一面。",
                GuidanceStage.HINT: "再找找它片面或不严谨的地方，注意条件和范围。你先指出一个不够全面的点。",
                GuidanceStage.FALLBACK: "我们按合理处→片面处→修正表述梳理。最后的完整辨析请你自己得出。",
            },
        ),
    ),
}
if set(SUBJECT_STRATEGY_RULES) != SUBJECT_SET - {"物理"}:
    raise RuntimeError("SUBJECT_STRATEGY_RULES must cover every non-physics subject")


class SubjectGuidanceService:
    practice_request_signals = (
        "再来一题",
        "再给我一题",
        "再给我一道",
        "出一道",
        "推荐一道",
        "推荐题",
        "巩固题",
        "练习题",
        "变式题",
        "同类型",
        "类似题",
    )

    def analyze(
        self,
        question: str,
        subject: str,
        stage: GuidanceStage,
        *,
        mode_override: SubjectTeachingMode | None = None,
    ) -> SubjectGuidanceStrategy | None:
        if mode_override is not None:
            rules = SUBJECT_STRATEGY_RULES.get(subject)
            if rules:
                for rule in rules:
                    if rule.mode == mode_override:
                        fallback = rule.fallback_by_stage.get(stage) or rule.fallback_by_stage[GuidanceStage.INITIAL]
                        return SubjectGuidanceStrategy(
                            subject=subject,
                            teaching_mode=rule.mode,
                            prompt_section=rule.prompt_section,
                            fallback_text=fallback,
                            matched_by_trigger=True,  # 意图分类结果视为高置信
                        )
        return self._registry_strategy(question, subject, stage)

    def _registry_strategy(
        self, question: str, subject: str, stage: GuidanceStage
    ) -> SubjectGuidanceStrategy | None:
        rules = SUBJECT_STRATEGY_RULES.get(subject)
        if not rules:
            return None
        normalized = question.strip()
        matched = next((rule for rule in rules if any(kw in normalized for kw in rule.triggers)), None)
        matched_by_trigger = matched is not None
        if matched is None:
            matched = rules[-1]  # 末位规则作为兜底
        fallback = matched.fallback_by_stage.get(stage) or matched.fallback_by_stage[GuidanceStage.INITIAL]
        return SubjectGuidanceStrategy(
            subject=subject,
            teaching_mode=matched.mode,
            prompt_section=matched.prompt_section,
            fallback_text=fallback,
            matched_by_trigger=matched_by_trigger,
        )

    def is_math_practice_request(self, question: str) -> bool:
        normalized = question.strip()
        return any(signal in normalized for signal in self.practice_request_signals)


subject_guidance_service = SubjectGuidanceService()
