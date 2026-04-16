"""
Parse ПП 719 (appendix with technological requirements) into a flat
ОКПД2 → {current_threshold, upcoming[]} JSON справочник.

The source is the Контур.Норматив MHTML export of the full act (including all
28 sections I–XXVIII). The main приложение lives in a single <table>
с тремя колонками: ОКПД2 | Наименование | Требования.

Thresholds live INSIDE the Требования cell. Two shapes:

  1. Временная шкала:
       «...оцениваемых суммарным количеством баллов,
        с 2022 года - не менее 18 баллов,
        с 2024 года - не менее 22 баллов,
        с 2026 года - не менее 26 баллов: ...»

  2. Одиночный порог:
       «...совокупное количество баллов должно составлять не менее 75 баллов...»

The Требования cell often uses rowspan="3" and applies to a block of 3
adjacent ОКПД2 codes (e.g. шлифовальные/отрезные/полировальные круги).

Usage:
    python scripts/build_pp719.py \
        --src "C:/path/mhtml" \
        --out app/data/pp719_thresholds.json \
        [--as-of 2026-04-16]

The current threshold is chosen as the latest (from_date ≤ as_of) from the
parsed schedule. Future-only points go into `upcoming` for UI warnings.
Historical points (superseded) are dropped.
"""
from __future__ import annotations

import argparse
import email
import html as html_lib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# MHTML loader
# ---------------------------------------------------------------------------


def load_html_from_mhtml(path: Path) -> tuple[str, str]:
    with open(path, "rb") as fh:
        msg = email.message_from_binary_file(fh)
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        body = part.get_payload(decode=True)
        charset = part.get_content_charset() or "utf-8"
        text = body.decode(charset, errors="replace")
        return text, part.get("Content-Location", "") or ""
    raise RuntimeError(f"No text/html part in {path.name}")


# ---------------------------------------------------------------------------
# Table walker that respects rowspan
# ---------------------------------------------------------------------------


@dataclass
class Cell:
    text: str
    colspan: int = 1
    rowspan: int = 1


