"""可配置的过滤规则引擎。

StudyAgent 的三层过滤（输入黑名单 / 学科白名单 / LLM 输出校验）统一从
JSON 配置文件加载规则，默认路径为 ``backend/data/filter_rules.json``，
可用环境变量 ``FILTER_RULES_PATH`` 覆盖。

Fail-closed 原则：
- 代码内置 ``DEFAULT_CONFIG`` 是完整规则集保底（与出厂配置文件语义一致）。
- 配置文件缺失、JSON 损坏、schema 非法或任一 regex 编译失败时，**整体**
  回退到内置默认规则并记录日志——绝不会因配置问题导致过滤失效，
  解析失败宁可更严（回退完整保底集）不能更松（跳过规则）。

运行时重载：
- 每次取快照时按 mtime/size 轻量检测配置文件变化（默认 1s 节流）。
- ``reload()`` 强制立即重载，供管理端点调用。
"""

from __future__ import annotations

import json
import logging
import re
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

LAYER_QUESTION_BLOCKLIST = "question_blocklist"
LAYER_DIRECT_ANSWER = "direct_answer"
LAYER_IMAGE_UNCERTAINTY = "image_uncertainty"
VALID_LAYERS = {LAYER_QUESTION_BLOCKLIST, LAYER_DIRECT_ANSWER, LAYER_IMAGE_UNCERTAINTY}
VALID_RULE_TYPES = {"regex", "keyword"}

SOURCE_CONFIG_FILE = "config_file"
SOURCE_BUILTIN_DEFAULT = "builtin_default"

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "data" / "filter_rules.json"
CONFIG_PATH_ENV_VAR = "FILTER_RULES_PATH"


