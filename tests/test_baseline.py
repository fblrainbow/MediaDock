"""Stage-001 progress-parsing baseline (stdlib only, no new deps)."""
import unittest

import server as srv


class TestProgressParsing(unittest.TestCase):
    def test_normal_progress_line(self):
        line = "[download]  8.8% of ~ 50.00MiB at 22.68KiB/s ETA 47:53"
        m = srv.PROGRESS_RE.search(line)
        self.assertIsNotNone(m)
        self.assertAlmostEqual(float(m.group(1)), 8.8)
        self.assertIn("KiB/s", m.group(2))
        self.assertEqual(m.group(3).strip(), "47:53")

    def test_hls_style_progress_lines(self):
        for pct in ("99.9", "45.2"):
            line = f"[download] {pct}% of ~ 10.00MiB at 1.23MiB/s ETA 00:04"
            m = srv.PROGRESS_RE.search(line)
            self.assertIsNotNone(m, line)
            self.assertAlmostEqual(float(m.group(1)), float(pct))

    def test_merger_line_detected(self):
        line = '[Merger] Merging formats into "out.mp4"'
        self.assertTrue(srv.MERGE_RE.search(line) is not None)

    def test_destination_line_ignored(self):
        line = "[download] Destination: downloads\\x.mp4"
        self.assertIsNone(srv.PROGRESS_RE.search(line))


if __name__ == "__main__":
    unittest.main()

