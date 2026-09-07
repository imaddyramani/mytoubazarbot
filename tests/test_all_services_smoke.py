import os
import unittest
from unittest.mock import patch

from bus_ticket import extract_bus_ticket
from flight_extractor import extract_flight_ticket
from hotel_voucher import extract_hotel_voucher
from smart_assistant import generate_package_from_brief


class AllServicesSmokeTests(unittest.TestCase):
    @patch.dict(os.environ,{"AI_PROVIDER":"local"},clear=False)
    def test_local_tour_air_bus_hotel_paths(self):
        tour=generate_package_from_brief(
            "Make a Goa 3 nights 4 days package for Mr Amit, 2 adults, "
            "3 star hotel, breakfast and private cab"
        )
        self.assertEqual(tour["destination"],"Goa")
        self.assertEqual(len(tour["days"]),4)

        air=extract_flight_ticket([],"""Booking ID: AIR123
Airline PNR: PNR123
Status: Confirmed
Passenger: Mr Amit Sharma
Flight Number: AI 101
From: Delhi DEL
To: Mumbai BOM
Departure: 10:00 10 Oct 2026
Arrival: 12:10 10 Oct 2026
Baggage: 15 kg
Base Fare: INR 5000
Taxes: INR 1000""",None,None)
        self.assertEqual(air["airline_pnr"],"PNR123")
        self.assertEqual([x["flight_number"] for x in air["segments"]],["AI 101"])

        bus=extract_bus_ticket([],"""Booking ID: BUS123
Bus PNR: BP123
Status: Confirmed
Operator: Test Travels
Journey Date: 10 Oct 2026
From: Delhi
To: Jaipur
Departure Time: 10:00 PM
Arrival Time: 04:00 AM
Boarding Point: ISBT
Dropping Point: Sindhi Camp
1. Mr Amit Sharma Adult Seat 12A
Base Fare: INR 1000
Taxes: INR 100""",None,None)
        self.assertEqual(bus["booking_id"],"BUS123")
        self.assertEqual(bus["pnr"],"BP123")
        self.assertEqual(len(bus["passengers"]),1)

        hotel=extract_hotel_voucher([],"""Reservation ID: HTL123
Guest Name: Mr Amit Sharma
Hotel Name: Test Residency
Hotel Address: MG Road, Goa
Hotel City: Goa
Check-in: 10 Oct 2026
Check-out: 13 Oct 2026
Nights: 3
Room Type: Deluxe Room
Room Count: 1
Meal Plan: Breakfast
Base Fare: INR 12000
Taxes: INR 1800""",None,None)
        self.assertEqual(hotel["reservation_id"],"HTL123")
        self.assertEqual(hotel["hotel_name"],"Test Residency")


if __name__ == "__main__":
    unittest.main()
