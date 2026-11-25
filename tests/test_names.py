import unittest

from lib.names import normalize_user_name, UserNameError


class UserNameTests(unittest.TestCase):
    def test_trims_and_preserves_normal_name_punctuation(self):
        self.assertEqual(normalize_user_name('  Haoda  '), 'Haoda')
        self.assertEqual(normalize_user_name('A/B\'"'), 'A/B\'"')

    def test_counts_unicode_characters_instead_of_bytes_or_utf16_units(self):
        for name in ('abcdefghij', '一二三四五六七八九十', '🎵' * 10):
            self.assertEqual(normalize_user_name(name), name)
            with self.assertRaises(UserNameError):
                normalize_user_name(name + '甲')

    def test_normalizes_combining_accents_before_counting(self):
        self.assertEqual(normalize_user_name('e\u0301' * 10), 'é' * 10)

    def test_rejects_missing_empty_control_characters_and_nonstrings(self):
        for value in (None, 123, [], {}, '', '  ', '\n\t', 'A\nB', 'A\x00B', 'A\x85B'):
            with self.subTest(name=value), self.assertRaises(UserNameError):
                normalize_user_name(value)


if __name__ == '__main__':
    unittest.main()
