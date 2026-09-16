"""Direction 模型与 switch_direction 工具的契约测试。"""

import pytest
from pydantic import ValidationError

from agent.prompt import CHEAT_SHEET, DIRECTION_SECTION, SWITCH_DIRECTION_TOOL
from agent.schema import Direction


def make_direction(**overrides):
    base = {"id": "dir-001", "name": "sqli-id-param", "hypothesis": "id 参数存在盲注"}
    base.update(overrides)
    return Direction(**base)


def test_direction_defaults():
    d = make_direction()
    assert d.status == "exploring"
    assert d.steps_used == 0
    assert d.outcome is None


def test_direction_full_fields():
    d = make_direction(
        status="concluded", steps_used=12, outcome="确认 id 参数存在时间盲注"
    )
    assert d.status == "concluded"
    assert d.steps_used == 12
    assert d.outcome == "确认 id 参数存在时间盲注"


def test_direction_rejects_invalid_status():
    with pytest.raises(ValidationError):
        make_direction(status="bogus")


def test_direction_rejects_missing_required():
    with pytest.raises(ValidationError):
        Direction(id="dir-002")


def test_switch_tool_shape():
    func = SWITCH_DIRECTION_TOOL["function"]
    assert SWITCH_DIRECTION_TOOL["type"] == "function"
    assert func["name"] == "switch_direction"
    params = func["parameters"]
    assert set(params["properties"]) == {
        "current_outcome",
        "next_direction",
        "hypothesis",
    }
    assert params["required"] == ["next_direction", "hypothesis"]


def test_prompt_sections_content():
    assert DIRECTION_SECTION.strip()
    for kw in ["登录", "技术栈", "切换", "15 步", "预算"]:
        assert kw in DIRECTION_SECTION
    assert CHEAT_SHEET.strip()
    for kw in ["现象", "PoC", "Scope", "切换", "发现多少报多少"]:
        assert kw in CHEAT_SHEET
    assert len([ln for ln in CHEAT_SHEET.splitlines() if ln.startswith("- ")]) <= 10