# 内置默认规则集（保底）。与出厂配置文件 backend/data/filter_rules.json 保持一致；
# 修改规则时请同步两处（tests/test_filter_rules_config.py 会校验一致性）。
DEFAULT_CONFIG: dict = {
    "version": 1,
    "description": (
        "StudyAgent 三层过滤规则配置（输入黑名单 question_blocklist / 学科白名单 subjects / "
        "LLM 输出校验 direct_answer、image_uncertainty）。"
        "本文件缺失或损坏时，服务自动回退到代码内置默认规则（fail-closed），"
        "见 backend/services/filter_rule_engine.py 的 DEFAULT_CONFIG。"
    ),
    "subjects": {
        "语文": ["古诗", "文言文", "修辞", "阅读理解", "作文", "病句", "成语", "议论文", "论点", "意象"],
        "数学": ["函数", "导数", "数列", "几何", "概率", "方程", "不等式", "三角", "圆锥曲线"],
        "英语": ["语法", "完形", "阅读", "时态", "从句", "单词", "翻译"],
        "物理": ["速度", "受力", "电场", "磁场", "动能", "牛顿", "电路"],
        "化学": ["氧化", "还原", "离子", "方程式", "滴定", "平衡", "电解"],
        "生物": ["细胞", "遗传", "呼吸作用", "光合作用", "生态", "DNA"],
        "政治": ["哲学", "经济", "法治", "文化", "价值观", "国家"],
        "历史": ["朝代", "改革", "战争", "制度", "史料", "近代史", "历史", "材料题"],
        "地理": ["气候", "洋流", "农业", "地形", "人口", "区域"],
    },
    "generic_academic": {
        "min_length": 6,
        "tokens": ["为什么", "如何", "怎么", "求", "分析", "解释", "评价", "概括"],
    },
    "rules": [
        # ---- 输入黑名单（迁移自原 NON_SUBJECT_PATTERNS）----
        {
            "id": "input.offtopic.daily_life",
            "layer": LAYER_QUESTION_BLOCKLIST,
            "type": "regex",
            "pattern": r"天气|股价|彩票|恋爱|表白|游戏攻略|写代码",
            "enabled": True,
            "description": "非学科日常话题（迁移自硬编码规则）",
        },
        {
            "id": "input.jailbreak.persona_zh",
            "layer": LAYER_QUESTION_BLOCKLIST,
            "type": "regex",
            "pattern": r"你是谁|讲个笑话|闲聊|角色扮演|扮演成|忽略之前|无视规则|系统提示词|开发者模式|管理员模式|DAN",
            "enabled": True,
            "description": "中文越狱/角色扮演/闲聊（迁移自硬编码规则）",
        },
        {
            "id": "input.jailbreak.prompt_leak",
            "layer": LAYER_QUESTION_BLOCKLIST,
            "type": "regex",
            "pattern": r"prompt|system prompt|越过限制|绕过过滤|不要遵守|泄露提示词",
            "enabled": True,
            "description": "提示词泄露/绕过过滤（迁移自硬编码规则）",
        },
        {
            "id": "input.rag_leak.retrieval_dump",
            "layer": LAYER_QUESTION_BLOCKLIST,
            "type": "regex",
            "pattern": r"知识库原文|检索片段|资料片段|向量库|RAG|完整输出资料",
            "enabled": True,
            "description": "套取 RAG 检索原文（迁移自硬编码规则）",
        },
        {
            "id": "input.safety.sensitive",
            "layer": LAYER_QUESTION_BLOCKLIST,
            "type": "regex",
            "pattern": r"身份证|银行卡|密码|越狱|翻墙|成人",
            "enabled": True,
            "description": "敏感/违规内容（迁移自硬编码规则）",
        },
        # ---- 输入黑名单（新增：越狱 / prompt injection 变体）----
        {
            "id": "input.jailbreak.ignore_variants",
            "layer": LAYER_QUESTION_BLOCKLIST,
            "type": "regex",
            "pattern": r"(忽略|无视)(之前|以上|上面|前面)",
            "enabled": True,
            "description": "“忽略以上/无视上面”类指令覆盖变体（原规则只覆盖“忽略之前/无视规则”）。故意不匹配“忽略空气阻力/忽略所有摩擦”等物理表述",
        },
        {
            "id": "input.jailbreak.english_injection",
            "layer": LAYER_QUESTION_BLOCKLIST,
            "type": "regex",
            "pattern": r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|rules?)|disregard\s+(all\s+)?(the\s+)?(previous|above)|developer\s+mode|do\s+anything\s+now|jailbreak",
            "enabled": True,
            "description": "英文 prompt injection（ignore previous instructions / developer mode / DAN 全称等）",
        },
        {
            "id": "input.jailbreak.unrestricted_persona",
            "layer": LAYER_QUESTION_BLOCKLIST,
            "type": "regex",
            "pattern": r"你(现在)?(没有|不受)任何限制|(没有|解除|摆脱)(任何|规则|道德|安全)?(限制|约束)的\s*(模型|AI|助手|老师|机器人)|解除.{0,6}安全(策略|限制)|无限制模式",
            "enabled": True,
            "description": "“无限制人格”类越狱（假装成没有规则限制的 AI 等）。刻意收窄以避免误伤政治学科“权力不受限制”类正常提问",
        },
        {
            "id": "input.prompt_leak.variants",
            "layer": LAYER_QUESTION_BLOCKLIST,
            "type": "regex",
            "pattern": r"初始指令|原始指令|第一条消息|收到的(全部|所有)?指令|原样输出|一字不差|initial\s+instructions?",
            "enabled": True,
            "description": "提示词/指令泄露变体（要求复述系统收到的指令或原样输出内部内容）",
        },
        {
            "id": "input.injection.markers",
            "layer": LAYER_QUESTION_BLOCKLIST,
            "type": "regex",
            "pattern": r"<<\s*/?\s*sys\s*>>|\[/?system\]|\[/?inst\]|<\|im_start\|>|base64|解码后(执行|运行)",
            "enabled": True,
            "description": "注入标记（模型特殊分隔符、base64 编码绕过等）",
        },
        {
            "id": "input.bypass.skip_guidance",
            "layer": LAYER_QUESTION_BLOCKLIST,
            "type": "regex",
            "pattern": r"不[用要需]引导|跳过引导|直接(告诉我|给我?|说)(标准|最终|正确)?答案|答题机器",
            "enabled": True,
            "description": "绕过苏格拉底式引导、索要直接答案",
        },
        # ---- LLM 输出校验：直接给答案（迁移自原 DIRECT_ANSWER_PATTERNS）----
        {
            "id": "output.direct.final_answer",
            "layer": LAYER_DIRECT_ANSWER,
            "type": "regex",
            "pattern": r"最终答案[是为]",
            "enabled": True,
            "description": "直接给出最终答案（迁移自硬编码规则）",
        },
        {
            "id": "output.direct.standard_answer",
            "layer": LAYER_DIRECT_ANSWER,
            "type": "regex",
            "pattern": r"标准答案[是为]",
            "enabled": True,
            "description": "直接给出标准答案（迁移自硬编码规则）",
        },
        {
            "id": "output.direct.answer_colon_option",
            "layer": LAYER_DIRECT_ANSWER,
            "type": "regex",
            "pattern": r"答案[：:]\s*[A-D0-9一二三四五六七八九十]",
            "enabled": True,
            "description": "“答案：A/42”形式（迁移自硬编码规则）",
        },
        {
            "id": "output.direct.conclusion_result",
            "layer": LAYER_DIRECT_ANSWER,
            "type": "regex",
            "pattern": r"所以结果[是为]",
            "enabled": True,
            "description": "“所以结果是”形式（迁移自硬编码规则）",
        },
        {
            "id": "output.direct.correct_conclusion",
            "layer": LAYER_DIRECT_ANSWER,
            "type": "regex",
            "pattern": r"正确结论[是为]",
            "enabled": True,
            "description": "“正确结论是”形式（迁移自硬编码规则）",
        },
        # ---- LLM 输出校验：数学计算直接给出最终数值答案（新增，可按需 enabled=false 关闭）----
        {
            "id": "output.direct.numeric_answer",
            "layer": LAYER_DIRECT_ANSWER,
            "type": "regex",
            "pattern": r"答案(?:就)?[是为][:：]?\s*[-+]?\d+(?:\.\d+)?",
            "enabled": True,
            "description": "数学计算直接给出最终数值答案，如“答案是 42”。可关闭：设 enabled=false",
        },
        {
            "id": "output.direct.final_numeric_equation",
            "layer": LAYER_DIRECT_ANSWER,
            "type": "regex",
            "pattern": r"(?:所以|因此|故|综上|最终)[^\n。；;]{0,15}[=≈]\s*[-+]?\d+(?:\.\d+)?",
            "enabled": True,
            "description": "结论句直接给出数值结果，如“所以 W = 42”。可关闭：设 enabled=false",
        },
        {
            "id": "output.direct.trailing_numeric_result",
            "layer": LAYER_DIRECT_ANSWER,
            "type": "regex",
            "pattern": r"[=≈]\s*[-+]?\d+(?:\.\d+)?\s*[a-zA-Zμ°Ω%][a-zA-Z0-9/·^²³]*\s*[。.!！]?\s*$",
            "enabled": True,
            "description": "答案末尾裸数值等式兜底，如“= 42 J”。兜底模式，可能误伤“已知 g = 9.8 m/s²”这类常量声明结尾，误报偏高时可设 enabled=false 关闭",
        },
        # ---- LLM 输出校验：图片理解不确定性声明（迁移自原 IMAGE_DISCLAIMER_UNCERTAINTY_PATTERNS）----
        {
            "id": "image.uncertainty.maybe_inaccurate",
            "layer": LAYER_IMAGE_UNCERTAINTY,
            "type": "keyword",
            "pattern": "可能不准确",
            "enabled": True,
            "description": "图片理解不确定性声明（迁移自硬编码规则）",
        },
        {
            "id": "image.uncertainty.not_necessarily_accurate",
            "layer": LAYER_IMAGE_UNCERTAINTY,
            "type": "keyword",
            "pattern": "不一定准确",
            "enabled": True,
            "description": "图片理解不确定性声明（迁移自硬编码规则）",
        },
        {
            "id": "image.uncertainty.hard_to_see",
            "layer": LAYER_IMAGE_UNCERTAINTY,
            "type": "keyword",
            "pattern": "看得不太清",
            "enabled": True,
            "description": "图片理解不确定性声明（迁移自硬编码规则）",
        },
        {
            "id": "image.uncertainty.maybe_misread",
            "layer": LAYER_IMAGE_UNCERTAINTY,
            "type": "keyword",
            "pattern": "理解可能有误",
            "enabled": True,
            "description": "图片理解不确定性声明（迁移自硬编码规则）",
        },
        {
            "id": "image.uncertainty.recognition_failed",
            "layer": LAYER_IMAGE_UNCERTAINTY,
            "type": "keyword",
            "pattern": "识别失败",
            "enabled": True,
            "description": "图片理解不确定性声明（迁移自硬编码规则）",
        },
        {
            "id": "image.uncertainty.maybe_not_fully_accurate",
            "layer": LAYER_IMAGE_UNCERTAINTY,
            "type": "keyword",
            "pattern": "可能理解得不完全准确",
            "enabled": True,
            "description": "图片理解不确定性声明（迁移自硬编码规则）",
        },
    ],
}


