import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import digest  # noqa: E402


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.rules = digest.load_rules()
        self.by_subject = {}
        for m in digest.sample_mails():
            digest.classify(m, self.rules)
            self.by_subject[m.subject] = m.priority

    def test_high(self):
        self.assertEqual(self.by_subject["Payment failed for your credit card bill"], "high")
        self.assertEqual(self.by_subject["Contract review needed by tomorrow"], "high")
        self.assertEqual(self.by_subject["Security alert: new sign-in"], "high")

    def test_medium(self):
        self.assertEqual(self.by_subject["Dinner Saturday?"], "medium")
        self.assertEqual(self.by_subject["Invoice INV-204 is due"], "medium")

    def test_low(self):
        self.assertEqual(self.by_subject["Big sale: 70% off today only"], "low")
        self.assertEqual(self.by_subject["You appeared in 9 searches"], "low")
        self.assertEqual(self.by_subject["Top stories for you"], "low")

    def test_chunking(self):
        text = "\n".join(f"line {i} " + "x" * 80 for i in range(40))
        parts = digest.chunk(text, 500)
        self.assertTrue(all(len(p) <= 500 for p in parts))
        self.assertEqual("\n".join(parts), text)


class RenderTest(unittest.TestCase):
    def test_html_escapes_and_groups(self):
        rules = digest.load_rules()
        mails = digest.sample_mails()
        dup = digest.sample_mails()[1]
        mails.append(dup)
        mails.append(digest.Mail("work", "A<b>", "a@x.example", "Fix <script> & stuff ASAP", None))
        for m in mails:
            digest.classify(m, rules)
        out = digest.build_digest(mails, [], html=True)
        self.assertIn("&lt;script&gt; &amp; stuff", out)
        self.assertNotIn("<script>", out)
        self.assertIn("x2", out)


if __name__ == "__main__":
    unittest.main()
