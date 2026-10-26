"""Тестовый помощник: читает .xlsx, собранный app/xlsx.py (и совместимые файлы), только stdlib."""
import io
import posixpath
import zipfile
import xml.etree.ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
DOC_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def read_xml(data: bytes, member: str) -> ET.Element:
    """Корневой элемент XML-части архива, например «xl/styles.xml»."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return ET.fromstring(z.read(member))


def read_cells(data: bytes) -> dict[str, dict[str, str]]:
    """Имя листа -> адрес ячейки -> текст ("=" + формула для формульных, число — как <v>)."""
    wb = read_xml(data, "xl/workbook.xml")
    rels = read_xml(data, "xl/_rels/workbook.xml.rels")
    targets = {r.get("Id"): r.get("Target") for r in rels.iter(REL_NS + "Relationship")}
    result = {}
    for sheet in wb.iter(NS + "sheet"):
        member = posixpath.join("xl", targets[sheet.get(DOC_REL)])
        cells = {}
        for c in read_xml(data, member).iter(NS + "c"):
            f = c.find(NS + "f")
            v = c.find(NS + "v")
            if f is not None:
                cells[c.get("r")] = "=" + (f.text or "")
            elif c.get("t") == "inlineStr":
                cells[c.get("r")] = "".join(t.text or "" for t in c.iter(NS + "t"))
            elif v is not None:
                cells[c.get("r")] = v.text or ""
        result[sheet.get("name")] = cells
    return result
