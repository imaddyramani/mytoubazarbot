import unittest

from flight_print import _normalized_baggage_entries
from flight_extractor import _apply_verified_endpoint_rows


class AirVisualBaggageTests(unittest.TestCase):
    def test_weight_and_piece_are_not_additive(self):
        rows = _normalized_baggage_entries(
            'Check-in: 15 kg 1 piece; Cabin: 7 kg 1 pc', {'type': 'Adult'})
        self.assertEqual([row[2] for row in rows],
                         ['15 kg (1 piece)', '7 kg (1 piece)'])

    def test_no_piece_count_is_invented(self):
        rows = _normalized_baggage_entries('Checked in baggage Adult 15kgs', {'type': 'Adult'})
        self.assertEqual(rows[0][2], '15 kg')

    def test_plural_and_duplicate_allowances(self):
        rows = _normalized_baggage_entries('Check-in 23kg 23kg 2 pieces', {'type': 'Adult'})
        self.assertEqual(rows[0][2], '23 kg (2 pieces)')

    def test_same_cell_terminal_and_full_airport_are_preserved(self):
        data = {'segments': [{'flight_number': '6E 2090', 'dep_code': 'NMI', 'arr_code': 'DEL'}]}
        row = {'flight_number': '6E 2090', 'dep_code': 'NMI', 'arr_code': 'DEL',
               'dep_endpoint_text': 'Navi mumbai international airport, navi mumbai',
               'dep_terminal': '', 'dep_terminal_evidence': '',
               'arr_endpoint_text': 'Delhi indira gandhi international, delhi',
               'arr_terminal': 'Terminal 2', 'arr_terminal_evidence': 'Terminal 2'}
        result = _apply_verified_endpoint_rows(data, {'segments': [row]})['segments'][0]
        self.assertEqual(result['dep_terminal'], '')
        self.assertEqual(result['arr_terminal'], 'Terminal 2')
        self.assertIn('Delhi indira gandhi international, delhi', result['arr_airport'])

    def test_different_flight_cannot_supply_terminal(self):
        data = {'segments': [{'flight_number': '6E 2090', 'dep_code': 'NMI', 'arr_code': 'DEL'}]}
        row = {'flight_number': 'AI 101', 'dep_code': 'NMI', 'arr_code': 'DEL',
               'arr_endpoint_text': 'Delhi Terminal 2'}
        result = _apply_verified_endpoint_rows(data, {'segments': [row]})['segments'][0]
        self.assertNotIn('arr_terminal', result)


if __name__ == '__main__':
    unittest.main()