class _TableParser(HTMLParser):
    """Parse a single table fragment into list[Row] (each row = list[Cell]).

    Supports nested tables (section XXVIII has 38 of them): rows from
    inner tables are appended to the outer table's row list in document
    order, which is fine for our extraction — the visible layout keeps
    the ОКПД2 codes paired with their requirements cells regardless of
    the nesting level.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_tr = False
        self._in_cell = False
        self._cell_attrs: list[tuple[str, Optional[str]]] = []
        self._cell_chunks: list[str] = []
        self._row_cells: list[Cell] = []
        self.rows: list[list[Cell]] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "tr":
            self._in_tr = True
            self._row_cells = []
        elif t in ("td", "th"):
            self._in_cell = True
            self._cell_attrs = attrs
            self._cell_chunks = []
        elif t in ("br", "p") and self._in_cell:
            self._cell_chunks.append("\n")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "tr" and self._in_tr:
            self.rows.append(self._row_cells)
            self._in_tr = False
        elif t in ("td", "th") and self._in_cell:
            text = "".join(self._cell_chunks)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{2,}", "\n", text).strip()
            colspan = 1
            rowspan = 1
            for name, val in self._cell_attrs:
                if not val:
                    continue
                if name.lower() == "colspan":
                    try:
                        colspan = max(1, int(val))
                    except ValueError:
                        pass
                elif name.lower() == "rowspan":
                    try:
                        rowspan = max(1, int(val))
                    except ValueError:
                        pass
            self._row_cells.append(Cell(text=text, colspan=colspan, rowspan=rowspan))
            self._in_cell = False

    def handle_data(self, data):
        if self._in_cell:
            self._cell_chunks.append(data)


def parse_rows(html_fragment: str) -> list[list[Cell]]:
    p = _TableParser()
    p.feed(html_fragment)
    return p.rows


# ---------------------------------------------------------------------------
# Normalize rows to a grid — so that rowspan cells propagate downwards
# ---------------------------------------------------------------------------


def expand_rowspans(rows: list[list[Cell]]) -> list[list[Cell]]:
    """
    Convert a list-of-rows (where each row is a list of visible Cell objects)
    into a rectangular grid where rowspan'd cells appear explicitly on every
    row they span. Colspan is kept on the leftmost cell only (we do not need
    to split the text across logical columns).
    """
    grid: list[list[Cell]] = []
    # pending[col] = (cell, remaining_rows)
    pending: dict[int, tuple[Cell, int]] = {}

    for row in rows:
        out_row: list[Cell] = []
        max_col = 0
        src_iter = iter(row)
        col = 0
        while True:
            # First consume pending rowspan cells at current col
            while col in pending:
                cell, left = pending[col]
                out_row.append(cell)
                left -= 1
                if left <= 0:
                    del pending[col]
                else:
                    pending[col] = (cell, left)
                col += cell.colspan
            try:
                cell = next(src_iter)
            except StopIteration:
                break
            out_row.append(cell)
            if cell.rowspan > 1:
                pending[col] = (cell, cell.rowspan - 1)
            col += cell.colspan
            max_col = max(max_col, col)
        # drain trailing pending cells
        while pending:
            next_col = min(pending)
            if next_col < col:
                # already past it
                cell, left = pending[next_col]
                left -= 1
                if left <= 0:
                    del pending[next_col]
                else:
                    pending[next_col] = (cell, left)
                continue
            cell, left = pending[next_col]
            # fill gap
            out_row.append(cell)
            left -= 1
            if left <= 0:
                del pending[next_col]
            else:
                pending[next_col] = (cell, left)
            col = next_col + cell.colspan
        grid.append(out_row)
    return grid


# ---------------------------------------------------------------------------
# OKPD2 extraction
# ---------------------------------------------------------------------------

OKPD_PATTERN = re.compile(r"\b(\d{2}(?:\.\d{1,3}){0,4})\b")


def extract_okpd2(text: str) -> list[str]:
    out: list[str] = []
    for m in OKPD_PATTERN.finditer(text):
        code = m.group(1)
        if "." not in code:
            continue
        first = code.split(".")[0]
        try:
            if not 1 <= int(first) <= 99:
                continue
        except ValueError:
            continue
        if code not in out:
            out.append(code)
    return out


# ---------------------------------------------------------------------------
# Threshold parsing from the "Требования" cell
# ---------------------------------------------------------------------------

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}


@dataclass
class ThresholdPoint:
    from_date: date
    min_score: int


def _parse_year_only(phrase: str) -> Optional[date]:
    """«с 2022 года» / «с 2024 г.» → date(2022, 1, 1)"""
    m = re.search(r"с\s+(\d{4})\s+год", phrase)
    if m:
        return date(int(m.group(1)), 1, 1)
    return None


def _parse_day_month_year(phrase: str) -> Optional[date]:
    """«с 1 сентября 2026 г.» → date(2026, 9, 1)"""
    m = re.search(r"с\s+(\d{1,2})\s+([а-яё]+)\s+(\d{4})", phrase, flags=re.IGNORECASE)
    if m:
        day = int(m.group(1))
        mon = MONTHS.get(m.group(2).lower())
        if mon is not None:
            return date(int(m.group(3)), mon, day)
    return None


def _parse_month_year(phrase: str) -> Optional[date]:
    """«с января 2026 г.» → date(2026, 1, 1) — rare fallback"""
    m = re.search(r"с\s+([а-яё]+)\s+(\d{4})", phrase, flags=re.IGNORECASE)
    if m:
        mon = MONTHS.get(m.group(1).lower())
        if mon is not None:
            return date(int(m.group(2)), mon, 1)
    return None


def parse_thresholds(cell_text: str) -> list[ThresholdPoint]:
    """Extract a schedule of thresholds from a Требования cell.

    Strategy:
      Find every «<time-prefix> - не менее N баллов» or «<time-prefix> не менее N баллов»
      occurrence. The time-prefix is the text since the previous comma up to
      the number, searched for a date/year marker.

      If no time-prefix is found but a single «не менее N баллов» exists near
      the words «совокупное/суммарное количество», treat it as an untimed
      threshold effective from epoch (so it'll always be current).
    """
    if not cell_text:
        return []

    points: list[ThresholdPoint] = []

    # Two threshold shapes. Both are clear thresholds (not operation scores):
    #
    #   A. «не менее N баллов» — canonical phrasing
    #   B. «", - N балл(а/ов)» — used in inline references like
    #        «из 27.12.10.110 "Выключатели...", - 82 балла, с 1 сентября 2026 г. - 92 балла»
    #
    # Operation scores look like «сборка (5 баллов)» — wrapped in parens,
    # never preceded by «не менее» or by a quote+dash. We don't match those.
    combined = re.compile(
        r'(?:не\s+менее\s+(\d{1,4})\s+балл'
        r'|"\s*,\s*[-—−]\s+(\d{1,4})\s+балл'
        r'|г\.\s*[-—−]\s+(\d{1,4})\s+балл)'
    )

    for m in combined.finditer(cell_text):
        score = int(m.group(1) or m.group(2) or m.group(3))
        lookback = cell_text[max(0, m.start() - 200) : m.start()]
        candidates = [rm.start() for rm in re.finditer(r"с\s+\d", lookback)]
        from_date: Optional[date] = None
        if candidates:
            date_phrase = lookback[candidates[-1] : candidates[-1] + 60]
            from_date = _parse_day_month_year(date_phrase) or _parse_year_only(date_phrase)
        if from_date is None:
            points.append(ThresholdPoint(from_date=date(1900, 1, 1), min_score=score))
        else:
            points.append(ThresholdPoint(from_date=from_date, min_score=score))

    if not points:
        return []

    # If the cell has a mix of dated and undated, drop undated duplicates with
    # the same score as a dated one (they're the same requirement repeated).
    dated_scores = {p.min_score for p in points if p.from_date != date(1900, 1, 1)}
    if dated_scores:
        points = [p for p in points if p.from_date != date(1900, 1, 1) or p.min_score not in dated_scores]

    # De-duplicate (same date+score)
    seen = set()
    out: list[ThresholdPoint] = []
    for p in sorted(points, key=lambda x: (x.from_date, x.min_score)):
        key = (p.from_date, p.min_score)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Walk the grid → entries
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    okpd2: str
    name: str
    section: str
    thresholds: list[ThresholdPoint] = field(default_factory=list)


def extract_section_segments(html: str) -> list[tuple[str, str]]:
    """Split document text into (section_name, section_html) segments.

    Sections are delimited by `<a name="lNN"></a>{RomanNumeral}. Title</td>`.
    The last section runs to the end of the HTML, because the «Примечания»
    block is followed by multiple *addendum* tables (paragraphs 5, 38, 39 …)
    that carry per-year score matrices referencing earlier sections. Those
    matrices MUST be parsed — they're the only place where 2024/2025/2026/
    2028 thresholds live for radioelectronics and automotive products.
    """
    markers: list[tuple[int, str]] = []
    for m in re.finditer(r'<a name="l\d+"></a>([IVXLC]+)\.\s+([^<]{5,200})</td>', html):
        name = f"{m.group(1)}. {html_lib.unescape(m.group(2)).strip()}"
        markers.append((m.start(), name))
    if not markers:
        return []

    # Also grab «5. …», «38. …», «39. …» addendum paragraphs that contain
    # year-matrix tables. They appear AFTER the document «Примечания» block.
    # We use a conservative pattern: «<a name="lNN"></a>NN. <prose>» where the
    # prose mentions продукции/баллов — to avoid false positives.
    last_notes_iter = list(re.finditer(r"Примечания", html))
    notes_anchor = last_notes_iter[-1].start() if last_notes_iter else 0
    addendum_pat = re.compile(r'<a name="l\d+"></a>(\d{1,3})\.\s+([^<]{10,400}?балл[^<]{0,400})</p>')
    for m in addendum_pat.finditer(html, pos=notes_anchor):
        title = f"Прим. {m.group(1)}. {html_lib.unescape(m.group(2)).strip()[:120]}"
        markers.append((m.start(), title))

    markers.sort(key=lambda x: x[0])

    segments: list[tuple[str, str]] = []
    for i, (pos, name) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(html)
        segments.append((name, html[pos:end]))
    return segments


YEAR_HEADER_RE = re.compile(r"(\d{4})\s*год")


def _detect_year_header(row: list[Cell]) -> list[Optional[int]]:
    """Per-column list of year ints if this row is a year header; else all None.

    A header row looks like: [Код | Наименование | 2024 год | 2025 год | 2026 год | 2027 год | 2028 год и далее].
    """
    years: list[Optional[int]] = []
    for c in row:
        m = YEAR_HEADER_RE.search(c.text)
        if m:
            years.append(int(m.group(1)))
        else:
            years.append(None)
    return years


def _extract_year_matrix_thresholds(
    row: list[Cell], year_header: list[Optional[int]]
) -> list["ThresholdPoint"]:
    """For a data row under a year-matrix header, pull «не менее N баллов»
    out of each cell whose column corresponds to a year."""
    pts: list[ThresholdPoint] = []
    for col_idx, year in enumerate(year_header):
        if year is None or col_idx >= len(row):
            continue
        text = row[col_idx].text
        m = re.search(r"не\s+менее\s+(\d{1,4})\s+балл", text)
        if m:
            pts.append(ThresholdPoint(from_date=date(year, 1, 1), min_score=int(m.group(1))))
    return pts


def _split_multiline_codes(text: str) -> list[str]:
    """A year-matrix code cell often packs several ОКПД2 on separate lines.

    Example cell text:
        "29.32.30.320\n29.32.30.321\n29.32.30.322"
    """
    return extract_okpd2(text)


def _collect_record_blocks(grid: list[list[Cell]]) -> list[tuple[list[str], str, str]]:
    """Walk the grid, grouping contiguous rows into "records".

    Each record is:  (okpd2_codes_list, name, full_requirements_text)

    Records start when we see a row whose first cell contains an ОКПД2 code.
    Subsequent rows with an empty first cell (just prose continuation in
    col 2) are appended to the current record's requirements text.

    A special case: some records place multiple codes in *consecutive* rows
    before the requirements text kicks in (see section II — five codes then
    one huge 3rd cell). We collect those extra codes as long as col 2 is
    empty-ish (<20 chars) and col 0 is a code.
    """
    records: list[tuple[list[str], str, str]] = []
    current_codes: list[str] = []
    current_name: str = ""
    current_req_chunks: list[str] = []

    def flush():
        nonlocal current_codes, current_name, current_req_chunks
        if current_codes:
            req = "\n".join(current_req_chunks).strip()
            records.append((current_codes, current_name, req))
        current_codes = []
        current_name = ""
        current_req_chunks = []

    for row in grid:
        if not row:
            continue
        first = row[0].text if len(row) >= 1 else ""
        second = row[1].text if len(row) >= 2 else ""
        third = row[2].text if len(row) >= 3 else ""

        codes_in_first = extract_okpd2(first)

        if codes_in_first and not (len(first) > 80 and not re.match(r"^\s*\d{2}", first.strip())):
            # A row that starts a new ОКПД2 entry OR adds sibling codes
            if current_codes and not current_req_chunks and len(second.strip()) < 5 and not third.strip():
                # Sibling code row — same block, no requirements text yet
                current_codes.extend(codes_in_first)
                if second.strip():
                    current_name = second.strip()
                continue
            # Otherwise — close previous record and open a new one
            flush()
            current_codes = list(codes_in_first)
            current_name = second.strip()
            if third.strip():
                current_req_chunks.append(third)
        else:
            # Continuation row — append 3rd cell text to current record
            if current_codes and third.strip():
                current_req_chunks.append(third)

    flush()
    return records


def extract_section_plaintext(html_fragment: str) -> str:
    """Return a readable plain-text dump of the section html (for inline
    threshold references that sit outside tables)."""
    t = re.sub(r"<br[^>]*>", "\n", html_fragment)
    t = re.sub(r"</p>|</li>|</tr>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html_lib.unescape(t)
    # collapse whitespace but keep sentence structure
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t


INLINE_REF_RE = re.compile(
    r"из\s+(\d{2}(?:\.\d{1,3}){1,3})\s+"           # code like 27.12.10.110
    r'(?:"[^"]{0,200}")?'                            # optional quoted name
    r"[^.\n]{0,400}?"                                # up to ~400 chars before threshold
    r"[-—−]\s+(\d{1,4})\s+балл"                      # first dated/initial threshold
)


def extract_inline_threshold_refs(plain_text: str) -> dict[str, list[ThresholdPoint]]:
    """Find «из X.X.X.X ... - N балла[, с дата - M балла]» references.

    Typical form in Раздел V / XXI notes:
        «классифицируемой кодом … из 27.12.10.110 "Выключатели силовые
         высоковольтные напряжением 6 кВ и выше", - 82 балла, с 1 сентября
         2026 г. - 92 балла»
    """
    out: dict[str, list[ThresholdPoint]] = {}
    for m in INLINE_REF_RE.finditer(plain_text):
        code = m.group(1)
        # Pull the full clause (from 'из' to the next semicolon/period)
        end = plain_text.find(";", m.end())
        if end < 0:
            end = plain_text.find(".", m.end())
        if end < 0:
            end = m.end() + 400
        clause = plain_text[m.start() : end + 1]
        # The first score (m.group(2)) is the "initial" threshold — no date.
        # Additional «с <дата> ... - N балла» pairs follow after.
        pts: list[ThresholdPoint] = [ThresholdPoint(from_date=date(1900, 1, 1), min_score=int(m.group(2)))]
        for dm in re.finditer(r"с\s+(?:\d{1,2}\s+)?[а-яё]*\s*\d{4}[^-—−]{0,60}[-—−]\s+(\d{1,4})\s+балл", clause, flags=re.IGNORECASE):
            phrase = clause[max(0, dm.start() - 60) : dm.end()]
            d = _parse_day_month_year(phrase) or _parse_month_year(phrase) or _parse_year_only(phrase)
            if d is None:
                continue
            pts.append(ThresholdPoint(from_date=d, min_score=int(dm.group(1))))
        out.setdefault(code, []).extend(pts)
    return out


def build_entries_from_section(section_name: str, html_fragment: str) -> list[Entry]:
    rows = parse_rows(html_fragment)
    if not rows:
        return []
    grid = expand_rowspans(rows)

    entries: list[Entry] = []

    # --- (1) Scan for year-matrix tables and emit per-year ThresholdPoints ---
    year_header: list[Optional[int]] = []
    for row in grid:
        if not row:
            continue
        maybe_years = _detect_year_header(row)
        if sum(1 for y in maybe_years if y is not None) >= 2:
            year_header = maybe_years
            continue
        if not year_header:
            continue
        # Under an active year-header, look for data rows. The code cell is
        # usually col 0; the name in col 1; scores in col 2..
        codes = _split_multiline_codes(row[0].text) if row else []
        if not codes:
            # Not a data row (formula / continuation / blank) — don't reset
            # the header, just skip; header is shared across many rows.
            continue
        pts = _extract_year_matrix_thresholds(row, year_header)
        if not pts:
            continue
        name_cell = row[1].text.strip() if len(row) > 1 else ""
        for code in codes:
            entries.append(
                Entry(
                    okpd2=code,
                    name=name_cell,
                    section=section_name,
                    thresholds=pts,
                )
            )

    # --- (2) Prose records (the main 3-column table) ---
    records = _collect_record_blocks(grid)
    for codes, name, req_text in records:
        thresholds = parse_thresholds(req_text)
        for code in codes:
            entries.append(
                Entry(
                    okpd2=code,
                    name=name,
                    section=section_name,
                    thresholds=thresholds,
                )
            )

    # --- (3) Inline threshold references in section notes ---
    # These are paragraphs like «классифицируемой кодом … из X.X.X.X …, - N балла,
    # с 1 сентября YYYY г. - M балла» that live outside the main table.
    plain = extract_section_plaintext(html_fragment)
    inline_refs = extract_inline_threshold_refs(plain)
    for code, pts in inline_refs.items():
        entries.append(
            Entry(
                okpd2=code,
                name="",  # will be inherited from main-table entry during merge
                section=section_name,
                thresholds=pts,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Merge thresholds across codes & finalize
# ---------------------------------------------------------------------------


def merge(entries: list[Entry], as_of: date) -> dict:
    """Merge multiple entries for the same ОКПД2 (can happen when a code
    appears in several sections). For each code we keep the strictest
    current_threshold and the union of upcoming points.
    """
    by_code: dict[str, dict] = {}
    for e in entries:
        current, upcoming = _split_schedule(e.thresholds, as_of)
        slot = by_code.setdefault(
            e.okpd2,
            {"name": e.name, "section": e.section, "current": None, "upcoming": []},
        )
        # Main-table entries carry a non-empty name (column "Наименование");
        # inline-ref entries have name="". Always prefer the main-table
        # name/section — that's the canonical section the код belongs to.
        if e.name.strip() and not slot["name"].strip():
            slot["name"] = e.name
            slot["section"] = e.section
        # Prefer the strictest current threshold (max) on collisions
        if current is not None:
            if slot["current"] is None or current["min_score"] > slot["current"]["min_score"]:
                slot["current"] = current
        for pt in upcoming:
            if pt not in slot["upcoming"]:
                slot["upcoming"].append(pt)
    # Consolidate upcoming — for each date keep only the maximum score
    # (conservative: if one source says 205 and another 245, require 245).
    for slot in by_code.values():
        by_date: dict[str, int] = {}
        for pt in slot["upcoming"]:
            prev = by_date.get(pt["from"])
            if prev is None or pt["min_score"] > prev:
                by_date[pt["from"]] = pt["min_score"]
        # Drop upcoming points that are ≤ current_threshold
        cur_score = slot["current"]["min_score"] if slot["current"] else -1
        slot["upcoming"] = [
            {"from": d, "min_score": s}
            for d, s in sorted(by_date.items())
            if s > cur_score
        ]
    return by_code


def _split_schedule(
    thresholds: list[ThresholdPoint], as_of: date
) -> tuple[Optional[dict], list[dict]]:
    if not thresholds:
        return None, []
    active: Optional[ThresholdPoint] = None
    upcoming: list[ThresholdPoint] = []
    for p in sorted(thresholds, key=lambda x: x.from_date):
        if p.from_date <= as_of:
            active = p  # keep overwriting to get the latest effective one
        else:
            upcoming.append(p)
    current = None
    if active is not None:
        current = {"min_score": active.min_score, "effective_from": active.from_date.isoformat()}
    upc = [{"from": p.from_date.isoformat(), "min_score": p.min_score} for p in upcoming]
    return current, upc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", type=Path, required=True, help="Directory with MHTML (Kontur PP 719 export)")
    parser.add_argument("--out", type=Path, required=True, help="Output JSON path")
    parser.add_argument(
        "--as-of",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date.today(),
        help="Reference date for «current» threshold selection (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    # Find the right MHTML — pick any file that references normativ.kontur.ru
    # and contains «719» in the name, else use the largest MHTML in the dir.
    candidates = list(args.src.glob("*.mhtml"))
    target: Optional[Path] = None
    for fp in candidates:
        if "719" in fp.name and "Норматив" in fp.name:
            target = fp
            break
    if target is None and candidates:
        target = max(candidates, key=lambda p: p.stat().st_size)
    if target is None:
        print(f"ERROR: no MHTML in {args.src}", file=sys.stderr)
        return 2

    print(f"[src] {target.name}")
    html, url = load_html_from_mhtml(target)
    print(f"[src] {url}")
    print(f"[src] html size: {len(html):,} chars")

    segments = extract_section_segments(html)
    print(f"[parse] sections found: {len(segments)}")
    entries: list[Entry] = []
    per_section_stats: list[tuple[str, int, int]] = []
    for sec_name, sec_html in segments:
        sec_entries = build_entries_from_section(sec_name, sec_html)
        rows_count = sec_html.count("<tr")
        per_section_stats.append((sec_name, rows_count, len(sec_entries)))
        entries.extend(sec_entries)
    print(f"[parse] raw entries (ОКПД2 rows): {len(entries):,}")
    print()
    print("Per-section breakdown:")
    for name, trs, n in per_section_stats:
        print(f"  {name[:60]:<60}  <tr>={trs:>5}  entries={n:>5}")
    print()

    merged = merge(entries, args.as_of)
    with_current = sum(1 for s in merged.values() if s["current"] is not None)
    with_upcoming = sum(1 for s in merged.values() if s["upcoming"])

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": args.as_of.isoformat(),
        "source": "ПП РФ № 719 от 17.07.2015, полный текст приложения (Контур.Норматив)",
        "source_url": url,
        "unique_okpd2_with_threshold": with_current,
        "unique_okpd2_with_upcoming": with_upcoming,
        "total_okpd2_in_appendix": len(merged),
    }

    out = {"_summary": summary, "okpd2": {k: merged[k] for k in sorted(merged)}}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print()
    print("=== ПП 719 parse summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"  written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
