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

    def test_short_room_and_eb_rates_use_booking_quantities(self):
        self.hotel['extra_bed_count']=2
        cost=_parse_hotel_cost_input('room 4000 eb 1500',0,self.hotel)
        self.assertEqual(cost['rooms'],2)
        self.assertEqual(cost['nights'],3)
        self.assertEqual(cost['extra_beds'],2)
        self.assertEqual(cost['room_total'],24000)
        self.assertEqual(cost['eb_total'],9000)
        self.assertEqual(cost['total'],33000)

    def test_extra_person_wording_is_chargeable_eb_count(self):
        self.hotel['occupancy_summary']='2 Rooms / 6 Pax (Includes 02 Extra Persons)'
        cost=_parse_hotel_cost_input('room 4000 eb 1500',0,self.hotel)
        self.assertEqual(cost['extra_beds'],2)
        self.assertEqual(cost['total'],33000)


if __name__=='__main__':
    unittest.main()