@dataclass(frozen=True)
class CompiledRule:
    id: str
    layer: str
    type: str
    raw_pattern: str
    pattern: re.Pattern
    description: str = ""


@dataclass(frozen=True)
class RuleSnapshot:
    source: str
    config_path: str
    loaded_at: str
    error: str | None
    subjects: dict[str, tuple[str, ...]]
    generic_tokens: tuple[str, ...]
    generic_min_length: int
    rules_by_layer: dict[str, tuple[CompiledRule, ...]] = field(default_factory=dict)
    disabled_rule_ids: tuple[str, ...] = ()

    @property
    def enabled_rule_count(self) -> int:
        return sum(len(rules) for rules in self.rules_by_layer.values())

    @property
    def enabled_rule_ids(self) -> tuple[str, ...]:
        return tuple(rule.id for rules in self.rules_by_layer.values() for rule in rules)

    def layer_rules(self, layer: str) -> tuple[CompiledRule, ...]:
        return self.rules_by_layer.get(layer, ())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _compile_rule(entry: object) -> tuple[CompiledRule | None, str]:
    """校验并编译一条规则，返回 (rule, rule_id)。禁用规则返回 (None, rule_id)。"""
    _require(isinstance(entry, dict), f"rule entry must be an object, got {type(entry).__name__}")
    rule_id = entry.get("id")
    _require(isinstance(rule_id, str) and rule_id.strip() != "", "rule id must be a non-empty string")
    layer = entry.get("layer")
    _require(layer in VALID_LAYERS, f"rule {rule_id}: invalid layer {layer!r}, expected one of {sorted(VALID_LAYERS)}")
    rule_type = entry.get("type")
    _require(rule_type in VALID_RULE_TYPES, f"rule {rule_id}: invalid type {rule_type!r}, expected one of {sorted(VALID_RULE_TYPES)}")
    raw_pattern = entry.get("pattern")
    _require(isinstance(raw_pattern, str) and raw_pattern != "", f"rule {rule_id}: pattern must be a non-empty string")
    enabled = entry.get("enabled", True)
    _require(isinstance(enabled, bool), f"rule {rule_id}: enabled must be a boolean")
    description = entry.get("description", "")
    _require(isinstance(description, str), f"rule {rule_id}: description must be a string")
    if not enabled:
        return None, rule_id
    try:
        if rule_type == "keyword":
            compiled = re.compile(re.escape(raw_pattern), re.IGNORECASE)
        else:
            compiled = re.compile(raw_pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"rule {rule_id}: invalid regex pattern: {exc}") from exc
    return CompiledRule(id=rule_id, layer=layer, type=rule_type, raw_pattern=raw_pattern, pattern=compiled, description=description), rule_id


