import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Tuple

import pypdfium2 as pdfium
from striprtf.striprtf import rtf_to_text

from .docling_simple_pdf import run_pipeline as run_pdf_pipeline
from .docling_doc import run_pipeline as run_office_pipeline

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


def convert_to_markdown(path: Path, update_status: Callable[[str], None] | None = None) -> str:
    update_status = update_status or (lambda msg: None)
    suffix = path.suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported format: {suffix}")

    if suffix == ".txt":
        return path.read_text(encoding="utf-8", errors="ignore").strip()

    if suffix == ".rtf":
        content = path.read_text(encoding="utf-8", errors="ignore")
        return rtf_to_text(content).strip()

    if suffix == ".doc":
        converted = _convert_with_libreoffice(path, path.parent, "docx")
        suffix = ".docx"
        path = converted

    if suffix in {".docx", ".xlsx"}:
        update_status("Office document conversion started.")
        return run_office_pipeline(str(path), update_status)

    if suffix == ".pdf":
        update_status("PDF conversion started.")
        page_range = _build_page_range(path)
        return run_pdf_pipeline(str(path), update_status, {}, page_range)

    raise ValueError(f"Unsupported format: {suffix}")
