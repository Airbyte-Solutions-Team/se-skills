from html import escape
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, Table

DEFAULT_TEXT_COLOR = colors.HexColor("#222222")
DEFAULT_BREAK_LONG_WORDS_AFTER = 24
BREAKABLE_CHARACTERS = "/_:-.→"
SOFT_HYPHEN = "&shy;"


def create_wrapped_table(
    data: list[list[Any]],
    col_widths: Optional[list[float]] = None,
    repeat_rows: int = 1,
    font_size: int = 9,
    header_font_size: Optional[int] = None,
    header_text_color: colors.Color = colors.white,
    bold_rows: Optional[set[int]] = None,
    text_color_overrides: Optional[dict[tuple[int, int], colors.Color]] = None,
    break_long_words_after: int = DEFAULT_BREAK_LONG_WORDS_AFTER,
) -> Table:
    header_size = header_font_size or font_size
    bold_row_indexes = bold_rows or set()
    cell_text_colors = text_color_overrides or {}

    rows = []
    for row_index, row in enumerate(data):
        row_is_header = row_index < repeat_rows
        row_is_bold = row_is_header or row_index in bold_row_indexes or row_index - len(data) in bold_row_indexes
        row_font_size = header_size if row_is_header else font_size
        row_text_color = header_text_color if row_is_header else DEFAULT_TEXT_COLOR
        rows.append([
            _wrap_cell(
                cell,
                create_table_cell_style(
                    font_size=row_font_size,
                    bold=row_is_bold,
                    text_color=cell_text_colors.get((row_index, col_index), row_text_color),
                ),
                break_long_words_after=break_long_words_after,
            )
            for col_index, cell in enumerate(row)
        ])

    return Table(rows, colWidths=col_widths, repeatRows=repeat_rows)


def create_table_cell(
    value: Any,
    font_size: int = 9,
    bold: bool = False,
    text_color: colors.Color = DEFAULT_TEXT_COLOR,
    break_long_words_after: int = DEFAULT_BREAK_LONG_WORDS_AFTER,
) -> Paragraph:
    return Paragraph(
        _prepare_cell_text(value, break_long_words_after=break_long_words_after),
        create_table_cell_style(font_size, bold, text_color),
    )


def create_table_cell_style(
    font_size: int,
    bold: bool = False,
    text_color: colors.Color = DEFAULT_TEXT_COLOR,
    alignment: int = TA_LEFT,
) -> ParagraphStyle:
    styles = getSampleStyleSheet()
    return ParagraphStyle(
        "WrappedTableCell",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=font_size,
        leading=font_size + 2,
        textColor=text_color,
        alignment=alignment,
        spaceAfter=0,
    )


def _wrap_cell(
    cell: Any,
    style: ParagraphStyle,
    break_long_words_after: int = DEFAULT_BREAK_LONG_WORDS_AFTER,
) -> Any:
    if hasattr(cell, "wrap") and hasattr(cell, "drawOn"):
        return cell
    return Paragraph(_prepare_cell_text(cell, break_long_words_after=break_long_words_after), style)


def _prepare_cell_text(
    value: Any,
    break_long_words_after: int = DEFAULT_BREAK_LONG_WORDS_AFTER,
) -> str:
    text = escape("" if value is None else str(value))
    if break_long_words_after <= 0:
        return text

    return " ".join(_break_long_word(word, break_long_words_after) for word in text.split(" "))


def _break_long_word(word: str, break_long_words_after: int) -> str:
    if len(word) <= break_long_words_after:
        return word

    output = []
    chars_since_break = 0
    for char in word:
        output.append(char)
        chars_since_break += 1
        if char in BREAKABLE_CHARACTERS or chars_since_break >= break_long_words_after:
            output.append(SOFT_HYPHEN)
            chars_since_break = 0
    return "".join(output)