def compile_config(raw: object, *, source: str, config_path: str, error: str | None = None) -> RuleSnapshot:
    """校验配置并编译为不可变快照；任何 schema/regex 问题抛出 ValueError。"""
    _require(isinstance(raw, dict), "config root must be a JSON object")

    subjects_raw = raw.get("subjects")
    _require(isinstance(subjects_raw, dict) and subjects_raw, "subjects must be a non-empty object")
    subjects: dict[str, tuple[str, ...]] = {}
    for subject, keywords in subjects_raw.items():
        _require(isinstance(subject, str) and subject.strip() != "", "subject name must be a non-empty string")
        _require(
            isinstance(keywords, list) and keywords and all(isinstance(item, str) and item for item in keywords),
            f"subjects[{subject}] must be a non-empty list of non-empty strings",
        )
        subjects[subject] = tuple(keywords)

    generic_raw = raw.get("generic_academic")
    _require(isinstance(generic_raw, dict), "generic_academic must be an object")
    min_length = generic_raw.get("min_length")
    _require(isinstance(min_length, int) and not isinstance(min_length, bool) and min_length >= 1, "generic_academic.min_length must be an integer >= 1")
    tokens_raw = generic_raw.get("tokens")
    _require(
        isinstance(tokens_raw, list) and all(isinstance(item, str) and item for item in tokens_raw),
        "generic_academic.tokens must be a list of non-empty strings",
    )

    rules_raw = raw.get("rules")
    _require(isinstance(rules_raw, list) and rules_raw, "rules must be a non-empty list")
    rules_by_layer: dict[str, list[CompiledRule]] = {layer: [] for layer in VALID_LAYERS}
    disabled_ids: list[str] = []
    seen_ids: set[str] = set()
    for entry in rules_raw:
        rule, rule_id = _compile_rule(entry)
        _require(rule_id not in seen_ids, f"duplicate rule id: {rule_id}")
        seen_ids.add(rule_id)
        if rule is None:
            disabled_ids.append(rule_id)
        else:
            rules_by_layer[rule.layer].append(rule)

    return RuleSnapshot(
        source=source,
        config_path=config_path,
        loaded_at=datetime.now(UTC).isoformat(),
        error=error,
        subjects=subjects,
        generic_tokens=tuple(tokens_raw),
        generic_min_length=min_length,
        rules_by_layer={layer: tuple(rules) for layer, rules in rules_by_layer.items()},
        disabled_rule_ids=tuple(disabled_ids),
    )


