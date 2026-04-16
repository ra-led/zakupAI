"""Generate PDF report using ReportLab."""
import json
from datetime import datetime
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Table, TableStyle,
    Spacer, HRFlowable, PageBreak,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# Register DejaVu Sans (supports Cyrillic)
_FONT = "DejaVuSans"
_FONT_BOLD = "DejaVuSans-Bold"
_fonts_registered = False


def _register_fonts():
    global _fonts_registered
    if _fonts_registered:
        return
    # DejaVu is installed via fonts-dejavu-core package
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    # Also check Windows paths
    win_font_paths = [
        "C:/Windows/Fonts/DejaVuSans.ttf",
        "C:/Windows/Fonts/DejaVuSans-Bold.ttf",
    ]
    if all(Path(p).exists() for p in font_paths):
        pdfmetrics.registerFont(TTFont(_FONT, font_paths[0]))
        pdfmetrics.registerFont(TTFont(_FONT_BOLD, font_paths[1]))
    elif all(Path(p).exists() for p in win_font_paths):
        pdfmetrics.registerFont(TTFont(_FONT, win_font_paths[0]))
        pdfmetrics.registerFont(TTFont(_FONT_BOLD, win_font_paths[1]))
    else:
        # Fallback: try to find anywhere
        import glob
        for f in glob.glob("/usr/share/fonts/**/DejaVuSans.ttf", recursive=True):
            pdfmetrics.registerFont(TTFont(_FONT, f))
            break
        for f in glob.glob("/usr/share/fonts/**/DejaVuSans-Bold.ttf", recursive=True):
            pdfmetrics.registerFont(TTFont(_FONT_BOLD, f))
            break
        # Try Windows font dirs
        for f in glob.glob("C:/Windows/Fonts/**/DejaVuSans.ttf", recursive=True):
            pdfmetrics.registerFont(TTFont(_FONT, f))
            break
        for f in glob.glob("C:/Windows/Fonts/**/DejaVuSans-Bold.ttf", recursive=True):
            pdfmetrics.registerFont(TTFont(_FONT_BOLD, f))
            break
    # Register font family so <b> tags in Paragraph resolve correctly
    registerFontFamily(
        _FONT,
        normal=_FONT,
        bold=_FONT_BOLD,
        italic=_FONT,
        boldItalic=_FONT_BOLD,
    )
    _fonts_registered = True


# Status colors
STATUS_COLORS = {
    "ok": colors.HexColor("#22c55e"),
    "warning": colors.HexColor("#f59e0b"),
    "error": colors.HexColor("#ef4444"),
    "mismatch": colors.HexColor("#ef4444"),
    "not_found": colors.HexColor("#94a3b8"),
    "not_actual": colors.HexColor("#f97316"),
    "insufficient": colors.HexColor("#ef4444"),
}

STATUS_LABELS = {
    "ok": "Соответствует",
    "not_actual": "Не актуально",
    "not_found": "Не найден в реестре",
    "registry_error": "Реестр недоступен",
    "mismatch": "Несоответствие",
    "warning": "Предупреждение",
    "insufficient": "Недостаточно баллов",
    "okpd_not_found": "ОКПД 2 не указан",
    "out_of_scope": "Нацрежим не применяется",
    "advisory_min_share": "Мин. доля российских (ПП 1875)",
    "score_missing": "Баллы не указаны в реестре",
    "wording": "Формулировка отличается",
    "missing_in_gisp": "Нет в ГИСП",
    "skipped": "Не проверялось",
}


def generate_report(check, items: list, output_path: str) -> str:
    """Generate PDF report for a check. Returns path to created file."""
    _register_fonts()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    story = []

    # Title
    title_style = ParagraphStyle("title", fontName=_FONT_BOLD, fontSize=16, spaceAfter=6, alignment=TA_CENTER)
    story.append(Paragraph("Отчёт о проверке товаров", title_style))
    story.append(Paragraph(
        f"по реестру 719 ПП и каталогу ГИСП",
        ParagraphStyle("subtitle", fontName=_FONT, fontSize=11, textColor=colors.gray, alignment=TA_CENTER, spaceAfter=12),
    ))
    story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
    story.append(Spacer(1, 0.3 * cm))

    # Meta table
    meta_data = [
        ["Дата проверки:", datetime.now().strftime("%d.%m.%Y %H:%M")],
        ["Всего позиций:", str(len(items))],
        ["Соответствует:", str(check.ok_count or 0)],
        ["Предупреждения:", str(check.warning_count or 0)],
        ["Несоответствия:", str(check.error_count or 0)],
        ["Не найдено:", str(check.not_found_count or 0)],
    ]
    meta_table = Table(meta_data, colWidths=[5 * cm, 12 * cm])
    meta_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.gray),
        ("FONTNAME", (0, 0), (-1, -1), _FONT),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 0.5 * cm))

    # Items
    for i, item in enumerate(items):
        story.extend(_render_item(item, i + 1, styles))

    doc.build(story)
    return output_path


