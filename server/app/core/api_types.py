"""前后端契约生成：后端 OpenAPI → 前端 TypeScript 类型（TODO-6 后半）。

**为什么需要这一层**：前端 `apps/web/src/api/client.ts` 的请求/响应类型全靠
手写，与后端 DTO 的同步完全依赖人肉——本轮审计已抓到过两处不一致。契约靠
约定维持，就一定会漂移。本模块把**机器可读的那一半**（请求体 Pydantic 模型）
从 OpenAPI 直接生成成 TS 类型，并让验证脚本对生成结果做「是否过期」的对账。

**覆盖边界（必须说清楚，避免误以为全链路已受控）**：实测后端 136 个操作里
- 56 个有请求体 `$ref`（Pydantic 模型）→ **可生成**，本模块负责；
- 0 个有响应 `$ref`（各 router 直接返回裸 dict）→ **不可生成**，
  响应类型仍是手写，漂移风险未消除（要根治需给 router 补 `response_model`，
  属另一批工作）。

**渲染是确定性的**：同一份 OpenAPI 永远产出同一份文本（无时间戳、无随机序），
因此「生成文件与当前 schema 不一致」= 有人在改了后端契约后忘了重跑生成，
验证脚本据此报警（见 `_verify_pipeline.py` 断言 78）。

本模块不做任何语义判断：只按 OpenAPI 的结构做机械映射。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# 生成物路径（相对仓库根）
OUTPUT_REL = "apps/web/src/api/generated.ts"

# 生成文件的落款注释（勿手改 + 重跑命令）
HEADER = """/**
 * 后端契约生成物 —— 请勿手改。
 *
 * 由 `python scripts/gen_api_types.py` 从后端 OpenAPI 生成；
 * 改了后端请求 DTO 后必须重跑（验证脚本会对账，过期会失败）。
 *
 * 覆盖范围：请求体 Pydantic 模型 + 操作清单（METHOD /path → 请求类型）。
 * 响应体未标注 `response_model`，OpenAPI 里没有结构信息，故未生成。
 *
 * schema 指纹：{fingerprint}
 */
