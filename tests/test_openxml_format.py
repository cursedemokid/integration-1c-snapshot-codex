import io
import unittest
import zipfile
from xml.etree import ElementTree as ET
from app.reporting import build_excel_report


class OpenXmlFormatTests(unittest.TestCase):
    def test_report_uses_standard_namespaces_and_relationships(self):
        with zipfile.ZipFile(io.BytesIO(build_excel_report([]))) as book:
            ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            workbook = ET.fromstring(book.read("xl/workbook.xml"))
            self.assertEqual(workbook.tag, "{" + ns + "}workbook")
            self.assertIsNotNone(workbook.find("{" + ns + "}sheets/{" + ns + "}sheet"))
            rels = ET.fromstring(book.read("_rels/.rels"))
            self.assertTrue(any(r.attrib["Type"] ==
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
                for r in rels))