def _render_item(item, position: int, styles):
    elements = []
    elements.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
    elements.append(Spacer(1, 0.2 * cm))

    overall = item.overall_status or "not_found"
    status_color = STATUS_COLORS.get(overall, colors.gray)
    status_label = STATUS_LABELS.get(overall, overall)

    # Item header
    header_style = ParagraphStyle("item_header", fontName=_FONT, fontSize=11, leading=14, spaceAfter=4)
    name = item.product_name or "—"
    reg_num = item.registry_number or "—"
    okpd2 = item.okpd2_code or "—"
    elements.append(Paragraph(
        f"<b>{position}. {name}</b>",
        header_style,
    ))

    # Info row
    info_data = [
        ["Реестровый номер:", reg_num, "Код ОКПД 2:", okpd2, "Статус:", status_label],
    ]
    info_table = Table(info_data, colWidths=[3.5 * cm, 4 * cm, 3 * cm, 3 * cm, 2 * cm, 3.5 * cm])
    info_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.gray),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.gray),
        ("TEXTCOLOR", (4, 0), (4, -1), colors.gray),
        ("TEXTCOLOR", (5, 0), (5, -1), status_color),
        ("FONTNAME", (0, 0), (-1, -1), _FONT),
        ("FONTNAME", (5, 0), (5, -1), _FONT_BOLD),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(info_table)

    # Registry check
    reg_status = item.registry_status or "not_found"
    reg_label = STATUS_LABELS.get(reg_status, reg_status)
    reg_color = STATUS_COLORS.get(reg_status, colors.gray)
    elements.append(Spacer(1, 0.15 * cm))
    elements.append(Paragraph(
        f"<b>Реестр 719 ПП:</b> <font color='#{_hex(reg_color)}'>{reg_label}</font>"
        + (f" (действует до {item.registry_cert_end_date})" if item.registry_cert_end_date else ""),
        ParagraphStyle("reg", fontName=_FONT, fontSize=9, leading=12),
    ))

    # Localization check
    if item.localization_status and item.localization_status not in ("skipped",):
        loc_status = item.localization_status
        loc_label = STATUS_LABELS.get(loc_status, loc_status)
        loc_color = STATUS_COLORS.get(loc_status, colors.gray)
        score_info = ""
        if item.localization_actual_score is not None and item.localization_required_score is not None:
            score_info = f" (факт: {item.localization_actual_score}, требуется: {item.localization_required_score})"
        elements.append(Paragraph(
            f"<b>Баллы локализации (ПП 1875):</b> <font color='#{_hex(loc_color)}'>{loc_label}</font>{score_info}",
            ParagraphStyle("loc", fontName=_FONT, fontSize=9, leading=12),
        ))

    # Characteristics comparison
    comparison = _parse_json(item.gisp_comparison)
    if comparison:
        elements.append(Spacer(1, 0.2 * cm))
        elements.append(Paragraph("<b>Сравнение характеристик (ГИСП):</b>", ParagraphStyle("chars_header", fontName=_FONT, fontSize=9)))
        elements.append(Spacer(1, 0.1 * cm))

        table_data = [["Характеристика", "Поставщик", "ГИСП", "Статус"]]
        for row in comparison:
            s = row.get("status", "ok")
            s_label = STATUS_LABELS.get(s, s)
            s_color = STATUS_COLORS.get(s, colors.green)
            table_data.append([
                str(row.get("name", ""))[:50],
                str(row.get("supplier_value", ""))[:40],
                str(row.get("gisp_value", "") or "—")[:40],
                s_label,
            ])

        comp_table = Table(
            table_data,
            colWidths=[5.5 * cm, 4.5 * cm, 4.5 * cm, 4.5 * cm],
            repeatRows=1,
        )
        comp_style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 0), (-1, -1), _FONT),
            ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.lightgrey),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("WORDWRAP", (0, 0), (-1, -1), True),
        ]
        # Color status cells
        for row_idx, row in enumerate(comparison, start=1):
            s = row.get("status", "ok")
            c = STATUS_COLORS.get(s, colors.green)
            if s != "ok":
                comp_style.append(("BACKGROUND", (3, row_idx), (3, row_idx), colors.HexColor("#fff7ed") if s == "wording" else colors.HexColor("#fef2f2")))
                comp_style.append(("TEXTCOLOR", (3, row_idx), (3, row_idx), c))

        comp_table.setStyle(TableStyle(comp_style))
        elements.append(comp_table)

    elements.append(Spacer(1, 0.3 * cm))
    return elements


def _parse_json(val) -> list:
    if not val:
        return []
    if isinstance(val, list):
        return val
    try:
        return json.loads(val)
    except Exception:
        return []


def _hex(color) -> str:
    try:
        if hasattr(color, "hexval"):
            h = color.hexval()  # returns "0x94a3b8"
            return h[2:] if h.startswith("0x") else h.lstrip("#")
        return "000000"
    except Exception:
        return "000000"