"""

# OpenAPI 内置的错误结构（FastAPI 自带），前端不消费，不生成
_SKIP_SCHEMAS = {"HTTPValidationError", "ValidationError"}

_JSON = "application/json"


def _ref_name(ref: str) -> str:
    """`#/components/schemas/X` → `X`（非本地引用原样返回）。"""
    prefix = "#/components/schemas/"
    return ref[len(prefix):] if ref.startswith(prefix) else ref


def _quote(value: str) -> str:
    """TS 字符串字面量（用 json 转义，天然处理引号与反斜杠）。"""
    return json.dumps(value, ensure_ascii=False)


def ts_type(schema: Any) -> str:
    """OpenAPI schema → TS 类型表达式（结构映射，无语义推断）。"""
    if not isinstance(schema, dict) or not schema:
        return "unknown"
    if "$ref" in schema:
        return _ref_name(str(schema["$ref"]))

    # 枚举：字符串字面量联合
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return " | ".join(_quote(str(v)) if isinstance(v, str) else json.dumps(v)
                          for v in enum)

    # 联合（anyOf/oneOf）：nullable 展开为 `T | null`
    for key in ("anyOf", "oneOf"):
        variants = schema.get(key)
        if isinstance(variants, list) and variants:
            parts: list[str] = []
            for v in variants:
                t = ts_type(v)
                if t not in parts:
                    parts.append(t)
            return " | ".join(parts)

    t = schema.get("type")
    # type 可能给成列表（如 ["string","null"]）
    if isinstance(t, list):
        return " | ".join("null" if x == "null" else ts_type({**schema, "type": x})
                          for x in t)
    if t == "null":
        return "null"
    if t == "string":
        return "string"
    if t in ("integer", "number"):
        return "number"
    if t == "boolean":
        return "boolean"
    if t == "array":
        item = ts_type(schema.get("items"))
        # 数组元素含联合类型时需加括号：`(A | B)[]` 而非 `A | B[]`
        return f"({item})[]" if "|" in item else f"{item}[]"
    if t == "object":
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            required = set(schema.get("required") or [])
            fields = "".join(
                f"{_ident(k)}{'' if k in required else '?'}: {ts_type(v)}; "
                for k, v in props.items()
            )
            return "{ " + fields + "}"
        ap = schema.get("additionalProperties")
        if isinstance(ap, dict):
            return f"Record<string, {ts_type(ap)}>"
        if ap is True:
            return "Record<string, unknown>"
        return "Record<string, unknown>"
    # 无 type 但有 properties（Pydantic 有时省略 type）
    if isinstance(schema.get("properties"), dict):
        return ts_type({**schema, "type": "object"})
    return "unknown"


def _ident(name: str) -> str:
    """TS 属性名：合法标识符直接使用，否则加引号。"""
    if name.isidentifier():
        return name
    return _quote(name)


def _interface(name: str, schema: dict) -> str:
    """组件 schema → TS interface 文本（字段顺序沿用声明序，保持可读）。"""
    props = schema.get("properties")
    required = set(schema.get("required") or [])
    lines = [f"export interface {name} {{"]
    title = schema.get("title")
    if isinstance(title, str) and title and title != name:
        lines.insert(0, f"/** {title} */")
    if not isinstance(props, dict) or not props:
        # 无字段对象（如空 Body）：给可索引兜底，避免产出空 interface 造成误导
        lines.append("  [key: string]: unknown;")
    else:
        for k, v in props.items():
            opt = "" if k in required else "?"
            lines.append(f"  {_ident(k)}{opt}: {ts_type(v)};")
    lines.append("}")
    return "\n".join(lines)


def _op_body_schema(op: dict) -> tuple[Any, str]:
    """取操作的请求体 schema：优先 JSON，其次 multipart（返回 FormData 标记）。"""
    content = (op.get("requestBody") or {}).get("content") or {}
    if _JSON in content:
        return (content[_JSON] or {}).get("schema"), "json"
    for media in content:
        if media.startswith("multipart/") or media.startswith(
                "application/x-www-form-urlencoded"):
            return None, "form"
    return None, "none"


def render_typescript(spec: dict) -> str:
    """OpenAPI 文档 → 前端 TS 类型文件全文（确定性输出）。"""
    schemas = ((spec.get("components") or {}).get("schemas") or {})
    names = sorted(n for n in schemas if n not in _SKIP_SCHEMAS)

    blocks = [_interface(n, schemas[n]) for n in names]

    # 操作清单：METHOD /path → 请求体类型（无请求体为 null，表单为 FormData）
    ops: list[str] = []
    for path in sorted(spec.get("paths") or {}):
        methods = spec["paths"][path] or {}
        for method in sorted(methods):
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            schema, kind = _op_body_schema(methods[method] or {})
            if kind == "form":
                body = "FormData"
            elif schema:
                body = ts_type(schema)
            else:
                body = "null"
            ops.append(f"  {_quote(f'{method.upper()} {path}')}: {body};")

    parts = [
        HEADER.format(fingerprint=fingerprint(spec)),
        "\n\n".join(blocks),
        "// 操作清单：冒号右侧为该操作的请求体类型（null = 无请求体）\n"
        "export interface ApiRequestMap {\n" + "\n".join(ops) + "\n}",
        "",
    ]
    return "\n\n".join(parts)


def fingerprint(spec: dict) -> str:
    """契约指纹：只看类型相关的部分（路径 + 组件 schema），与描述文案无关。

    不参与指纹的内容：`info`、`servers`、summary/description（改了不该让
    生成物"过期"）。这样指纹变化 = 真正影响前端类型的契约变化。
    """
    subset = {
        "schemas": (spec.get("components") or {}).get("schemas") or {},
        "paths": {
            p: {m: {"requestBody": (op or {}).get("requestBody"),
                    "parameters": [
                        {k: v for k, v in prm.items() if k in ("name", "in", "required", "schema")}
                        for prm in ((op or {}).get("parameters") or [])
                    ]}
                for m, op in (methods or {}).items()
                if m.lower() in ("get", "post", "put", "patch", "delete")}
            for p, methods in (spec.get("paths") or {}).items()
        },
    }
    raw = json.dumps(subset, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
