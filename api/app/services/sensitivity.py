"""数据敏感分级与脱敏（FR-05 / FR-06，规则依据 PRD 附录 C）。

四级划分（平台自定义，不采用国家三级数据框架）：

| 级别 | 名称 | 判定 | 默认处置 |
|------|------|------|---------|
| L1 | 公开 | 无敏感标识 | 可直接使用 |
| L2 | 内部 | 未命中 L3/L4 规则的业务数据 | 记录即可 |
| L3 | 敏感 | 姓名、手机号、邮箱、地址等 | 输出前脱敏 |
| L4 | 机密 | 身份证、银行卡、护照、生物特征、健康、行踪 | 审批加严 + 强制脱敏 |

**匹配方式**：字段名关键词 + 样本值正则，双路匹配。
**越级规则**：任一字段命中 L4，则整个数据源按 L4 对待（见 ``aggregate_level``）。

> 关于 L1：自动打标能可靠判定的最低级别是 L2（「不是敏感字段」不等于「可以公开」），
> L1 需由数据工程师在探查结果上显式标记，不由本模块猜测。
"""

import enum
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


class SensitivityLevel(enum.StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"

    @property
    def order(self) -> int:
        """用于比较与取最大值：L1=1 … L4=4。"""
        return int(self.value[1:])

    @property
    def label(self) -> str:
        return {"L1": "公开", "L2": "内部", "L3": "敏感", "L4": "机密"}[self.value]


# --------------------------------------------------------------------------
# 规则表（字段名）—— 「规则表可维护」是附录 C 的明确要求
# --------------------------------------------------------------------------

# 字段名先归一化为 snake_case 再匹配，因此键统一用下划线形式
_L4_KEYWORDS: dict[str, str] = {
    "身份证": "身份证号",
    "id_card": "身份证号",
    "identity_card": "身份证号",
    "identity_no": "身份证号",
    "cert_no": "证件号",
    "银行卡": "银行卡号",
    "bank_card": "银行卡号",
    "card_no": "卡号",
    "护照": "护照号",
    "passport": "护照号",
    "生物特征": "生物特征",
    "biometric": "生物特征",
    "fingerprint": "指纹",
    "face_id": "人脸特征",
    "健康": "健康信息",
    "health": "健康信息",
    "medical": "医疗信息",
    "病历": "医疗信息",
    "行踪": "行踪轨迹",
    "轨迹": "行踪轨迹",
    "location": "位置信息",
    "gps": "位置信息",
    "latitude": "位置信息",
    "longitude": "位置信息",
}

_L3_KEYWORDS: dict[str, str] = {
    "姓名": "姓名",
    "name": "姓名",
    "real_name": "姓名",
    "user_name": "姓名",
    "手机": "手机号",
    "phone": "手机号",
    "mobile": "手机号",
    "tel": "联系电话",
    "邮箱": "邮箱",
    "email": "邮箱",
    "mail": "邮箱",
    "地址": "地址",
    "address": "地址",
    "addr": "地址",
}

# 过于宽泛的关键词：命中这些词时需先排除技术性字段名（table_name 不是「姓名」）
_GENERIC_KEYWORDS = frozenset({"name", "tel", "mail", "addr"})

# 出现即说明该字段是技术元数据而非个人数据
_STOPWORDS = frozenset(
    {
        "table",
        "file",
        "host",
        "db",
        "database",
        "schema",
        "type",
        "code",
        "status",
        "field",
        "column",
        "index",
        "key",
        "path",
        "source",
        "target",
        "job",
        "task",
        "pipeline",
        "operator",
        "connector",
        "topic",
        "bucket",
        "目录",
        "表名",
        "字段",
    }
)


def _normalize(column_name: str) -> str:
    """字段名归一化：拆驼峰 → 小写 → 连字符/空格转下划线。"""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", column_name.strip())
    return re.sub(r"[\s-]+", "_", spaced.lower())


def _keyword_hit(normalized: str, keywords: dict[str, str]) -> tuple[str, str] | None:
    """返回 ``(命中的关键词, 敏感类别)``；精确匹配优先于包含匹配。"""
    if normalized in keywords:
        return normalized, keywords[normalized]

    for keyword, category in keywords.items():
        if keyword not in normalized:
            continue
        # 「name」这类泛化词要避开 table_name / file_name 等技术字段
        if keyword in _GENERIC_KEYWORDS and any(word in normalized for word in _STOPWORDS):
            continue
        return keyword, category
    return None


# --------------------------------------------------------------------------
# 规则表（样本值）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ValueRule:
    category: str
    level: SensitivityLevel
    pattern: re.Pattern[str]


# 顺序无关（命中取最高级）；注意 18 位纯数字同时匹配身份证与银行卡，二者同为 L4
_VALUE_RULES: tuple[ValueRule, ...] = (
    ValueRule("身份证号", SensitivityLevel.L4, re.compile(r"^\d{17}[\dXx]$")),
    ValueRule("银行卡号", SensitivityLevel.L4, re.compile(r"^\d{16,19}$")),
    ValueRule("手机号", SensitivityLevel.L3, re.compile(r"^1[3-9]\d{9}$")),
    ValueRule("邮箱", SensitivityLevel.L3, re.compile(r"^[\w.+-]+@[\w-]+\.[\w.-]+$")),
)


def _match_samples(samples: Sequence[str]) -> tuple[SensitivityLevel, str] | None:
    hits: dict[SensitivityLevel, str] = {}
    for raw in samples:
        text = ("" if raw is None else str(raw)).strip()
        if not text:
            continue
        for rule in _VALUE_RULES:
            if rule.pattern.match(text):
                # 同级别保留先命中的规则：规则表按「越具体越靠前」排列，
                # 避免 18 位数字把「身份证号」覆盖成更宽泛的「银行卡号」
                hits.setdefault(rule.level, rule.category)
    if not hits:
        return None
    level = max(hits, key=lambda item: item.order)
    return level, hits[level]


# --------------------------------------------------------------------------
# 定级
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnAssessment:
    """单字段的分级结论，``matched_by`` 用于在探查结果页解释「为什么是这一级」。"""

    level: SensitivityLevel
    category: str
    matched_by: str  # column_name | sample_value | default


def assess_column(column_name: str, samples: Sequence[str] = ()) -> ColumnAssessment:
    """对单个字段定级：字段名规则优先，其次样本值规则，都不命中则为 L2。"""
    normalized = _normalize(column_name)

    hit = _keyword_hit(normalized, _L4_KEYWORDS)
    if hit:
        return ColumnAssessment(SensitivityLevel.L4, hit[1], "column_name")

    hit = _keyword_hit(normalized, _L3_KEYWORDS)
    if hit:
        return ColumnAssessment(SensitivityLevel.L3, hit[1], "column_name")

    sample_hit = _match_samples(samples)
    if sample_hit:
        return ColumnAssessment(sample_hit[0], sample_hit[1], "sample_value")

    return ColumnAssessment(SensitivityLevel.L2, "业务数据", "default")


def aggregate_level(levels: Iterable[SensitivityLevel]) -> SensitivityLevel:
    """越级规则：取最高级别；空集合按 L2 处理（不臆测为公开）。"""
    return max(levels, key=lambda level: level.order, default=SensitivityLevel.L2)


# --------------------------------------------------------------------------
# 脱敏（FR-06：不返回明文原始值）
# --------------------------------------------------------------------------


class MaskingStrategy(enum.StrEnum):
    FULL = "full"  # 完全遮蔽
    PARTIAL = "partial"  # 保留首尾，中间遮蔽
    TRUNCATE = "truncate"  # 仅暴露极少前缀


_STRATEGY_BY_LEVEL: dict[SensitivityLevel, MaskingStrategy] = {
    SensitivityLevel.L1: MaskingStrategy.TRUNCATE,
    SensitivityLevel.L2: MaskingStrategy.TRUNCATE,
    SensitivityLevel.L3: MaskingStrategy.PARTIAL,
    SensitivityLevel.L4: MaskingStrategy.FULL,
}


def _truncate_mask(text: str) -> str:
    visible = min(4, len(text) - 1)
    return f"{text[:visible]}…" if visible > 0 else "*" * len(text)


def _partial_mask(text: str) -> str:
    """保留首尾、遮蔽中段。

    首尾可见字符数之和**必须小于字符串长度**，否则短值会被原样返回——
    这是 FR-06 不允许的。
    """
    length = len(text)
    if length <= 2:
        return "*" * length
    head = min(3, (length - 1) // 2)
    tail = min(4, length - 1 - head)
    return f"{text[:head]}{'*' * (length - head - tail)}{text[-tail:]}"


def mask_value(value: object, level: SensitivityLevel) -> str:
    """按级别脱敏单个样本值。

    **所有级别都不返回原始明文**——这是 FR-06 的硬要求，
    因此 L1/L2 也只暴露极短前缀用于人工核对字段内容形态。
    """
    text = "" if value is None else str(value)
    if not text:
        return ""

    strategy = _STRATEGY_BY_LEVEL[level]
    if strategy is MaskingStrategy.FULL:
        return "*" * min(len(text), 8)
    if strategy is MaskingStrategy.PARTIAL:
        return _partial_mask(text)
    return _truncate_mask(text)
