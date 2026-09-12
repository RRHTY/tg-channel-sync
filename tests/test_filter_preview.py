import unittest
from services.filter_rules import validate_filter_rule, preview_filter_rule


class FilterPreviewTests(unittest.TestCase):
    def test_invalid_drafts_are_rejected(self):
        for args in [('replace', '[', '', 0), ('replace', '(a)', r'\g<missing>', 0),
                     ('unknown', 'a', '', 0), ('drop', '', '', 0)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_filter_rule(*args)

    def test_python_replacement_and_filename_drop(self):
        result = preview_filter_rule('replace', r'(?P<word>hello)', r'[\g<word>]', 0, 'HELLO world', '')
        self.assertFalse(result['dropped'])
        self.assertTrue(result['matched'])
        self.assertEqual(result['text'], '[HELLO] world')
        result = preview_filter_rule('drop', r'\.exe$', '', 0, 'hello', 'file.EXE')
        self.assertTrue(result['dropped'])

    def test_pathological_pattern_times_out(self):
        with self.assertRaisesRegex(ValueError, '超时'):
            preview_filter_rule('drop', '(a+)+$', '', 0, 'a' * 200 + '!', '', timeout=2)

    def test_empty_input_matching_is_not_part_of_validation(self):
        # Both the save and preview entrypoints call this validator.
        pattern = '(?:(?:a?){100000}){100000}'
        validate_filter_rule('replace', pattern, 'x', 0)
        with self.assertRaisesRegex(ValueError, '超时'):
            preview_filter_rule('replace', pattern, 'x', 0, '', '', timeout=2)
