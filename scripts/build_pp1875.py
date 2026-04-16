"""
Parse ПП 1875 appendices (consultant.ru MHTML saves) into a flat ОКПД2 → regime JSON.

Source MHTML files must live in one directory. They are identified by the
consultant.ru URL embedded in the MHTML part's `Content-Location` header:

  dc1993c2b5c2478f2ab15f73dba12882e6b458c8 → Приложение 1 (ban)
  b0541e42afa5defcd0520054f0979f05aff711fb → Приложение 2 (restriction)
  3040cb5a1945ab3d2bbe52797e35a36534993493 → Приложение 3 (min share)
  c3836295ac8c3eb64fa7456f31294ffaae6a69e9 → Приложение 4 (medical devices — no ОКПД2)

Usage:
    python scripts/build_pp1875.py \
        --src "C:/path/to/mhtml_dir" \
        --out app/data/pp1875.json

Output:
  {
    "_summary": {...},
    "okpd2": {
      "13.2": {
        "regime": "ban",
        "appendix": 1,
        "position": 1,
        "name": "Ткани текстильные",
        "source_url": "..."
      },
      ...
    },
    "appendix_4_medical_devices": [
      {"position": 1, "name": "...", "max_foreign_share_pct": 20, "required_document": "..."}
    ]
  }

The same ОКПД2 can appear in several appendices — we merge them into one
entry with per-appendix blocks so the checker can enforce the strictest rule.
"""
from __future__ import annotations

import argparse
import email
import html as html_lib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator, Optional


APPENDIX_SLUG_TO_INFO = {
    "dc1993c2b5c2478f2ab15f73dba12882e6b458c8": {
        "appendix": 1,
        "regime": "ban",
        "label": "Запрет закупок иностранных товаров (ПП 1875, приложение 1)",
    },
    "b0541e42afa5defcd0520054f0979f05aff711fb": {
        "appendix": 2,
        "regime": "restriction",
        "label": "Ограничения допуска иностранных товаров (ПП 1875, приложение 2)",
    },
    "3040cb5a1945ab3d2bbe52797e35a36534993493": {
        "appendix": 3,
        "regime": "minimum_share",
        "label": "Минимальная обязательная доля закупок российских товаров (ПП 1875, приложение 3)",
    },
    "c3836295ac8c3eb64fa7456f31294ffaae6a69e9": {
        "appendix": 4,
        "regime": "medical_device_share",
        "label": "Предельные значения доли иностранных материалов (ПП 1875, приложение 4)",
    },
}


@dataclass
class Row:
    """A single table row, unpacked into plain-text cells."""

    cells: list[str]
    colspans: list[int]

    def flat_text(self, idx: int) -> str:
        return self.cells[idx] if idx < len(self.cells) else ""


# ---------------------------------------------------------------------------
# MHTML loader
# ---------------------------------------------------------------------------


def load_html_from_mhtml(path: Path) -> tuple[str, str]:
    """Return (html_text, source_url). source_url is the Content-Location of the HTML part."""
    with open(path, "rb") as fh:
        msg = email.message_from_binary_file(fh)
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        body = part.get_payload(decode=True)
        charset = part.get_content_charset() or "utf-8"
        text = body.decode(charset, errors="replace")
        return text, part.get("Content-Location", "") or ""
    raise RuntimeError(f"No text/html part found in {path.name}")


def detect_appendix(source_url: str) -> Optional[dict]:
    for slug, info in APPENDIX_SLUG_TO_INFO.items():
        if slug in source_url:
            return {**info, "source_url": source_url}
    return None


# ---------------------------------------------------------------------------
# Table extractor — one-table-per-page, which holds for all 4 appendices.
# ---------------------------------------------------------------------------