def _builtin_snapshot(config_path: str, error: str | None) -> RuleSnapshot:
    """编译内置默认规则集。DEFAULT_CONFIG 由测试保证永远可编译。"""
    return compile_config(DEFAULT_CONFIG, source=SOURCE_BUILTIN_DEFAULT, config_path=config_path, error=error)


class FilterRuleEngine:
    """从配置文件加载过滤规则，支持 mtime 自动重载与显式 reload；失败回退内置默认。"""

    def __init__(self, config_path: str | os.PathLike | None = None, *, check_interval_seconds: float = 1.0) -> None:
        env_path = os.environ.get(CONFIG_PATH_ENV_VAR)
        self._config_path = Path(config_path if config_path is not None else (env_path or DEFAULT_CONFIG_PATH))
        self._check_interval = check_interval_seconds
        self._lock = Lock()
        self._file_signature: tuple[int, int] | None = None
        self._next_check_monotonic = 0.0
        self._snapshot = self._load()

    @property
    def config_path(self) -> Path:
        return self._config_path

    def snapshot(self) -> RuleSnapshot:
        """返回当前规则快照；按 mtime/size 轻量检测配置变化并自动重载。"""
        if time.monotonic() >= self._next_check_monotonic:
            with self._lock:
                if time.monotonic() >= self._next_check_monotonic:
                    self._next_check_monotonic = time.monotonic() + self._check_interval
                    if self._current_signature() != self._file_signature:
                        self._snapshot = self._load()
        return self._snapshot

    def reload(self) -> RuleSnapshot:
        """强制立即重载配置（供管理端点/运维调用）。"""
        with self._lock:
            self._snapshot = self._load()
            self._next_check_monotonic = time.monotonic() + self._check_interval
            return self._snapshot

    def _current_signature(self) -> tuple[int, int] | None:
        try:
            stat = self._config_path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _load(self) -> RuleSnapshot:
        self._file_signature = self._current_signature()
        path_text = str(self._config_path)
        if self._file_signature is None:
            logger.warning(
                "filter rules config missing at %s; falling back to builtin defaults (fail-closed)",
                path_text,
            )
            return _builtin_snapshot(path_text, "config_file_missing")
        try:
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
            snapshot = compile_config(raw, source=SOURCE_CONFIG_FILE, config_path=path_text)
        except (OSError, ValueError) as exc:
            # json.JSONDecodeError 是 ValueError 的子类
            logger.error(
                "filter rules config invalid at %s (%s); falling back to builtin defaults (fail-closed)",
                path_text,
                exc,
            )
            return _builtin_snapshot(path_text, f"config_invalid: {exc}")
        logger.info(
            "filter rules loaded from %s: %d enabled rules, %d disabled",
            path_text,
            snapshot.enabled_rule_count,
            len(snapshot.disabled_rule_ids),
        )
        return snapshot


default_engine = FilterRuleEngine()
