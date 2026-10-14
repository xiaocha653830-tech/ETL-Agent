"""敏感分级与脱敏测试（FR-05 / FR-06，规则见 PRD 附录 C）。"""

import pytest

from app.services.sensitivity import (
    SensitivityLevel,
    aggregate_level,
    assess_column,
    mask_value,
)

# --------------------------------------------------------------------------
# 字段名规则
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "expected_category"),
    [
        ("id_card", "身份证号"),
        ("idCard", "身份证号"),  # 驼峰需先归一化
        ("bank_card_no", "银行卡号"),
        ("passport_no", "护照号"),
        ("health_status", "健康信息"),
        ("gps_latitude", "位置信息"),
        ("latitude", "位置信息"),
    ],
)
def test_l4_by_column_name(column: str, expected_category: str):
    result = assess_column(column)
    assert result.level is SensitivityLevel.L4
    assert result.category == expected_category
    assert result.matched_by == "column_name"


@pytest.mark.parametrize(
    ("column", "expected_category"),
    [
        ("user_name", "姓名"),
        ("real_name", "姓名"),
        ("userName", "姓名"),
        ("mobile_phone", "手机号"),
        ("contact_email", "邮箱"),
        ("shipping_address", "地址"),
    ],
)
def test_l3_by_column_name(column: str, expected_category: str):
    result = assess_column(column)
    assert result.level is SensitivityLevel.L3
    assert result.category == expected_category


@pytest.mark.parametrize("column", ["table_name", "file_name", "host_name", "db_name"])
def test_technical_name_columns_are_not_personal_data(column: str):
    """「name」是泛化关键词，不能把 table_name 误判成姓名。"""
    assert assess_column(column).level is SensitivityLevel.L2


def test_business_column_defaults_to_l2():
    result = assess_column("order_amount")
    assert result.level is SensitivityLevel.L2
    assert result.matched_by == "default"


# --------------------------------------------------------------------------
# 样本值规则
# --------------------------------------------------------------------------


def test_l3_by_sample_value():
    """列名无法判断时，靠样本值兜底。"""
    result = assess_column("col_1", ["13812345678"])
    assert result.level is SensitivityLevel.L3
    assert result.category == "手机号"
    assert result.matched_by == "sample_value"


def test_l4_by_sample_value():
    result = assess_column("col_2", ["110101199003078888"])
    assert result.level is SensitivityLevel.L4
    assert result.category == "身份证号"


def test_column_name_takes_precedence_over_samples():
    """字段名规则优先于样本值规则（避免样本恰好不是敏感值时被降级）。"""
    result = assess_column("id_card", ["none"])
    assert result.level is SensitivityLevel.L4
    assert result.matched_by == "column_name"


# --------------------------------------------------------------------------
# 越级规则
# --------------------------------------------------------------------------


def test_aggregate_takes_highest_level():
    levels = [SensitivityLevel.L2, SensitivityLevel.L4, SensitivityLevel.L3]
    assert aggregate_level(levels) is SensitivityLevel.L4


def test_aggregate_of_empty_is_not_public():
    """空集合按 L2 处理 —— 不能把「没扫到」当成「可以公开」。"""
    assert aggregate_level([]) is SensitivityLevel.L2


# --------------------------------------------------------------------------
# 脱敏
# --------------------------------------------------------------------------


def test_l4_is_fully_masked():
    assert mask_value("110101199003078888", SensitivityLevel.L4) == "********"


def test_l3_keeps_only_head_and_tail():
    assert mask_value("13812345678", SensitivityLevel.L3) == "138****5678"
    # 短值必须保留至少一个星号，不能被原样返回
    assert mask_value("a@b.com", SensitivityLevel.L3) == "a@b*com"
    assert mask_value("abc", SensitivityLevel.L3) == "a*c"


def test_l2_exposes_only_a_short_prefix():
    assert mask_value("ACTIVE", SensitivityLevel.L2) == "ACTI…"
    assert mask_value("ok", SensitivityLevel.L2) == "o…"
    assert mask_value("x", SensitivityLevel.L2) == "*"


@pytest.mark.parametrize("level", list(SensitivityLevel))
def test_no_level_returns_the_full_plaintext(level: SensitivityLevel):
    """FR-06 硬要求：任何级别都不得回传完整明文。"""
    secret = "13812345678"
    masked = mask_value(secret, level)
    assert masked != secret
    assert secret not in masked


def test_empty_value_stays_empty():
    assert mask_value(None, SensitivityLevel.L3) == ""
    assert mask_value("", SensitivityLevel.L4) == ""