class _TableRowsParser(HTMLParser):
    """Extract rows from the FIRST <table> encountered.

    Each cell becomes a plain-text string (tags stripped, whitespace collapsed).
    Line breaks inside a <br> are preserved as '\n' so multi-value cells stay
    recognizable later.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_table = False
        self._table_level = 0  # handle nested tables defensively
        self._in_row = False
        self._in_cell = False
        self._cell_colspan = 1
        self._cell_chunks: list[str] = []
        self._row_cells: list[str] = []
        self._row_colspans: list[int] = []
        self.rows: list[Row] = []
        self.seen_first_table = False

    # --- tag handlers ---------------------------------------------------

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "table":
            if not self.seen_first_table:
                self._in_table = True
                self.seen_first_table = True
            self._table_level += 1
            return
        if not self._in_table:
            return
        if t == "tr":
            self._in_row = True
            self._row_cells = []
            self._row_colspans = []
        elif t in ("td", "th"):
            self._in_cell = True
            self._cell_chunks = []
            span = 1
            for name, val in attrs:
                if name.lower() == "colspan":
                    try:
                        span = max(1, int(val))
                    except (TypeError, ValueError):
                        pass
            self._cell_colspan = span
        elif t == "br" and self._in_cell:
            self._cell_chunks.append("\n")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "table":
            self._table_level -= 1
            if self._table_level <= 0:
                self._in_table = False
            return
        if not self._in_table:
            return
        if t == "tr":
            if self._in_row:
                self.rows.append(Row(cells=self._row_cells, colspans=self._row_colspans))
            self._in_row = False
        elif t in ("td", "th") and self._in_cell:
            text = "".join(self._cell_chunks)
            # Collapse whitespace but keep explicit newlines
            lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
            text = "\n".join(ln for ln in lines if ln)
            self._row_cells.append(text)
            self._row_colspans.append(self._cell_colspan)
            self._in_cell = False

    def handle_data(self, data):
        if self._in_cell:
            self._cell_chunks.append(data)


def extract_rows(html: str) -> list[Row]:
    p = _TableRowsParser()
    p.feed(html)
    return p.rows


# ---------------------------------------------------------------------------
# ОКПД2 extraction
# ---------------------------------------------------------------------------

# Only accept OKPD2-shaped codes. Allows XX, XX.X, XX.XX, XX.XX.X, XX.XX.XX,
# XX.XX.XX.XXX; leading 2-digit class is mandatory.
OKPD_PATTERN = re.compile(r"\b(\d{2}(?:\.\d{1,2}){0,3}(?:\.\d{3})?)\b")


def extract_okpd2_codes(cell_text: str) -> list[str]:
    """Pull all OKPD2 codes from a code cell, preserving order and uniqueness."""
    seen: list[str] = []
    for m in OKPD_PATTERN.finditer(cell_text):
        code = m.group(1)
        # Guard against numbers that look like codes but are clearly something else
        # (e.g. "2024" → won't match since no dot). Real OKPD classes start at 01.
        if code.count(".") == 0:
            continue
        parts = code.split(".")
        first = int(parts[0])
        if first < 1 or first > 99:
            continue
        if code not in seen:
            seen.append(code)
    return seen


def parse_position_number(cell_text: str) -> Optional[int]:
    m = re.search(r"\b(\d{1,4})\b", cell_text)
    return int(m.group(1)) if m else None


def parse_percent(cell_text: str) -> Optional[int]:
    """Extract first integer-looking percent value from a cell."""
    # Values like "80", "не более 20", "90%", "10"
    m = re.search(r"(\d{1,3})", cell_text)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Per-appendix row interpretation
# ---------------------------------------------------------------------------


def parse_appendix_1_or_2(rows: list[Row], appendix_info: dict) -> list[dict]:
    """Columns: [№] [Наименование] [ОКПД2].  Header is row 0."""
    out: list[dict] = []
    for r in rows[1:]:
        if len(r.cells) < 3:
            continue
        position = parse_position_number(r.cells[0])
        name = r.cells[1].strip()
        okpd_cell = r.cells[2]
        codes = extract_okpd2_codes(okpd_cell)
        if not codes or not name or position is None:
            continue
        for code in codes:
            out.append(
                {
                    "okpd2": code,
                    "appendix": appendix_info["appendix"],
                    "regime": appendix_info["regime"],
                    "position": position,
                    "name": name,
                    "source_url": appendix_info["source_url"],
                }
            )
    return out


def parse_appendix_3(rows: list[Row], appendix_info: dict) -> list[dict]:
    """Columns: [№] [Наименование] [ОКПД2] [% мин. доли]."""
    out: list[dict] = []
    for r in rows[1:]:
        if len(r.cells) < 4:
            continue
        position = parse_position_number(r.cells[0])
        name = r.cells[1].strip()
        codes = extract_okpd2_codes(r.cells[2])
        min_share = parse_percent(r.cells[3])
        if not codes or not name or position is None:
            continue
        for code in codes:
            out.append(
                {
                    "okpd2": code,
                    "appendix": appendix_info["appendix"],
                    "regime": appendix_info["regime"],
                    "position": position,
                    "name": name,
                    "min_share_percent": min_share,
                    "source_url": appendix_info["source_url"],
                }
            )
    return out


def parse_appendix_4(rows: list[Row], appendix_info: dict) -> list[dict]:
    """Columns: [№] [Наименование МИ] [% доли материалов] [Подтверждающий документ].

    No ОКПД2. We return a separate list that gets attached at top level.
    """
    out: list[dict] = []
    for r in rows[1:]:
        if len(r.cells) < 4:
            continue
        position = parse_position_number(r.cells[0])
        name = r.cells[1].strip()
        max_share = parse_percent(r.cells[2])
        doc = r.cells[3].strip()
        if position is None or not name:
            continue
        out.append(
            {
                "appendix": 4,
                "position": position,
                "name": name,
                "max_foreign_share_pct": max_share,
                "required_document": doc,
                "source_url": appendix_info["source_url"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Merge & finalize
# ---------------------------------------------------------------------------


def merge(all_entries: list[dict]) -> dict:
    """Group по ОКПД2. Один код может попасть в несколько приложений (например,
    часть 878-электроники есть одновременно в запрете и ограничении — приоритет
    запрета выше)."""
    by_code: dict[str, dict] = {}
    for e in all_entries:
        code = e["okpd2"]
        slot = by_code.setdefault(code, {"okpd2": code, "entries": []})
        block = {k: v for k, v in e.items() if k != "okpd2"}
        slot["entries"].append(block)
    # Order entries by appendix number so downstream sees predictable priority
    for slot in by_code.values():
        slot["entries"].sort(key=lambda b: (b["appendix"], b.get("position", 0)))
    return by_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", type=Path, required=True, help="Directory with MHTML files")
    parser.add_argument("--out", type=Path, required=True, help="Output JSON path")
    args = parser.parse_args()

    if not args.src.is_dir():
        print(f"ERROR: {args.src} is not a directory", file=sys.stderr)
        return 2

    mhtml_files = sorted(args.src.glob("*.mhtml"))
    if not mhtml_files:
        print(f"ERROR: no *.mhtml found in {args.src}", file=sys.stderr)
        return 2

    all_entries: list[dict] = []
    appendix_4_items: list[dict] = []
    per_file_stats: list[dict] = []

    for fp in mhtml_files:
        html, url = load_html_from_mhtml(fp)
        info = detect_appendix(url)
        if info is None:
            print(f"[skip] unknown appendix slug in {fp.name}: {url}")
            continue

        rows = extract_rows(html)
        if info["appendix"] in (1, 2):
            entries = parse_appendix_1_or_2(rows, info)
            all_entries.extend(entries)
            per_file_stats.append(
                {"file": fp.name, "appendix": info["appendix"], "rows_in_table": len(rows), "entries_extracted": len(entries)}
            )
        elif info["appendix"] == 3:
            entries = parse_appendix_3(rows, info)
            all_entries.extend(entries)
            per_file_stats.append(
                {"file": fp.name, "appendix": 3, "rows_in_table": len(rows), "entries_extracted": len(entries)}
            )
        elif info["appendix"] == 4:
            items = parse_appendix_4(rows, info)
            appendix_4_items.extend(items)
            per_file_stats.append(
                {"file": fp.name, "appendix": 4, "rows_in_table": len(rows), "entries_extracted": len(items)}
            )

    okpd2 = merge(all_entries)

    # Summary
    by_appendix: dict[int, int] = {}
    codes_by_appendix: dict[int, set[str]] = {}
    for code, slot in okpd2.items():
        for b in slot["entries"]:
            by_appendix[b["appendix"]] = by_appendix.get(b["appendix"], 0) + 1
            codes_by_appendix.setdefault(b["appendix"], set()).add(code)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "ПП РФ № 1875 от 23.12.2024 (консультант+)",
        "per_file": per_file_stats,
        "unique_okpd2_total": len(okpd2),
        "entries_per_appendix": by_appendix,
        "unique_okpd2_per_appendix": {k: len(v) for k, v in codes_by_appendix.items()},
        "appendix_4_items_count": len(appendix_4_items),
    }

    out = {
        "_summary": summary,
        "okpd2": {k: okpd2[k] for k in sorted(okpd2)},
        "appendix_4_medical_devices": appendix_4_items,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print()
    print("=== ПП 1875 parse summary ===")
    for s in per_file_stats:
        print(f"  app {s['appendix']}: rows={s['rows_in_table']:>4}  entries={s['entries_extracted']:>4}  ({s['file'][:60]}...)")
    print(f"  unique ОКПД2 total: {summary['unique_okpd2_total']}")
    for app_no in sorted(by_appendix):
        print(f"  приложение {app_no}: {by_appendix[app_no]} записей, {summary['unique_okpd2_per_appendix'].get(app_no, 0)} уникальных ОКПД2")
    print(f"  приложение 4 (мед. изделия без ОКПД2): {summary['appendix_4_items_count']} позиций")
    print(f"  written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
