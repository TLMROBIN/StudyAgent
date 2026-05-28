from backend.models.user import UserRole
from backend.services.account_service import (
    _transliterate_chinese_run,
    build_default_password,
    build_generated_username,
    extract_classroom_number,
    transliterate_name_to_pinyin,
)


def test_transliterate_name_to_pinyin_handles_common_names():
    assert transliterate_name_to_pinyin("张三") == "zhangsan"
    assert transliterate_name_to_pinyin("欧阳娜娜") == "ouyangnana"
    assert transliterate_name_to_pinyin("周俊栋") == "zhoujundong"


def test_transliterate_chinese_run_falls_back_to_bundled_map(monkeypatch):
    monkeypatch.setattr("backend.services.account_service.lazy_pinyin", None)
    monkeypatch.setattr("backend.services.account_service.Style", None)

    assert _transliterate_chinese_run("周俊栋") == ["zhou", "jun", "dong"]


def test_extract_classroom_number_supports_digits_and_chinese_numbers():
    assert extract_classroom_number("12班") == "12"
    assert extract_classroom_number("三班") == "3"
    assert extract_classroom_number("十二班") == "12"


def test_build_generated_username_and_default_password_follow_rules():
    assert build_generated_username("张三", UserRole.STUDENT, "8班") == "zhangsan8"
    assert build_generated_username("李老师", UserRole.TEACHER) == "lilaoshi"
    assert build_default_password("张三") == "zhangsan123456"
