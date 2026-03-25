import shutil
import subprocess
from html import unescape
from pathlib import Path
from typing import Callable, Tuple

import pypdfium2 as pdfium
from bs4 import BeautifulSoup, NavigableString, Tag
from striprtf.striprtf import rtf_to_text

from .mistral_pdf import run_pipeline as run_pdf_pipeline


DEFAULT_PAGE_RANGE = (1, 20)
ALLOWED_EXTENSIONS = {".pdf", ".xlsx", ".doc", ".docx", ".rtf", ".txt"}


def _build_page_range(path: Path) -> Tuple[int, int]:
    if path.suffix.lower() != ".pdf":
        return DEFAULT_PAGE_RANGE
    try:
        total = len(pdfium.PdfDocument(str(path)))
    except Exception:
        return DEFAULT_PAGE_RANGE
    start = max(1, total - 19)
    end = total
    return (start, end)


def _find_soffice() -> str:
    return shutil.which("soffice") or shutil.which("libreoffice") or "soffice"


def _convert_with_libreoffice(source: Path, output_dir: Path, target_format: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        _find_soffice(),
        "--headless",
        "--convert-to",
        target_format,
        "--outdir",
        str(output_dir),
        str(source),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice conversion failed: {result.stderr or result.stdout}")
    converted = output_dir / f"{source.stem}.{target_format}"
    if not converted.exists():
        raise RuntimeError("LibreOffice did not produce converted file")
    return converted


def _clean_whitespace(text: str) -> str:
    return " ".join((text or "").split())


def _strip_non_table_attrs(tag: Tag) -> None:
    allowed_table_attrs = {"colspan", "rowspan"}
    if not isinstance(tag, Tag):
        return
    if tag.name in {"table", "thead", "tbody", "tfoot", "tr", "th", "td"}:
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in allowed_table_attrs}
    else:
        tag.attrs = {}


def _convert_table_to_html(table_tag: Tag) -> str:
    table_copy = BeautifulSoup(str(table_tag), "html.parser")
    root = table_copy.find("table")
    if not root:
        return ""
    allowed_table_tags = {"table", "thead", "tbody", "tfoot", "tr", "th", "td"}

    # Unwrap inline/layout tags from LibreOffice output and keep only table structure.
    for tag in list(root.find_all(True)):
        tag_name = (tag.name or "").lower()
        if tag_name in allowed_table_tags:
            continue
        if tag_name in {"col", "colgroup"}:
            tag.decompose()
            continue
        tag.unwrap()

    for cell in root.find_all(["th", "td"]):
        text = _clean_whitespace(cell.get_text(" ", strip=True))
        cell.clear()
        if text:
            cell.append(NavigableString(text))

    for tag in root.find_all(True):
        _strip_non_table_attrs(tag)
    _strip_non_table_attrs(root)
    return str(root)


def _node_to_markdown(node, list_level: int = 0) -> list[str]:
    if isinstance(node, NavigableString):
        text = _clean_whitespace(unescape(str(node)))
        return [text] if text else []

    if not isinstance(node, Tag):
        return []

    name = (node.name or "").lower()
    if name in {"style", "script", "noscript", "meta", "link", "svg"}:
        return []

    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(name[1])
        text = _clean_whitespace(node.get_text(" ", strip=True))
        return [f"{'#' * level} {text}", ""] if text else []

    if name == "p":
        text = _clean_whitespace(node.get_text(" ", strip=True))
        return [text, ""] if text else []

    if name in {"ul", "ol"}:
        lines: list[str] = []
        index = 1
        for li in node.find_all("li", recursive=False):
            prefix = f"{index}. " if name == "ol" else "- "
            li_parts: list[str] = []
            for child in li.contents:
                if isinstance(child, Tag) and child.name in {"ul", "ol"}:
                    continue
                if isinstance(child, NavigableString):
                    text_part = _clean_whitespace(unescape(str(child)))
                elif isinstance(child, Tag):
                    text_part = _clean_whitespace(child.get_text(" ", strip=True))
                else:
                    text_part = ""
                if text_part:
                    li_parts.append(text_part)
            li_text = _clean_whitespace(" ".join(li_parts))
            if li_text:
                lines.append(f"{'  ' * list_level}{prefix}{li_text}")
            for child_list in li.find_all(["ul", "ol"], recursive=False):
                lines.extend(_node_to_markdown(child_list, list_level + 1))
            if name == "ol":
                index += 1
        lines.append("")
        return lines

    if name == "table":
        html_table = _convert_table_to_html(node)
        return [html_table, ""] if html_table else []

    lines: list[str] = []
    for child in node.children:
        lines.extend(_node_to_markdown(child, list_level=list_level))
    return lines


def _html_to_markdown(html_text: str) -> str:
    soup = BeautifulSoup(html_text or "", "html.parser")
    root = soup.body or soup
    lines: list[str] = []
    for child in root.children:
        lines.extend(_node_to_markdown(child))

    normalized: list[str] = []
    prev_blank = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not prev_blank:
                normalized.append("")
            prev_blank = True
            continue
        normalized.append(line.rstrip())
        prev_blank = False
    return "\n".join(normalized).strip()


def convert_to_markdown(path: Path, update_status: Callable[[str], None] | None = None) -> str:
    update_status = update_status or (lambda msg: None)
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported format: {suffix}")

    if suffix == ".txt":
        markdown = path.read_text(encoding="utf-8", errors="ignore").strip()
        print(f"[doc-to-md] converted_markdown_txt={markdown}")
        return markdown

    if suffix == ".rtf":
        # Quick path for plain RTF text where LibreOffice is unavailable.
        content = path.read_text(encoding="utf-8", errors="ignore")
        quick_text = rtf_to_text(content).strip()
        if quick_text:
            print(f"[doc-to-md] converted_markdown_rtf={quick_text}")
            return quick_text

    if suffix != ".pdf":
        update_status("Office document conversion to HTML started.")
        converted_html = _convert_with_libreoffice(path, path.parent, "html")
        html_content = converted_html.read_text(encoding="utf-8", errors="ignore")
        markdown = _html_to_markdown(html_content)
        if not markdown:
            raise RuntimeError("LibreOffice HTML conversion produced empty markdown")
        print(f"[doc-to-md] converted_markdown_non_pdf={markdown}")
        return markdown

    if suffix == ".pdf":
        update_status("PDF conversion started.")
        page_range = _build_page_range(path)
        markdown = run_pdf_pipeline(str(path), update_status, {}, page_range)
        print(f"[doc-to-md] converted_markdown_pdf={markdown}")
        return markdown

    raise ValueError(f"Unsupported format: {suffix}")
