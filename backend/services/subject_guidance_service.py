from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.models.conversation import GuidanceStage


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


@dataclass(frozen=True)
class SubjectGuidanceStrategy:
    subject: str
    teaching_mode: SubjectTeachingMode
    prompt_section: str
    fallback_text: str


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
    concept_signals = ("什么是", "是什么意思", "为什么", "概念", "定义", "原理", "区别")
    math_word_problem_signals = ("应用题", "设x", "设 x", "列方程", "数量关系", "行程", "工程", "浓度", "利润", "相遇")
    english_writing_signals = ("作文", "写作", "批改", "句式", "邮件", "段落", "改这篇")
    english_vocabulary_signals = ("词汇DNA", "词汇 DNA", "存入词汇", "记单词", "背单词", "复习词汇", "单词")
    chinese_material_signals = ("素材库", "存入素材", "素材", "名言", "事例", "好句")
    chinese_classical_signals = ("文言文", "古诗", "古诗词", "诗词", "苏轼", "李白", "杜甫", "翻译", "作者心情")
    chinese_writing_signals = ("作文", "写作", "议论文", "记叙文", "说明文", "爆破思路", "提纲", "辩论")
    chinese_reading_signals = ("阅读理解", "现代文", "出题人", "答题", "丢分")

    def analyze(self, question: str, subject: str, stage: GuidanceStage) -> SubjectGuidanceStrategy | None:
        if subject == "数学":
            return self._math_strategy(question, stage)
        if subject == "英语":
            return self._english_strategy(question, stage)
        if subject == "语文":
            return self._chinese_strategy(question, stage)
        return None

    def is_math_practice_request(self, question: str) -> bool:
        normalized = question.strip()
        return any(signal in normalized for signal in self.practice_request_signals)

    def _math_strategy(self, question: str, stage: GuidanceStage) -> SubjectGuidanceStrategy:
        normalized = question.replace(" ", "")
        if any(signal.replace(" ", "") in normalized for signal in self.math_word_problem_signals):
            if stage == GuidanceStage.INITIAL:
                fallback = "先别急着列式。我们按数量关系三步来：识别量→说关系→转方程。你先把题目里变化的量和不变的量各圈出来。"
            elif stage == GuidanceStage.HINT:
                fallback = "现在把每个量之间的关系说成一句普通话，再把这句话翻译成方程。你先试着写出最核心的数量关系。"
            else:
                fallback = "我们收束一下：先设未知数，再列数量关系句，最后转成方程。最后一步求解和检验单位请你自己完成。"
            return SubjectGuidanceStrategy(
                subject="数学",
                teaching_mode=SubjectTeachingMode.MATH_WORD_PROBLEM,
                prompt_section=(
                    "数学专项引导策略：应用题必须先做建模，不直接替学生列方程。"
                    "请按“识别量→说关系→转方程”推进，让学生自己说出数量关系，再把自然语言转成数学语言。"
                ),
                fallback_text=fallback,
            )
        if any(signal in question for signal in self.concept_signals):
            return SubjectGuidanceStrategy(
                subject="数学",
                teaching_mode=SubjectTeachingMode.MATH_CONCEPT,
                prompt_section=(
                    "数学概念直觉模式：先用生活类比或图形直觉建立意义，再引入定义、公式或符号；"
                    "不要只让学生背结论，要追问这个定义解决了什么问题。"
                ),
                fallback_text="先不背公式。你先说说这个概念想解决什么问题，再举一个最简单的例子来检验理解。",
            )
        return SubjectGuidanceStrategy(
            subject="数学",
            teaching_mode=SubjectTeachingMode.MATH_PROBLEM,
            prompt_section=(
                "数学专项引导策略：先定位知识点和已知条件，再让学生选择方法。"
                "不要直接给计算结果；如果学生要同类题，优先给变式训练。"
            ),
            fallback_text="先把题目条件、目标结论和可能用到的知识点分开写。你先判断这题更像函数、几何、方程还是概率问题？",
        )

    def _english_strategy(self, question: str, stage: GuidanceStage) -> SubjectGuidanceStrategy:
        if any(signal in question for signal in self.english_vocabulary_signals):
            return SubjectGuidanceStrategy(
                subject="英语",
                teaching_mode=SubjectTeachingMode.ENGLISH_VOCABULARY,
                prompt_section=(
                    "英语词汇DNA模式：围绕词义、词性、例句和复习触发点帮助学生记词。"
                    "只在学生明确要求时记录词汇；不要承诺外部定时提醒。"
                ),
                fallback_text="先把这个词拆成词义、词性和一个你能自己造的例句。你先说说它在这句话里是什么词性？",
            )
        if any(signal in question for signal in self.english_writing_signals):
            return SubjectGuidanceStrategy(
                subject="英语",
                teaching_mode=SubjectTeachingMode.ENGLISH_WRITING,
                prompt_section=(
                    "英语写作专项：采用AI外教三维批改法，从语法、用词、逻辑中只抓最关键的2-3处。"
                    "不要代写整篇作文，用追问引导学生自己升级句式。"
                ),
                fallback_text="我们先不重写整篇。你先选一句最想提升的句子，我只从语法、用词、逻辑里挑最关键的一点让你自己改。",
            )
        return SubjectGuidanceStrategy(
            subject="英语",
            teaching_mode=SubjectTeachingMode.ENGLISH_GRAMMAR,
            prompt_section=(
                "英语语法追问教练：不要一次性改完所有错误。先定位最核心的1-2个语法点，"
                "用追问让学生自己发现主谓、时态、从句或搭配问题。"
            ),
            fallback_text="先看一个核心点：这句话的主语是谁，谓语动词应该跟主语保持什么形式？你先试着改第一处。",
        )

    def _chinese_strategy(self, question: str, stage: GuidanceStage) -> SubjectGuidanceStrategy:
        if any(signal in question for signal in self.chinese_material_signals):
            return SubjectGuidanceStrategy(
                subject="语文",
                teaching_mode=SubjectTeachingMode.CHINESE_MATERIAL,
                prompt_section=(
                    "语文素材库模式：帮助学生按主题、人物、适用文体整理素材，并在写作前主动匹配。"
                    "不要替学生写成完整作文。"
                ),
                fallback_text="先给这条素材打两个标签：它适合哪个主题？更适合议论文、记叙文还是说明文？",
            )
        if any(signal in question for signal in self.chinese_classical_signals):
            return SubjectGuidanceStrategy(
                subject="语文",
                teaching_mode=SubjectTeachingMode.CHINESE_CLASSICAL,
                prompt_section=(
                    "文言文复活模式：先还原作者处境、情感触发点和关键字词，再进入翻译或赏析。"
                    "让学生从“这个人在什么情境中说这句话”开始理解。"
                ),
                fallback_text="先别逐字翻译。你先判断作者当时处在什么处境、最强烈的情绪是什么，再看关键词为什么这样写。",
            )
        if any(signal in question for signal in self.chinese_writing_signals):
            return SubjectGuidanceStrategy(
                subject="语文",
                teaching_mode=SubjectTeachingMode.CHINESE_WRITING,
                prompt_section=(
                    "语文写作教练：按“爆破思路→检验逻辑→学生自己写→AI首读→精准提升”推进。"
                    "不代写作文，只激活学生自己的观点和材料。"
                ),
                fallback_text="先做思路爆破：围绕题目写出3个你真实相信的观点，再选一个最有证据支撑的观点展开。",
            )
        return SubjectGuidanceStrategy(
            subject="语文",
            teaching_mode=SubjectTeachingMode.CHINESE_READING,
            prompt_section=(
                "阅读理解拆解模式：从出题人视角分析题目，先判断考点，再回到文本找依据。"
                "不要直接给标准答案，训练学生识别常见失分坑。"
            ),
            fallback_text="先从出题人视角看：这题到底在考概括、赏析、作用，还是人物/情感理解？你先选一个考点。",
        )


subject_guidance_service = SubjectGuidanceService()
