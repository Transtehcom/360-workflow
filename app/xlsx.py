"""Минимальный писатель .xlsx на стандартной библиотеке: листы, ячейки, фиксированный набор стилей.

Строки пишутся как inlineStr (без sharedStrings), формулы — без кешированного значения:
Excel пересчитывает их при открытии (fullCalcOnLoad)."""
import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

# Запрещено в XML 1.0: управляющие символы (кроме \t \n \r), ￾, ￿ и одиночные суррогаты.
_BAD_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")

# Стили: имя -> номер в cellXfs (0 — стиль по умолчанию, в листах не используется).
STYLE_IDS = {"title": 1, "header": 2, "header_left": 3, "comp": 4, "comp_num": 5, "text": 6, "num": 7}

_FONTS = (
    '<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'                                 # 0 по умолчанию
    '<font><b/><sz val="12"/><name val="Calibri"/><family val="2"/></font>'                             # 1 жирный 12
    '<font><b/><sz val="12"/><color rgb="FF44546A"/><name val="Calibri"/><family val="2"/></font>'      # 2 жирный 12, #44546A
    '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font>'                             # 3 жирный 11
    '<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'                                 # 4 обычный 11
)
_FILLS = (
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFE4EEDB"/><bgColor indexed="64"/></patternFill></fill>'
)
_THIN = '<{0} style="thin"><color indexed="64"/></{0}>'
_BORDERS = (
    '<border><left/><right/><top/><bottom/><diagonal/></border>'
    '<border>' + "".join(_THIN.format(side) for side in ("left", "right", "top", "bottom")) + '<diagonal/></border>'
)


def _xf(num_fmt, font, fill, border, horizontal=None, vertical=None, wrap=False):
    align = "".join((
        f' horizontal="{horizontal}"' if horizontal else "",
        f' vertical="{vertical}"' if vertical else "",
        ' wrapText="1"' if wrap else "",
    ))
    return (f'<xf numFmtId="{num_fmt}" fontId="{font}" fillId="{fill}" borderId="{border}" xfId="0" '
            f'applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
            f'<alignment{align}/></xf>')


_CELL_XFS = (
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'    # 0 по умолчанию
    + _xf(0, 1, 0, 0)                                                   # 1 title
    + _xf(0, 2, 0, 1, "center", "center", True)                         # 2 header
    + _xf(0, 2, 0, 1, "left", "center", True)                           # 3 header_left
    + _xf(0, 3, 2, 1, "left", "top", True)                              # 4 comp
    + _xf(164, 3, 2, 1, "right", "top", True)                           # 5 comp_num
    + _xf(0, 4, 0, 1, "left", "top", True)                              # 6 text
    + _xf(164, 4, 0, 1, "right", "top")                                 # 7 num
)

STYLES_XML = (
    XML_HEAD + f'<styleSheet xmlns="{MAIN_NS}">'
    '<numFmts count="1"><numFmt numFmtId="164" formatCode="0.0"/></numFmts>'
    f'<fonts count="5">{_FONTS}</fonts>'
    f'<fills count="3">{_FILLS}</fills>'
    f'<borders count="2">{_BORDERS}</borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    f'<cellXfs count="{len(STYLE_IDS) + 1}">{_CELL_XFS}</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


@dataclass
class Cell:
    """Ячейка: текст (str), число или формула без «=» (например, "AVERAGE(D5:F5)"); style — ключ STYLE_IDS."""
    value: str | float | int | None = None
    style: str = "text"
    formula: str | None = None


@dataclass
class Sheet:
    """Лист: cells — {(строка, колонка): Cell}, строки и колонки с 1; ширины и высоты — в единицах Excel."""
    name: str
    cells: dict[tuple[int, int], Cell]
    col_widths: dict[int, float] = field(default_factory=dict)
    row_heights: dict[int, float] = field(default_factory=dict)


def col_letter(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    letters = ""
    while n > 0:
        n, rest = divmod(n - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _text(value) -> str:
    """Строка для XML: без запрещённых символов и с экранированием."""
    return escape(_BAD_XML_CHARS.sub("", str(value)))


def _num(value) -> str:
    """Число без лишнего «.0» (28.0 -> «28», 47.25 -> «47.25»)."""
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("В ячейку нельзя записать NaN или бесконечность.")
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def _cell_xml(row: int, col: int, cell: Cell) -> str:
    if cell.style not in STYLE_IDS:
        raise ValueError(f"Неизвестный стиль ячейки: {cell.style}")
    head = f'<c r="{col_letter(col)}{row}" s="{STYLE_IDS[cell.style]}"'
    if cell.formula is not None:
        return f'{head}><f>{_text(cell.formula)}</f></c>'
    if cell.value is None:
        return f'{head}/>'
    if isinstance(cell.value, str):
        return f'{head} t="inlineStr"><is><t xml:space="preserve">{_text(cell.value)}</t></is></c>'
    return f'{head}><v>{_num(cell.value)}</v></c>'


def _sheet_xml(sheet: Sheet) -> str:
    rows: dict[int, dict[int, Cell]] = {}
    for (row, col), cell in sheet.cells.items():
        rows.setdefault(row, {})[col] = cell
    for row in sheet.row_heights:
        rows.setdefault(row, {})
    parts = [XML_HEAD, f'<worksheet xmlns="{MAIN_NS}" xmlns:r="{REL_NS}">',
             '<sheetFormatPr defaultRowHeight="15"/>']
    if sheet.col_widths:
        parts.append("<cols>" + "".join(
            f'<col min="{col}" max="{col}" width="{_num(width)}" customWidth="1"/>'
            for col, width in sorted(sheet.col_widths.items())) + "</cols>")
    parts.append("<sheetData>")
    for row in sorted(rows):
        height = sheet.row_heights.get(row)
        attrs = f' ht="{_num(height)}" customHeight="1"' if height is not None else ""
        parts.append(f'<row r="{row}"{attrs}>'
                     + "".join(_cell_xml(row, col, cell) for col, cell in sorted(rows[row].items())) + "</row>")
    parts.append("</sheetData></worksheet>")
    return "".join(parts)


def write_workbook(sheets: list[Sheet]) -> bytes:
    """Собирает книгу .xlsx; листы идут в переданном порядке."""
    if not sheets:
        raise ValueError("В книге должен быть хотя бы один лист.")
    n = len(sheets)
    content_types = (
        XML_HEAD + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                  for i in range(1, n + 1))
        + '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
    root_rels = (
        XML_HEAD + f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    workbook = (
        XML_HEAD + f'<workbook xmlns="{MAIN_NS}" xmlns:r="{REL_NS}"><sheets>'
        + "".join(f'<sheet name="{_text(s.name).replace(chr(34), "&quot;")}" sheetId="{i}" r:id="rId{i}"/>'
                  for i, s in enumerate(sheets, 1))
        + '</sheets><calcPr fullCalcOnLoad="1"/></workbook>')
    workbook_rels = (
        XML_HEAD + f'<Relationships xmlns="{PKG_REL_NS}">'
        + "".join(f'<Relationship Id="rId{i}" Type="{REL_NS}/worksheet" Target="worksheets/sheet{i}.xml"/>'
                  for i in range(1, n + 1))
        + f'<Relationship Id="rId{n + 1}" Type="{REL_NS}/styles" Target="styles.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        z.writestr("xl/styles.xml", STYLES_XML)
        for i, sheet in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(sheet))
    return buf.getvalue()
