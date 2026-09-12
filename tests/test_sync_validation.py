import unittest
from services.sync_validation import validate_sync_request, load_json_export


class SyncValidationTests(unittest.TestCase):
    def test_reject_invalid_parameters(self):
        defaults = dict(mode='api', sender='bot', target_type='channel', source_id='source',
                        target_id='target', delay=5, start_id=0, end_id=0, window=3)
        for changes in ({'mode': 'oops'}, {'sender': 'oops'}, {'target_type': 'oops'},
                        {'source_id': ' '}, {'target_id': ''}, {'delay': float('nan')},
                        {'delay': -1}, {'start_id': -1}, {'start_id': 10, 'end_id': 3}, {'window': 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_sync_request(**(defaults | changes))

    def test_valid_open_ended_and_saved(self):
        validate_sync_request('api', 'bot', 'saved', 'source', '', .5, 10, 0, 3)

    def test_missing_export_is_actionable(self):
        with self.assertRaisesRegex(ValueError, 'JSON 文件'):
            load_json_export('temp/does-not-exist.json')
