import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.xlsx import Cell, Sheet, col_letter, write_workbook  # noqa: E402
from xlsx_read import NS, read_cells, read_xml  # noqa: E402


def one_sheet(cells, **kw):
    return write_workbook([Sheet("Лист", cells, **kw)])


class XlsxTest(unittest.TestCase):
    def test_package_is_well_formed(self):
        data = write_workbook([
            Sheet("Первый лист", {(1, 1): Cell("а")}),
            Sheet("Второй & <лист>", {(1, 1): Cell(1)}),
        ])
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        for member in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
                       "xl/styles.xml", "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
            self.assertIn(member, names)
            read_xml(data, member)  # парсится
        wb = read_xml(data, "xl/workbook.xml")
        self.assertEqual([s.get("name") for s in wb.iter(NS + "sheet")], ["Первый лист", "Второй & <лист>"])
        self.assertEqual(wb.find(NS + "calcPr").get("fullCalcOnLoad"), "1")

    def test_cell_types(self):
        root = read_xml(one_sheet({(1, 1): Cell("текст"), (1, 2): Cell(3),
                                   (1, 3): Cell(formula="AVERAGE(D5:F5)", style="num")}),
                        "xl/worksheets/sheet1.xml")
        a, b, c = {x.get("r"): x for x in root.iter(NS + "c")}.values()
        self.assertEqual(a.get("t"), "inlineStr")
        self.assertEqual(a.find(NS + "is").find(NS + "t").text, "текст")
        self.assertEqual(b.find(NS + "v").text, "3")
        self.assertIsNone(b.get("t"))
        self.assertEqual(c.find(NS + "f").text, "AVERAGE(D5:F5)")
        self.assertIsNone(c.get("t"))
        self.assertIsNone(c.find(NS + "v"))

    def test_read_cells_roundtrip(self):
        data = one_sheet({(1, 1): Cell("а"), (2, 1): Cell(2.5), (2, 2): Cell(formula="SUM(A1:A2)")})
        self.assertEqual(read_cells(data), {"Лист": {"A1": "а", "A2": "2.5", "B2": "=SUM(A1:A2)"}})

    def test_text_is_never_formula(self):
        root = read_xml(one_sheet({(1, 1): Cell("=1+1")}), "xl/worksheets/sheet1.xml")
        c = next(root.iter(NS + "c"))
        self.assertEqual(c.get("t"), "inlineStr")
        self.assertIsNone(c.find(NS + "f"))
        self.assertEqual(read_cells(one_sheet({(1, 1): Cell("=1+1")}))["Лист"]["A1"], "=1+1")  # как текст

    def test_control_chars_and_emoji(self):
        data = one_sheet({(1, 1): Cell("а\x0bб\x00в 😀\nг￾￿")})
        self.assertEqual(read_cells(data)["Лист"]["A1"], "абв 😀\nг")

    def test_col_letter(self):
        self.assertEqual([col_letter(n) for n in (1, 26, 27, 52, 53, 702, 703)],
                         ["A", "Z", "AA", "AZ", "BA", "ZZ", "AAA"])

    def test_widths_and_heights(self):
        data = one_sheet({(4, 2): Cell("x")}, col_widths={2: 28, 1: 10.5}, row_heights={4: 47.25})
        root = read_xml(data, "xl/worksheets/sheet1.xml")
        cols = {c.get("min"): c for c in root.find(NS + "cols")}
        self.assertEqual(float(cols["2"].get("width")), 28)
        self.assertEqual(cols["2"].get("max"), "2")
        self.assertEqual(cols["2"].get("customWidth"), "1")
        self.assertEqual(float(cols["1"].get("width")), 10.5)
        row = next(root.iter(NS + "row"))
        self.assertEqual(row.get("r"), "4")
        self.assertEqual(row.get("ht"), "47.25")
        self.assertEqual(row.get("customHeight"), "1")

    def test_order_ascending_and_height_only_row(self):
        data = one_sheet({(3, 2): Cell("b"), (3, 1): Cell("a"), (1, 5): Cell("e")}, row_heights={2: 20})
        root = read_xml(data, "xl/worksheets/sheet1.xml")
        self.assertEqual([r.get("r") for r in root.iter(NS + "row")], ["1", "2", "3"])
        self.assertEqual([c.get("r") for c in root.iter(NS + "c")], ["E1", "A3", "B3"])

    def test_style_ids_resolve(self):
        styles = ("title", "header", "header_left", "comp", "comp_num", "text", "num")
        cells = {(1, i + 1): Cell(i, style=s) for i, s in enumerate(styles)}
        data = one_sheet(cells)
        st = read_xml(data, "xl/styles.xml")
        count = len(st.find(NS + "cellXfs"))
        self.assertEqual(st.find(NS + "cellXfs").get("count"), str(count))
        fills = st.find(NS + "fills")
        self.assertEqual(fills[0][0].get("patternType"), "none")
        self.assertEqual(fills[1][0].get("patternType"), "gray125")
        used = [int(c.get("s")) for c in read_xml(data, "xl/worksheets/sheet1.xml").iter(NS + "c")]
        self.assertEqual(len(used), len(styles))
        self.assertTrue(all(0 <= n < count for n in used))
        self.assertEqual(len(set(used)), len(styles))  # у каждого стиля свой xf

    def test_unknown_style_and_empty_workbook(self):
        with self.assertRaises(ValueError):
            one_sheet({(1, 1): Cell("x", style="нет")})
        with self.assertRaises(ValueError):
            write_workbook([])


if __name__ == "__main__":
    unittest.main()
