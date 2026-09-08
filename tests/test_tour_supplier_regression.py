import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from extractor import _local_tour_result, _source_days, extract_itinerary_from_parts
from performance_utils import collect_complete_supplier_text, prepare_supplier_for_ai


SUPPLIER_TEXT = """6 Nights and 7 Days Shree Shivay Travels //03Oct//04Adult
Tour Starting Date: 03/Oct/2026                               Tour Ending Date: 08/Oct/2026
Destination             Hotels          Room Category      Nights        Meal Plan        No of Rooms
Munnar    Munnar Summer castle  Balcony Room Non AC       2          BREAKFAST               2
Thekkady      Seasons Thekakdy              Classic A/c           1          BREAKFAST               2
Alleppey       Pagoda Resorts         A/c Deluxe Room        1          BREAKFAST               2
Trivandrum  Pattom Royal Residency     Royal Standard AC        1          BREAKFAST               2
Transportation                                         Ertiga A/c
Total Package Cost (Super Deluxe)      INR 71,000/- PER COUPLE AS OF NOW WITH FLIGHT TKT
DAY 1 : Munnar 03 OCT 2026 Arrival in Kerala and proceed to Munnar.
DAY 2 : Munnar 04 OCT 2026 Visit Mattupetty Dam and Echo Point after breakfast.
DAY 3 : Thekkady 05 OCT 2026 Drive to Thekkady after breakfast.
DAY 4 : Alleppey 06 OCT 2026 Proceed to Alleppey after breakfast.
DAY 5 : Trivandrum 07 OCT 2026 Proceed to Trivandrum after breakfast. Overnight at Trivandrum.
DAY 6 : Trivandrum Airport 08 OCT 2026 Proceed to the airport.
--- FILE supplier.pdf / PAGE 2 ---
Inclusions                                Exclusions
FLIGHT DETAILS
1. RPR TO KOCHI 03 OCT 2026 INDIGO 09.55AM-14.00PM VIA BENGALURU
2. Trivandrum TO RPR 08 OCT 2026 INDIGO 13.15PM-19.20PM VIA BENGALURU
"""


class TourSupplierRegressionTests(unittest.TestCase):
    def test_local_parser_recovers_supplier_table_days_cost_and_transit(self):
        days, _ = _source_days(SUPPLIER_TEXT)
        data = _local_tour_result(SUPPLIER_TEXT, days)
        self.assertEqual(data['client_name'], '')
        self.assertEqual(data['travel_dates'], '03/Oct/2026 – 08/Oct/2026')
        self.assertEqual(data['vehicle'], 'Ertiga A/c')
        self.assertEqual(len(data['days']), 6)
        self.assertEqual(data['days'][0]['title'], 'Munnar')
        self.assertNotIn('PAGE 2', data['days'][-1]['description'])
        self.assertEqual(len(data['hotels']), 4)
        self.assertEqual(data['hotels'][0]['hotel_name'], 'Munnar Summer castle')
        self.assertEqual(data['hotels'][0]['dates'], '03 Oct 2026 – 05 Oct 2026')
        self.assertEqual(data['package_costs'][0]['total_cost'], '71000')
        self.assertEqual(len(data['transit']), 2)

    def test_selectable_pdf_is_not_added_twice(self):
        with TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'supplier.pdf'
            fake.write_bytes(b'%PDF fake')
            with patch('performance_utils.extract_pdf_text', return_value='x' * 500):
                parts, text, _ = prepare_supplier_for_ai([fake], 'owner note', preserve_pdf_layout=True)
            self.assertEqual(text, 'owner note')
            self.assertEqual(len(parts), 1)
            self.assertNotIn('LOCAL SELECTABLE PDF TEXT', text)

    def test_empty_guest_label_does_not_consume_next_page_marker(self):
        text='AUTHORITATIVE GUEST / CLIENT NAME: \n--- FILE supplier.pdf / PAGE 1 ---\nDAY 1: Arrival'
        with patch('extractor.complete_json', return_value=None), patch(
            'performance_utils.collect_complete_supplier_text', return_value=text
        ):
            data=extract_itinerary_from_parts([], text, None, None)
        self.assertEqual(data['client_name'], '')


if __name__ == '__main__':
    unittest.main()
