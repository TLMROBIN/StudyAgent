from pathlib import Path

from backend.models.conversation import GuidanceStage
from backend.services.socratic_service import socratic_service
from backend.services.subject_guidance_service import subject_guidance_service


def test_guidance_stage_progression():
    assert socratic_service.infer_stage(0) == GuidanceStage.INITIAL
    assert socratic_service.infer_stage(1) == GuidanceStage.HINT
    assert socratic_service.infer_stage(3) == GuidanceStage.FALLBACK


def test_fallback_text_never_contains_final_answer_phrase():
    text = socratic_service.build_fallback_text("求函数最值", "数学", GuidanceStage.FALLBACK, "calculation")
    assert "最终答案" not in text
    assert "标准答案" not in text


def test_build_prompt_adds_latex_instruction_for_stem_subjects():
    prompt = socratic_service.build_prompt(
        question="已知加速度 a 和时间 t，求位移",
        subject="物理",
        history=[],
        retrieved_context="",
        system_prompt="",
        student_grade=2,
    )
    system_text = prompt.messages[0]["content"]

    assert "标准 LaTeX" in system_text
    assert "$...$" in system_text
    assert "$$...$$" in system_text


def test_build_prompt_deduplicates_base_system_prompt():
    prompt = socratic_service.build_prompt(
        question="什么是加速度",
        subject="物理",
        history=[],
        retrieved_context="",
        system_prompt=f"  {socratic_service.base_prompt}\n",
    )

    assert prompt.messages[0]["content"].count(socratic_service.base_prompt) == 1


def test_build_prompt_adds_practice_review_context():
    prompt = socratic_service.build_prompt(
        question="选B",
        subject="数学",
        history=[],
        retrieved_context="",
        system_prompt="",
        practice_context={
            "question_text": "已知 $2x+3=11$，求 $x$。",
            "answer_text": "4",
            "explanation_text": "两边先减 3，再除以 2。",
            "source": "generated",
            "issued_turn_index": 1,
        },
    )
    system_text = prompt.messages[0]["content"]

    assert "判卷模式" in system_text
    assert "已知 $2x+3=11$，求 $x$。" in system_text
    assert "参考答案：4" in system_text
    assert "两边先减 3，再除以 2。" in system_text


def test_build_prompt_does_not_force_disclaimer_for_high_confidence_image_turn():
    prompt = socratic_service.build_prompt(
        question="请看这张图",
        subject="物理",
        history=[],
        retrieved_context="",
        system_prompt="",
        image_summary="图中给出了受力分析示意。",
        image_confidence="high",
        image_related=True,
    )
    system_text = prompt.messages[0]["content"]

    assert "每一轮回复都必须明确说明" not in system_text
    assert "看错图片" not in system_text


def test_build_prompt_requires_grounding_on_image_summary():
    prompt = socratic_service.build_prompt(
        question="请看图",
        subject="物理",
        history=[],
        retrieved_context="",
        system_prompt="",
        image_summary="匀强电场、匀强磁场、带电微粒沿直线运动。",
        image_confidence="high",
        image_related=True,
    )
    system_text = prompt.messages[0]["content"]

    assert "必须先引用图片理解摘要中的1-2个具体关键词" in system_text
    assert "匀强电场、匀强磁场、带电微粒沿直线运动" in system_text


def test_medium_confidence_image_prompt_requires_uncertainty_confirmation():
    prompt = socratic_service.build_prompt(
        question="请看图",
        subject="数学",
        history=[],
        retrieved_context="",
        system_prompt="",
        image_summary="题干：求函数单调区间。不确定处：第2问条件可能是 a≥1",
        image_confidence="medium",
        image_related=True,
        image_uncertainties=["第2问条件可能是 a≥1"],
    )
    system_text = prompt.messages[0]["content"]

    assert "先用1-2句复述识别到的题干要点" in system_text
    assert "向学生确认" in system_text
    assert "第2问条件可能是 a≥1" in system_text


