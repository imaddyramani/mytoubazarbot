import unittest

from bot import _parse_hotel_cost_input


class HotelCostingTests(unittest.TestCase):
    def setUp(self):
        self.hotel={
            'check_in':'11 Sep 2026','check_out':'14 Sep 2026',
            'nights':'','room_count':2,'extra_bed_count':0,
        }

    def test_generic_per_night_rate_uses_nights_not_room_count(self):
        cost=_parse_hotel_cost_input('for night 3500',0,self.hotel)
        self.assertEqual(cost['nights'],3)
        self.assertEqual(cost['rate_scope'],'hotel')
        self.assertEqual(cost['total'],10500)

    def test_explicit_room_rate_uses_rooms_and_nights(self):
        cost=_parse_hotel_cost_input('3500 per room per night',0,self.hotel)
        self.assertEqual(cost['rate_scope'],'room')
        self.assertEqual(cost['total'],21000)

    def test_extra_bed_rate_is_added_for_every_night(self):
        cost=_parse_hotel_cost_input('hotel 3500 per night and EB 1200 per night',0,self.hotel)
        self.assertEqual(cost['extra_beds'],1)
        self.assertEqual(cost['total'],14100)


if __name__=='__main__':
    unittest.main()
