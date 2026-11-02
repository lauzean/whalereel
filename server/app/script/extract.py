"""剧本文档文本提取：txt/md/docx/pdf 等文本格式文档 → 纯文本。

- txt/md：编码探测解码（utf-8 → gb18030 → 宽松兜底）
- docx：OOXML 即 zip 包，stdlib zipfile + ElementTree 解析 word/document.xml
  （无需 python-docx 依赖；w:t 文本 + w:tab/w:br 制表换行还原段落）
- pdf：pypdf 提取（文字版 PDF；扫描版/图片型提取不到文本时明确报错）

提取结果仅供确认：不直接落库解析，前端填回编辑器由用户确认后走解析流程。
"""

import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree

# 支持的剧本文档扩展名（小写）
SCRIPT_DOC_EXTENSIONS = (".txt", ".md", ".markdown", ".docx", ".pdf")

# 上传文件大小上限（20 MB，剧本文本远小于此，主要防误传超大文件）
SCRIPT_DOC_MAX_BYTES = 20 * 1024 * 1024

# 展示用 MB（错误提示换算）
_BYTES_PER_MB = 1024 * 1024

# docx OOXML 命名空间（wordprocessingml）
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
# 段落内关心的节点：文本 run / 制表符 / 换行符
_TAG_TEXT = f"{{{_W_NS}}}t"
_TAG_TAB = f"{{{_W_NS}}}tab"
_TAG_BR = f"{{{_W_NS}}}br"


class DocExtractError(RuntimeError):
    """文档提取失败（格式不支持/超限/损坏/无文本），信息直接面向用户展示。"""


def _decode_text(data: bytes) -> str:
    """按编码优先级解码纯文本文档：BOM utf-8 → gb18030（中文 txt 常见）→ 宽松 utf-8。"""
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_docx_text(data: bytes) -> str:
    """解压 docx（zip 容器）并解析 word/document.xml 段落文本。

    逐段落遍历子节点按文档顺序拼接：w:t 文本、w:tab 制表、w:br 换行；
    段落间以换行分隔（剧本按行解析依赖换行结构）。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            xml_bytes = zf.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        raise DocExtractError("docx 文件损坏或格式异常，请在 Word 中重新另存为 .docx") from e
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as e:
        raise DocExtractError("docx 内容解析失败（document.xml 异常）") from e
    paragraphs: list[str] = []
    # iter 深度优先覆盖正文与文本框等全部 w:p 段落
    for p in root.iter(f"{{{_W_NS}}}p"):
        parts: list[str] = []
        for node in p.iter():
            if node.tag == _TAG_TEXT:
                parts.append(node.text or "")
            elif node.tag == _TAG_TAB:
                parts.append("\t")
            elif node.tag == _TAG_BR:
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)


def _extract_pdf_text(data: bytes) -> str:
    """pypdf 提取 PDF 文本（逐页，页间换行）；扫描版无文本时明确报错。"""
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise DocExtractError("PDF 解析依赖未安装：请执行 pip install pypdf 后重启后端") from e
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # 空密码尝试解密（仅限无打开密码、仅权限受限的 PDF）
            try:
                reader.decrypt("")
            except Exception as e:  # noqa: BLE001 各类加密算法异常统一转用户提示
                raise DocExtractError("PDF 已加密，请先解除密码保护后再上传") from e
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except DocExtractError:
        raise
    except Exception as e:  # noqa: BLE001 pypdf 各类解析异常统一转用户提示
        raise DocExtractError(f"PDF 解析失败：{e}") from e
    text = "\n".join(x for x in pages if x)
    if not text.strip():
        raise DocExtractError(
            "PDF 中未提取到文本：可能是扫描版/图片型 PDF，"
            "请改用文字版文档或直接粘贴剧本文本"
        )
    return text


def extract_text(filename: str, data: bytes) -> str:
    """按扩展名分发提取纯文本（校验扩展名白名单与大小上限），空文本由调用方判。

    旧版 .doc 为二进制格式不支持，给出明确转换指引。
    """
    if len(data) > SCRIPT_DOC_MAX_BYTES:
        raise DocExtractError(
            f"文件过大（上限 {SCRIPT_DOC_MAX_BYTES // _BYTES_PER_MB} MB），请拆分后上传")
    ext = Path(filename).suffix.lower()
    if ext == ".doc":
        raise DocExtractError("暂不支持旧版 .doc，请在 Word 中另存为 .docx 后上传")
    if ext not in SCRIPT_DOC_EXTENSIONS:
        supported = " / ".join(SCRIPT_DOC_EXTENSIONS)
        raise DocExtractError(f"不支持的文档格式：{ext or '未知'}（支持：{supported}）")
    if ext in (".txt", ".md", ".markdown"):
        text = _decode_text(data)
    elif ext == ".docx":
        text = _extract_docx_text(data)
    else:  # .pdf
        text = _extract_pdf_text(data)
    # 统一换行符（Windows CRLF → LF），剧本按行解析依赖一致换行
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()
