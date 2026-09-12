import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scotus_monitor as monitor


HTML = '''
<html><body>
  <a href="/opinions/26pdf/25-123_abcd.pdf">25-123 Example v. Sample</a>
  <a href="/opinions/26pdf/25-456_efgh.pdf"><span>25-456</span> Second Case</a>
  <a href="/about/biographies.pdf">Biographies</a>
</body></html>
'''


class MonitorTests(unittest.TestCase):
    def test_parses_and_filters_pdf_links(self):
        items = monitor.parse_items(HTML, "https://www.supremecourt.gov/opinions/slipopinion/26", "Opinions")
        self.assertEqual(2, len(items))
        self.assertEqual("25-123 Example v. Sample", items[0].title)
        self.assertEqual("https://www.supremecourt.gov/opinions/26pdf/25-123_abcd.pdf", items[0].url)

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            monitor.save_state(path, {"b", "a"})
            self.assertEqual(["a", "b"], monitor.load_state(path)["seen"])

    def test_term_changes_in_october(self):
        class September:
            @classmethod
            def now(cls):
                return type("D", (), {"year": 2026, "month": 9})()
        with patch.object(monitor, "datetime", September):
            self.assertEqual("25", monitor.term_year())


if __name__ == "__main__":
    unittest.main()
