import unittest
from gateway.edit_ops import sanitize_ops


class TestSanitizeOps(unittest.TestCase):
    def test_accepts_well_formed_delete(self):
        ops = sanitize_ops([{'type': 'delete', 'clipId': 'a'}], {'a', 'b'})
        self.assertEqual(ops, [{'type': 'delete', 'clipId': 'a'}])

    def test_rejects_unknown_clip_id(self):
        ops = sanitize_ops([{'type': 'delete', 'clipId': 'nonexistent'}], {'a', 'b'})
        self.assertEqual(ops, [])

    def test_rejects_unknown_op_type(self):
        ops = sanitize_ops([{'type': 'reformat_entire_disk', 'clipId': 'a'}], {'a'})
        self.assertEqual(ops, [])

    def test_rejects_non_list_input(self):
        self.assertEqual(sanitize_ops('not a list', {'a'}), [])
        self.assertEqual(sanitize_ops({'type': 'delete'}, {'a'}), [])
        self.assertEqual(sanitize_ops(None, {'a'}), [])

    def test_rejects_non_dict_items(self):
        ops = sanitize_ops(['just a string', 42, None, {'type': 'delete', 'clipId': 'a'}], {'a'})
        self.assertEqual(ops, [{'type': 'delete', 'clipId': 'a'}])

    def test_clamps_out_of_range_speed(self):
        ops = sanitize_ops([{'type': 'speed', 'clipId': 'a', 'value': 999}], {'a'})
        self.assertEqual(ops[0]['value'], 4.0)
        ops = sanitize_ops([{'type': 'speed', 'clipId': 'a', 'value': -50}], {'a'})
        self.assertEqual(ops[0]['value'], 0.25)

    def test_clamps_out_of_range_volume(self):
        ops = sanitize_ops([{'type': 'volume', 'clipId': 'a', 'value': 500}], {'a'})
        self.assertEqual(ops[0]['value'], 2.0)

    def test_rejects_trim_with_end_before_start(self):
        ops = sanitize_ops([{'type': 'trim', 'clipId': 'a', 'trimStart': 5, 'trimEnd': 2}], {'a'})
        self.assertEqual(ops, [])

    def test_rejects_non_numeric_values(self):
        ops = sanitize_ops([{'type': 'speed', 'clipId': 'a', 'value': 'fast'}], {'a'})
        self.assertEqual(ops, [])
        ops = sanitize_ops([{'type': 'trim', 'clipId': 'a', 'trimStart': 'oops', 'trimEnd': 5}], {'a'})
        self.assertEqual(ops, [])

    def test_caps_total_ops_at_twenty(self):
        many = [{'type': 'delete', 'clipId': 'a'} for _ in range(200)]
        ops = sanitize_ops(many, {'a'})
        self.assertLessEqual(len(ops), 20)

    def test_missing_clip_id_field_rejected(self):
        ops = sanitize_ops([{'type': 'delete'}], {'a'})
        self.assertEqual(ops, [])

    def test_extra_unexpected_fields_are_dropped_not_passed_through(self):
        ops = sanitize_ops([{'type': 'delete', 'clipId': 'a', 'malicious': 'DROP TABLE users'}], {'a'})
        self.assertEqual(ops, [{'type': 'delete', 'clipId': 'a'}])
        self.assertNotIn('malicious', ops[0])


if __name__ == '__main__':
    unittest.main()
