"""话题决策模型训练/评估/服务共用的纯函数（不依赖 backend，可在独立 venv 中运行）。

输入格式一旦用于训练就固定下来：训练、评估、serve.py 必须用同一个 format_input()，
否则线上分数与离线评估不可比。改格式等于换模型，需要重新训练与校准阈值。
"""

from __future__ import annotations

import hashlib
import re

LABELS = ("off_topic", "academic")
LAYA_TOPIC_QUESTIONS = {
    "topic": {
        "type": "choice",
        "instructions": (
            "判断[内容]中的实际请求是否属于高中学科学习或学习管理。"
            "[学科]只代表所选频道。简短追问结合[上一问]理解，上一问是学科问题也不能让新的无关请求通过。"
        ),
        "criteria": {
            "academic": "高中学科知识、解题、实验、作文和阅读辅导、复习与学习管理",
            "off_topic": "娱乐、闲聊、生活咨询、无教学目标的小说续写或角色接龙、索取系统信息",
        },
    },
}
INPUT_TEXT_MAX_CHARS = 1200
INPUT_CONTEXT_MAX_CHARS = 300

_PII_PATTERNS = (
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "<身份证号>"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "<手机号>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<邮箱>"),
    (re.compile(r"(?i)(qq|微信|wx|vx)[:：\s号]*[\w-]{5,}"), r"\1<账号>"),
    # 学号/考号等长数字串；题目里的数值一般不超过 7 位，保留
    (re.compile(r"(?<![\d.])\d{8,16}(?![\d.])"), "<长数字>"),
)


def normalize(text: str) -> str:
    """与 IntentClassifyService.classify_topic 的归一化一致。"""
    return "".join((text or "").split()).lower()


def text_hash(subject: str, text: str) -> str:
    """与 topic_model_shadow 日志里的 text_hash 一致，用于离线关联影子判定。"""
    return hashlib.sha256(f"{subject}|{normalize(text)}".encode()).hexdigest()[:16]


def sample_key(subject: str, text: str, context: str = "") -> str:
    """标注样本的稳定主键（学科 + 正文 + 上一问），用于人工复核表回填。"""
    raw = "|".join((subject or "", normalize(text), normalize(context)))
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def mask_pii(text: str) -> str:
    masked = text or ""
    for pattern, replacement in _PII_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def format_input(subject: str, text: str, context: str | None = None, kind: str = "input") -> str:
    parts = [f"[学科]{subject or '未知'}"]
    if kind == "output":
        parts.append("[类型]回复抽检")
    if context:
        parts.append(f"[上一问]{context[-INPUT_CONTEXT_MAX_CHARS:]}")
    parts.append(f"[内容]{(text or '')[:INPUT_TEXT_MAX_CHARS]}")
    return "\n".join(parts)


def binary_metrics(labels: list[int], probs: list[float], accept: float, reject: float) -> dict[str, float]:
    """按三档策略统计。1=academic。

    false_refusal：真实学科问题被模型直接拒（p<=reject）——对学生伤害最大；
    false_pass：真实越界被模型直接放（p>=accept）；
    uncertain_rate：落入中间档、需回退 LLM 的比例（决定省下多少 LLM 调用）。
    """
    academic = [p for y, p in zip(labels, probs) if y == 1]
    off_topic = [p for y, p in zip(labels, probs) if y == 0]
    total = len(probs) or 1
    return {
        "n": float(len(probs)),
        "academic_n": len(academic),
        "off_topic_n": len(off_topic),
        "false_refusals": sum(p <= reject for p in academic),
        "false_passes": sum(p >= accept for p in off_topic),
        "false_refusal_rate": sum(p <= reject for p in academic) / (len(academic) or 1),
        "false_pass_rate": sum(p >= accept for p in off_topic) / (len(off_topic) or 1),
        "uncertain_rate": sum(reject < p < accept for p in probs) / total,
        "accuracy_at_0.5": sum((p >= 0.5) == (y == 1) for y, p in zip(labels, probs)) / total,
    }


def sweep_thresholds(
    labels: list[int],
    probs: list[float],
    *,
    max_false_refusal: float = 0.005,
    max_false_pass: float = 0.02,
) -> dict[str, float] | None:
    """在误拒/漏放上限内找中间档最窄的一对阈值；找不到返回 None（说明模型还不能上线）。"""
    grid = [i / 100 for i in range(1, 100)]
    best: dict[str, float] | None = None
    for reject in grid:
        for accept in grid:
            if accept <= reject:
                continue
            metrics = binary_metrics(labels, probs, accept, reject)
            if metrics["false_refusal_rate"] > max_false_refusal or metrics["false_pass_rate"] > max_false_pass:
                continue
            if best is None or metrics["uncertain_rate"] < best["uncertain_rate"]:
                best = {"accept": accept, "reject": reject, **metrics}
    return best
