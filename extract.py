#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""附件解析器：把拖进来的文件变成可注入上下文的文本。

用法: python extract.py <path> [--max-chars 20000]
输出: 一行 JSON {"kind": ..., "text": ..., "meta": {...}}

设计要点：不依赖调用方运行时的库——PDF/DOCX 有库就用库，没有就退回到 stdlib 方案，
保证在任何 python 下都能跑出"至少可用的结果"。
"""

import io
import json
import os
import re
import sys
import zipfile
import zlib

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl", ".yaml", ".yml",
    ".csv", ".tsv", ".ini", ".cfg", ".conf", ".toml", ".env", ".sql", ".xml", ".html",
    ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs",
    ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".bat", ".ps1",
    ".tex", ".srt", ".vtt", ".ipynb", ".svg",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".ico"}


def read_text(path, max_chars):
    for enc in ("utf-8", "utf-8-sig", "gbk", "big5", "latin-1"):
        try:
            with open(path, "r", encoding=enc, errors="strict") as fh:
                data = fh.read(max_chars + 1)
            break
        except (UnicodeDecodeError, LookupError):
            continue
        except OSError as exc:
            return f"（读取失败：{exc}）"
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(max_chars + 1)
    if len(data) > max_chars:
        data = data[:max_chars] + f"\n…（已截断到 {max_chars} 字）"
    return data


def pdf_text(path, max_chars):
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            return None, "（本机解释器没有 pypdf，PDF 未能解析）"
    try:
        reader = PdfReader(path)
        pages = len(reader.pages)
        chunks, used = [], 0
        for index, page in enumerate(reader.pages):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                text = f"（第 {index + 1} 页解析失败：{exc}）"
            if not text.strip():
                continue
            chunks.append(f"----- 第 {index + 1} 页 -----\n{text.strip()}")
            used += len(text)
            if used > max_chars:
                chunks.append(f"…（共 {pages} 页，已截断）")
                break
        body = "\n\n".join(chunks)
        if not body.strip():
            return f"（{pages} 页，未提取到文本层，可能是扫描件）", None
        return body, None
    except Exception as exc:  # noqa: BLE001
        return None, f"（PDF 解析失败：{exc}）"


def docx_text(path, max_chars):
    try:
        import docx  # type: ignore

        document = docx.Document(path)
        parts = [p.text for p in document.paragraphs if p.text and p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        body = "\n".join(parts)
        if body.strip():
            return body[:max_chars], None
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        return None, f"（docx 解析失败：{exc}）"
    # 退路：docx 就是个 zip，直接抠 word/document.xml
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
        text = re.sub(r"<[^>]+>", "", xml)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:max_chars], None
    except Exception as exc:  # noqa: BLE001
        return None, f"（docx 解析失败：{exc}）"


def zip_text(path, max_chars):
    try:
        with zipfile.ZipFile(path) as zf:
            entries = zf.infolist()
            listing = [f"{e.filename}  ({e.file_size} B)" for e in entries[:400]]
            lines = ["压缩包内容：", *listing]
            if len(entries) > 400:
                lines.append(f"…（共 {len(entries)} 个条目）")
            budget = max_chars
            used = 0
            for entry in entries:
                if entry.is_dir():
                    continue
                ext = os.path.splitext(entry.filename)[1].lower()
                if ext not in TEXT_EXT or used >= budget:
                    continue
                try:
                    raw = zf.read(entry)
                except Exception:
                    continue
                text = decode_bytes(raw)
                if not text.strip():
                    continue
                share = min(4000, budget - used)
                snippet = text[:share]
                used += len(snippet)
                lines.append(f"\n----- {entry.filename} -----\n{snippet}")
            return "\n".join(lines)[:max_chars], None
    except Exception as exc:  # noqa: BLE001
        return None, f"（压缩包解析失败：{exc}）"


def decode_bytes(raw):
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def sanitize(text):
    """去掉控制字符等会让 JSON/终端错乱的内容，只保留换行与制表。"""
    if not text:
        return ""
    out = []
    for ch in text:
        code = ord(ch)
        if ch in "\n\t" or code >= 32:
            out.append(ch)
    return "".join(out)


def minimal_pdf_text(path, max_chars):
    """没有 pypdf 时的兜底：只对未压缩/Flate 的文本操作符有效。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return f"（读取失败：{exc}）"
    out = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        chunk = match.group(1)
        try:
            chunk = zlib.decompress(chunk)
        except zlib.error:
            pass
        for text in re.finditer(rb"\((?:[^()\\]|\\.)*\)", chunk):
            raw = text.group(0)[1:-1]
            raw = re.sub(rb"\\([()\\])", rb"\1", raw)
            out.append(decode_bytes(raw))
        if sum(len(x) for x in out) > max_chars:
            break
    body = " ".join(out)
    return body[:max_chars] if body.strip() else "（未提取到文本层，可能是扫描件）"


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"kind": "error", "text": "缺少路径", "meta": {}}, ensure_ascii=False))
        return 1
    path = sys.argv[1]
    max_chars = 20000
    if "--max-chars" in sys.argv:
        try:
            max_chars = int(sys.argv[sys.argv.index("--max-chars") + 1])
        except (IndexError, ValueError):
            pass
    if not os.path.exists(path):
        print(json.dumps({"kind": "error", "text": "文件不存在", "meta": {}}, ensure_ascii=False))
        return 1
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    size = os.path.getsize(path)
    kind, text, note = "binary", "", None
    try:
        if ext in IMAGE_EXT:
            kind, text = "image", f"（图片文件，{size} 字节；视觉模型可直接查看，其它成员可用读取工具打开此路径）"
        elif ext == ".pdf":
            kind = "pdf"
            text, note = pdf_text(path, max_chars)
            if text is None:
                text, note = minimal_pdf_text(path, max_chars), note
        elif ext == ".docx":
            kind = "docx"
            text, note = docx_text(path, max_chars)
            text = text or (note or "（未能解析）")
        elif ext == ".doc":
            kind, text = "doc", "（旧版 .doc 二进制格式暂不支持解出文本，请另存为 .docx 或 PDF）"
        elif ext in (".zip", ".jar", ".epub", ".apk", ".xlsx", ".pptx"):
            kind = "zip" if ext not in (".xlsx", ".pptx") else "office-zip"
            text, note = zip_text(path, max_chars)
            text = text or (note or "（未能解析）")
        elif ext in TEXT_EXT or size < 2_000_000:
            kind = "text"
            text = read_text(path, max_chars)
            if "\x00" in text[:2000] and ext not in TEXT_EXT:
                kind, text = "binary", f"（二进制文件，{size} 字节，未提取文本）"
        else:
            kind, text = "binary", f"（二进制文件，{size} 字节，未提取文本）"
    except Exception as exc:  # noqa: BLE001
        kind, text, note = "error", f"（解析异常：{exc}）", str(exc)
    payload = {
        "kind": kind,
        "text": sanitize(text or ""),
        "note": sanitize(note or "") or None,
        "meta": {"name": name, "size": size, "ext": ext},
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