def test_basic_concept_question_uses_explanation_mode():
    prompt = socratic_service.build_prompt(
        question="什么是函数单调性",
        subject="数学",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    system_text = prompt.messages[0]["content"]

    assert "问题类型：concept_explanation" in system_text
    assert "2-4 句解释基础概念" in system_text
    assert "1 个检查理解的问题" in system_text


def test_physics_calculation_prompt_uses_diagram_first_strategy():
    prompt = socratic_service.build_prompt(
        question="如图所示，物块在斜面上匀速下滑，求摩擦力",
        subject="物理",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    system_text = prompt.messages[0]["content"]

    assert "物理专项引导策略" in system_text
    assert "受力分析图" in system_text
    assert "先画" in prompt.fallback_text
    assert "先把已知条件和要求的量分别列出来" not in prompt.fallback_text


def test_physics_concept_prompt_uses_intuition_before_formula():
    prompt = socratic_service.build_prompt(
        question="为什么压强和受力面积有关？",
        subject="物理",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    system_text = prompt.messages[0]["content"]

    assert "物理概念直觉模式" in system_text
    assert "生活经验" in system_text
    assert "实验想象" in system_text
    assert "公式" in system_text


def test_math_word_problem_prompt_uses_modeling_sequence():
    prompt = socratic_service.build_prompt(
        question="应用题读了三遍还是不知道怎么设 x，行程相遇问题怎么列方程？",
        subject="数学",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    system_text = prompt.messages[0]["content"]

    assert "数学专项引导策略" in system_text
    assert "识别量→说关系→转方程" in system_text
    assert "数量关系" in prompt.fallback_text


def test_english_text_prompt_routes_grammar_writing_and_vocabulary_modes():
    grammar_prompt = socratic_service.build_prompt(
        question="I goes to school every day，帮我检查语法",
        subject="英语",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    writing_prompt = socratic_service.build_prompt(
        question="帮我批改这篇英语作文，看看句式怎么升级",
        subject="英语",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    vocabulary_prompt = socratic_service.build_prompt(
        question="把 photosynthesis 存入词汇DNA，之后提醒我复习",
        subject="英语",
        history=[],
        retrieved_context="",
        system_prompt="",
    )

    assert "英语语法追问教练" in grammar_prompt.messages[0]["content"]
    assert "AI外教三维批改法" in writing_prompt.messages[0]["content"]
    assert "词汇DNA" in vocabulary_prompt.messages[0]["content"]


def test_chinese_prompt_routes_reading_writing_classical_and_material_modes():
    reading_prompt = socratic_service.build_prompt(
        question="这道现代文阅读理解为什么我总是答不到点？",
        subject="语文",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    writing_prompt = socratic_service.build_prompt(
        question="帮我爆破作文思路，题目是成长",
        subject="语文",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    classical_prompt = socratic_service.build_prompt(
        question="这首古诗的作者心情是什么，文言文背景也看不懂",
        subject="语文",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    material_prompt = socratic_service.build_prompt(
        question="存入素材库：关于坚持的名言和事例",
        subject="语文",
        history=[],
        retrieved_context="",
        system_prompt="",
    )

    assert "出题人视角" in reading_prompt.messages[0]["content"]
    assert "语文写作教练" in writing_prompt.messages[0]["content"]
    assert "文言文复活" in classical_prompt.messages[0]["content"]
    assert "语文素材库" in material_prompt.messages[0]["content"]


def test_exercise_question_stays_guided_even_with_answer_language():
    prompt = socratic_service.build_prompt(
        question="这道题答案是多少",
        subject="数学",
        history=[],
        retrieved_context="",
        system_prompt="",
    )
    system_text = prompt.messages[0]["content"]

    assert "问题类型：calculation" in system_text
    assert "基础知识解释模式" not in system_text


def test_image_low_confidence_text_asks_user_to_correct_partial_understanding():
    text = socratic_service.image_low_confidence_text("数学", image_summary="像是一道函数图像题。")

    assert "看得不太清" in text
    assert "理解可能有误" in text
    assert "像是一道函数图像题" in text
    assert "纠正" in text


def test_image_extremely_low_confidence_text_reports_recognition_failure():
    text = socratic_service.image_low_confidence_text("数学")

    assert "识别失败" in text
    assert "重新上传" in text


def test_prompt_limits_questions_per_turn_default():
    package = socratic_service.build_prompt("什么是加速度", "物理", [], "", "")

    assert "最多提出 2 个引导问题" in package.messages[0]["content"]


def test_prompt_limits_questions_per_turn_configurable():
    package = socratic_service.build_prompt(
        "什么是加速度",
        "物理",
        [],
        "",
        "",
        guidance_params={"max_questions_per_turn": 1},
    )

    assert "最多提出 1 个引导问题" in package.messages[0]["content"]


def test_chat_router_passes_active_guidance_params_to_prompt():
    source = Path("backend/routers/chat.py").read_text()

    assert "guidance_params=active_config.guidance_params if active_config else None" in source


def test_default_agent_config_sets_question_limit():
    source = Path("backend/main.py").read_text()

    assert '"max_questions_per_turn": 2' in source


def test_fact_mode_gate_blocks_disguised_exercises():
    blocked = [
        "什么是使 x²+3x+2=0 的 x 的值",
        "三角形内角和是否可能为 200°，如图",
        "下列哪个正确（A）光合作用…（B）…",
        "求光合作用的定义",
        "第3题的这个概念是什么意思",
    ]
    for question in blocked:
        assert socratic_service.fact_mode_eligible(question) is False, question


def test_fact_mode_gate_allows_plain_facts():
    allowed = ["三溴苯酚是否部分溶于水", "什么是光合作用", "定语从句和同位语从句的区别"]
    for question in allowed:
        assert socratic_service.fact_mode_eligible(question) is True, question


def test_fact_mode_gate_vetoes_image_turns():
    assert socratic_service.fact_mode_eligible("什么是光合作用", image_related=True) is False


def test_mode_instruction_only_injected_when_eligible():
    fact_prompt = socratic_service.build_prompt("什么是光合作用", "生物", [], "", "")
    guide_prompt = socratic_service.build_prompt("求 x²+3x+2=0 的解", "数学", [], "", "")

    assert "<mode>" in fact_prompt.messages[0]["content"]
    assert fact_prompt.fact_mode_offered is True
    assert "<mode>" not in guide_prompt.messages[0]["content"]
    assert guide_prompt.fact_mode_offered is False


# --- 新增 5 科专项引导策略 ---


def test_chemistry_equation_and_experiment_modes():
    equation = socratic_service.build_prompt(
        "帮我配平这个化学方程式", "化学", [], "", ""
    )
    experiment = socratic_service.build_prompt(
        "这个实验加碱后出现沉淀是什么现象", "化学", [], "", ""
    )

    assert "守恒" in equation.messages[0]["content"]
    assert "现象" in experiment.messages[0]["content"]


def test_chemistry_falls_back_to_representation_mode():
    prompt = socratic_service.build_prompt("这道化学题不会", "化学", [], "", "")

    assert "三重表征" in prompt.messages[0]["content"]


def test_biology_experiment_and_process_modes():
    experiment = socratic_service.build_prompt(
        "怎么设计对照实验找自变量", "生物", [], "", ""
    )
    process = socratic_service.build_prompt(
        "光合作用的过程是怎样的", "生物", [], "", ""
    )

    assert "单一变量" in experiment.messages[0]["content"]
    assert "输入→环节→输出" in process.messages[0]["content"]


def test_history_source_and_causation_modes():
    source = socratic_service.build_prompt(
        "根据材料分析这段史料说明了什么", "历史", [], "", ""
    )
    causation = socratic_service.build_prompt(
        "分析安史之乱的原因和影响", "历史", [], "", ""
    )

    assert "论从史出" in source.messages[0]["content"]
    assert "多角度" in causation.messages[0]["content"]


def test_geography_chart_and_location_modes():
    chart = socratic_service.build_prompt(
        "如何判读这张等高线图", "地理", [], "", ""
    )
    location = socratic_service.build_prompt(
        "分析这个工业区的区位条件", "地理", [], "", ""
    )

    assert "图例" in chart.messages[0]["content"]
    assert "区位" in location.messages[0]["content"]


def test_politics_material_and_debate_modes():
    material = socratic_service.build_prompt(
        "结合材料说明如何体现哲学原理", "政治", [], "", ""
    )
    debate = socratic_service.build_prompt(
        "辨析这个观点是否正确", "政治", [], "", ""
    )

    assert "原理" in material.messages[0]["content"]
    assert "片面" in debate.messages[0]["content"]


def test_new_subject_fallback_preserves_no_answer_constraint():
    for subject, question in [
        ("化学", "帮我配平方程式"),
        ("生物", "光合作用过程"),
        ("历史", "根据材料分析"),
        ("地理", "判读气候图"),
        ("政治", "辨析这个观点"),
    ]:
        prompt = socratic_service.build_prompt(
            question, subject, [], "", "", guidance_params=None
        )
        text = prompt.fallback_text
        assert "标准答案" not in text
        assert "最终答案" not in text
        # FALLBACK 阶段保留最后一步
        fallback = subject_guidance_service.analyze(question, subject, GuidanceStage.FALLBACK)
        assert fallback is not None
        assert "自己" in fallback.fallback_text


def test_unregistered_subject_returns_none():
    assert subject_guidance_service.analyze("随便问问", "体育", GuidanceStage.INITIAL) is None
    assert subject_guidance_service.analyze("", "化学", GuidanceStage.INITIAL) is not None


# --- 数学/英语增量强化分支 ---


def test_math_error_review_and_multi_solution_modes():
    error_review = socratic_service.build_prompt(
        "这道题我又错了，为什么错", "数学", [], "", ""
    )
    multi_solution = socratic_service.build_prompt(
        "这道题还有别的方法吗", "数学", [], "", ""
    )

    assert "错因归类" in error_review.messages[0]["content"]
    assert "一题多解" in multi_solution.messages[0]["content"]


def test_english_reading_mode():
    prompt = socratic_service.build_prompt(
        "这道阅读理解的推断题怎么做", "英语", [], "", ""
    )

    assert "排除法" in prompt.messages[0]["content"]


# --- 配置合并语义 ---


def test_effective_guidance_params_merge_semantics():
    fn = socratic_service._effective_guidance_params
    assert fn(None, "数学") == {}
    assert fn({"max_questions_per_turn": 2}, "数学") == {"max_questions_per_turn": 2}
    # 仅 by_subject
    merged = fn({"by_subject": {"英语": {"max_questions_per_turn": 3}}}, "英语")
    assert merged["max_questions_per_turn"] == 3
    # 全局 + by_subject 覆盖，且 by_subject 键不泄漏
    both = fn(
        {"max_questions_per_turn": 1, "by_subject": {"英语": {"max_questions_per_turn": 3}}},
        "英语",
    )
    assert both == {"max_questions_per_turn": 3}
    # 未命中学科退回全局
    other = fn(
        {"max_questions_per_turn": 1, "by_subject": {"英语": {"max_questions_per_turn": 3}}},
        "数学",
    )
    assert other == {"max_questions_per_turn": 1}


def test_per_subject_max_questions_override_applies():
    package = socratic_service.build_prompt(
        "什么是加速度",
        "物理",
        [],
        "",
        "",
        guidance_params={"by_subject": {"物理": {"max_questions_per_turn": 1}}},
    )

    assert "最多提出 1 个引导问题" in package.messages[0]["content"]


def test_subject_supplement_injected_after_base_prompt():
    package = socratic_service.build_prompt(
        "什么是加速度",
        "物理",
        [],
        "",
        "全局提示词",
        subject_supplement="物理科追加提示：优先受力分析。",
    )
    system_text = package.messages[0]["content"]

    assert "物理科追加提示：优先受力分析。" in system_text
    # 硬约束仍在最前
    assert system_text.index(socratic_service.base_prompt) < system_text.index("物理科追加提示")
    # 补充段位于当前学科段之前
    assert system_text.index("物理科追加提示") < system_text.index("当前学科：")


def test_chat_router_passes_subject_supplement_and_keeps_guidance_params():
    source = Path("backend/routers/chat.py").read_text()

    assert "subject_supplement=subject_supplement" in source
    assert "guidance_params=active_config.guidance_params if active_config else None" in source


# --- 数/英/语声明式化 + 阶段化兜底 ---


def test_math_function_and_geometry_modes():
    function_prompt = socratic_service.build_prompt(
        "求这个函数的单调区间和最值", "数学", [], "", ""
    )
    geometry_prompt = socratic_service.build_prompt(
        "这道几何题怎么证明两个三角形全等", "数学", [], "", ""
    )

    assert "数学函数专项模式" in function_prompt.messages[0]["content"]
    assert "图像" in function_prompt.messages[0]["content"]
    assert "数学几何证明模式" in geometry_prompt.messages[0]["content"]
    assert "辅助线" in geometry_prompt.messages[0]["content"]


def test_math_concept_still_wins_over_function_rule():
    # 概念信号优先级高于函数细分规则（迁移保持原判定顺序）
    strategy = subject_guidance_service.analyze("什么是函数单调性", "数学", GuidanceStage.INITIAL)

    assert strategy is not None
    assert strategy.teaching_mode.value == "math_concept"


def test_migrated_subjects_have_stage_differentiated_fallbacks():
    cases = [
        ("数学", "这道题我又错了，为什么错"),
        ("数学", "求这个函数的最值"),
        ("英语", "这道阅读理解的推断题怎么做"),
        ("英语", "帮我批改这篇作文"),
        ("语文", "这首古诗的翻译"),
        ("语文", "现代文阅读怎么答"),
    ]
    for subject, question in cases:
        texts = {
            stage: subject_guidance_service.analyze(question, subject, stage).fallback_text
            for stage in (GuidanceStage.INITIAL, GuidanceStage.HINT, GuidanceStage.FALLBACK)
        }
        assert len(set(texts.values())) == 3, f"{subject}:{question} 三阶段文案应互不相同"


def test_migrated_subjects_fallback_stage_keeps_last_step_for_student():
    cases = [
        ("数学", "解这个方程组"),
        ("数学", "应用题不会列方程"),
        ("数学", "这道题我又错了"),
        ("数学", "还有别的方法吗"),
        ("数学", "什么是导数的定义"),
        ("英语", "I goes to school 怎么改"),
        ("英语", "帮我批改这篇作文"),
        ("英语", "这道阅读理解怎么做"),
        ("英语", "帮我记这个单词"),
        ("语文", "现代文阅读怎么答"),
        ("语文", "帮我爆破作文思路"),
        ("语文", "这段文言文的翻译"),
        ("语文", "存入素材库这条名言"),
    ]
    for subject, question in cases:
        strategy = subject_guidance_service.analyze(question, subject, GuidanceStage.FALLBACK)
        assert strategy is not None
        text = strategy.fallback_text
        assert "自己" in text, f"{subject}:{question} FALLBACK 应保留最后一步给学生"
        assert "最终答案" not in text
        assert "标准答案" not in text


def test_matched_by_trigger_marks_catch_all_rules():
    triggered = subject_guidance_service.analyze("帮我配平这个化学方程式", "化学", GuidanceStage.INITIAL)
    catch_all = subject_guidance_service.analyze("这道化学题不会", "化学", GuidanceStage.INITIAL)
    math_triggered = subject_guidance_service.analyze("求函数最值", "数学", GuidanceStage.INITIAL)
    math_catch_all = subject_guidance_service.analyze("这道题不会做", "数学", GuidanceStage.INITIAL)

    assert triggered.matched_by_trigger is True
    assert catch_all.matched_by_trigger is False
    assert math_triggered.matched_by_trigger is True
    assert math_catch_all.matched_by_trigger is False
