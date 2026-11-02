"""技能包库单测：Claude Code 式渐进式加载（目录 → LLM 自选 → 按名加载正文）。

覆盖：目录展示全部技能包（题材+模型）、按名加载不过滤、正文渲染截断、
注入路由分组与加载审计摘要。运行：python server/test_model_skills.py
"""

from app.agent.skills import (
    get_skill_packs, load_skill_packs, render_skill_directory, render_skill_text,
)


def test_directory_contains_all_packs() -> None:
    """渐进式第一级：目录展示全部技能包，模型包行含适用模型声明。"""
    packs = load_skill_packs()
    assert len(packs) >= 12  # 5 题材包 + 7 模型包
    directory = render_skill_directory()
    # 题材包与模型包都进目录（模型包不再被代码过滤，由 LLM 对照预设自选）
    for name in ("悬疑复仇分镜", "Agnes 生图规范", "Seedance 生视频规范"):
        assert name in directory, name
    # 模型包目录行展示适用模型声明（LLM 据此与项目预设对应，系列通配同样可读）
    assert "agnes-image*" in directory
    assert "doubao-seedance*" in directory
    print("技能目录展示验证通过")


def test_get_packs_by_name() -> None:
    """渐进式第二级：按名精确加载，题材/模型包统一入口，幻觉名称跳过。"""
    loaded = get_skill_packs(["悬疑复仇分镜", "Agnes 生图规范",
                              "不存在的技能包", ""])
    assert [p.name for p in loaded] == ["悬疑复仇分镜", "Agnes 生图规范"]
    # 元数据完整：题材包带 genres 无 task_type，模型包带 models + task_type
    assert loaded[0].genres and not loaded[0].task_type
    assert loaded[1].models and loaded[1].task_type == "t2i"
    assert get_skill_packs([]) == []
    print("按名加载验证通过")


def test_render_and_route() -> None:
    """正文渲染 + 按包元数据路由注入位置（题材→全阶段 / t2i→生图 / t2v→画面描述）。"""
    loaded = get_skill_packs(["悬疑复仇分镜", "Agnes 生图规范", "Agnes 生视频规范"])
    skill_text = render_skill_text([p for p in loaded if not p.task_type])
    t2i_skill = render_skill_text([p for p in loaded if p.task_type == "t2i"])
    t2v_skill = render_skill_text([p for p in loaded if p.task_type == "t2v"])
    # 渲染段落格式：【技能包·名】+ 简介 + 正文
    assert skill_text.startswith("【技能包·悬疑复仇分镜】")
    assert t2i_skill.startswith("【技能包·Agnes 生图规范】")
    assert t2v_skill.startswith("【技能包·Agnes 生视频规范】")
    # 路由互斥：生图正文不含视频包内容，反之亦然
    assert "生视频" not in t2i_skill
    assert "生图" not in t2v_skill
    # 超长正文截断保护（阈值 900 字符）
    long_pack = load_skill_packs()[0]
    truncated = render_skill_text([long_pack], max_chars_per_skill=10)
    assert truncated.endswith("……")
    print("渲染与注入路由验证通过")


def test_skill_audit() -> None:
    """加载审计摘要：已加载/自选未命中/注入路由三条信息齐备。"""
    from app.script.llm_parser import _render_skill_audit

    summary = _render_skill_audit({
        "selected": ["悬疑复仇分镜", "Agnes 生图规范", "幻觉包"],
        "loaded": ["悬疑复仇分镜", "Agnes 生图规范"],
        "routes": {"all": ["悬疑复仇分镜"], "t2i": ["Agnes 生图规范"], "t2v": []},
    })
    assert summary == ("已加载：悬疑复仇分镜、Agnes 生图规范；自选未命中：幻觉包；"
                       "注入全阶段：悬疑复仇分镜；注入生图提示词：Agnes 生图规范"), summary
    empty = _render_skill_audit({"selected": [], "loaded": [],
                                 "routes": {"all": [], "t2i": [], "t2v": []}})
    assert "无（LLM 未自选或名称未命中）" in empty
    print("技能加载审计验证通过")


if __name__ == "__main__":
    test_directory_contains_all_packs()
    test_get_packs_by_name()
    test_render_and_route()
    test_skill_audit()
    print("全部通过")
