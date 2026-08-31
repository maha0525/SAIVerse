import unittest

from tools.fuzzy import find_closest, resolve_fuzzy


class TestFindClosest(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(
            find_closest("foo", ["foo", "bar", "baz"]),
            "foo",
        )

    def test_close_match(self):
        result = find_closest(
            "saiverse-elyth-addon__elyth",
            ["saiverse-elyth-addon", "saiverse-voice-tts"],
        )
        self.assertEqual(result, "saiverse-elyth-addon")

    def test_no_match_below_threshold(self):
        self.assertIsNone(
            find_closest("xyz", ["aaaaaa", "bbbbbb"], threshold=0.9)
        )

    def test_empty_value(self):
        self.assertIsNone(find_closest("", ["foo"]))

    def test_empty_candidates(self):
        self.assertIsNone(find_closest("foo", []))

    def test_filters_empty_candidates(self):
        self.assertEqual(
            find_closest("foo", ["", "foo", ""]),
            "foo",
        )


class TestResolveFuzzy(unittest.TestCase):
    def test_exact_match(self):
        resolved, was_exact, original = resolve_fuzzy(
            "foo", ["foo", "bar"]
        )
        self.assertEqual(resolved, "foo")
        self.assertTrue(was_exact)
        self.assertIsNone(original)

    def test_fuzzy_match_returns_original(self):
        resolved, was_exact, original = resolve_fuzzy(
            "saiverse-elyth-addon__elyth",
            ["saiverse-elyth-addon", "saiverse-voice-tts"],
        )
        self.assertEqual(resolved, "saiverse-elyth-addon")
        self.assertFalse(was_exact)
        self.assertEqual(original, "saiverse-elyth-addon__elyth")

    def test_no_match_returns_input(self):
        resolved, was_exact, original = resolve_fuzzy(
            "xyz", ["aaaaaa", "bbbbbb"], threshold=0.9
        )
        self.assertEqual(resolved, "xyz")
        self.assertFalse(was_exact)
        self.assertIsNone(original)

    def test_empty_value(self):
        resolved, was_exact, original = resolve_fuzzy("", ["foo"])
        self.assertEqual(resolved, "")
        self.assertFalse(was_exact)
        self.assertIsNone(original)

    def test_threshold_low_accepts_more(self):
        # threshold を下げると遠い候補もマッチする
        resolved, was_exact, original = resolve_fuzzy(
            "abcdef", ["abcxyz", "ghijkl"], threshold=0.3
        )
        self.assertEqual(resolved, "abcxyz")
        self.assertFalse(was_exact)
        self.assertEqual(original, "abcdef")


if __name__ == "__main__":
    unittest.main()
