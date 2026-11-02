"""资产字段边界守卫（G7/G8）：人设只写人设、外观只写外观。

背景（20 号 §1 根因 R6）：实测资产抽取把两类信息写串了——

- `persona` 里塞进**外观**：「…身高：约175cm；体型：匀称偏壮；体重体感：健壮…；
  记忆点：面无特别标志，神情因紧张而微红」；
- `appearance` 里塞进**性格/神态**：「…圆脸，络腮胡茬，**常咧嘴笑**，黑发短碎」。

后果是**生图质量的上游污染**：定妆图提示词需要完整外观（却被拆散在人设里、还夹着
性格），而项目红线「persona 禁进生图 prompt」在字段定义本身混杂时无法可靠执行。

本模块只做**确定性词表检测**（不调模型、不做语义判断），命中即**告警**：
- G7：`persona` 命中外观词表
- G8：`appearance` 命中性格词表或神态词表

**保守性**：词表是启发式，只用于提示与审计，**不拦截落库**（沿用「门禁只拦 critical」
的项目口径）；漏检不报错，误报只多一条告警。
"""

from __future__ import annotations

# —— 词表（禁魔术字符串：集中定义，便于按实测增补）——

# G7：只应出现在 appearance 里的外观词（含身高/体型/五官/毛发/肤质/识别特征）
APPEARANCE_WORDS: tuple[str, ...] = (
    "身高", "身量", "体型", "身材", "体重", "体态",
    "厘米", "cm", "CM",
    "脸型", "五官", "眉形", "眉毛", "鼻梁", "嘴唇", "下颌",
    "发型", "发色", "长发", "短发", "卷发", "马尾", "刘海", "鬓角",
    "肤色", "皮肤", "肤质", "瞳孔", "瞳色", "眼型",
    "疤痕", "刀疤", "胎记", "痣", "络腮", "胡茬", "胡须",
    "相貌", "长相", "容貌", "面相",
)

# G8-a：只应出现在 persona 里的性格词
PERSONALITY_WORDS: tuple[str, ...] = (
    "性格", "爽朗", "忠诚", "温柔", "清冷", "乐观", "急躁", "冷酷", "善良",
    "阴险", "豪爽", "内敛", "活泼", "沉稳", "隐忍", "泼辣", "仗义", "圆滑",
    "固执", "多疑", "刚正", "憨厚",
)

# G8-b：神态/表情类词——两个字段都不该写（persona 越界写神态、appearance 写了性格外化）
DEMEANOR_WORDS: tuple[str, ...] = (
    "神情", "神态", "眼神", "表情", "眉头", "面色", "目光", "嘴角",
    "咧嘴", "常笑", "爱笑", "习惯性", "说话时", "语气",
)


def check_character_fields(persona: str, appearance: str) -> list[str]:
    """检查一个角色的字段边界，返回告警列表（空列表 = 通过）。

    返回的每条告警形如 `persona 含外观词「身高」`，供日志与轨迹透出。
    """
    warnings: list[str] = []
    p = persona or ""
    a = appearance or ""

    for word in APPEARANCE_WORDS:
        if word in p:
            warnings.append(f"persona 含外观词「{word}」——外观应归 appearance（G7）")
            break  # 同一字段只报一次，避免告警刷屏
    for word in DEMEANOR_WORDS:
        if word in p:
            warnings.append(f"persona 含神态词「{word}」——神情神态属外观或应删（G7）")
            break
    for word in PERSONALITY_WORDS:
        if word in a:
            warnings.append(f"appearance 含性格词「{word}」——性格应归 persona（G8）")
            break
    for word in DEMEANOR_WORDS:
        if word in a:
            warnings.append(f"appearance 含神态词「{word}」——只写可拍的外观（G8）")
            break
    return warnings
