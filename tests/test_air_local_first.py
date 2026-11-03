import unittest
from unittest.mock import patch

import flight_extractor as air


def complete_local_ticket():
    return {
        "booking_id": "MTB12345",
        "booking_date": "01 Nov 2026",
        "airline_pnr": "ABC123",
        "gds_pnr": "",
        "status": "VOUCHERED",
        "mobile": "",
        "airline_customer_support": "",
        "baggage_summary": "Check-in: 15kg | Cabin: 7kg",
        "special_ancillary_summary": "",
        "passengers": [{
            "name": "Komalkant Kumar Sahu", "title": "Mr.",
            "ticket_number": "0987654321", "type": "Adult",
            "dob": "", "baggage": "Check-in: 15kg | Cabin: 7kg",
            "special_ancillary": "",
        }],
        "segments": [{
            "flight": "IndiGo", "flight_number": "6E 659",
            "aircraft": "Airbus A320", "cabin": "Economy", "fare_type": "Saver",
            "dep_time": "05:55", "dep_city": "Delhi", "dep_code": "DEL",
            "dep_date": "11 Sep 2026", "dep_airport": "Indira Gandhi International Airport",
            "dep_terminal": "Terminal 2", "arr_time": "07:55", "arr_city": "Raipur",
            "arr_code": "RPR", "arr_date": "11 Sep 2026",
            "arr_airport": "Swami Vivekananda Airport", "arr_terminal": "",
            "duration": "2h", "stops": "Non-stop", "layover": "",
        }],
        "base_fare": 5000.0, "taxes": 1000.0, "gross_total": 6000.0,
        "payment_items": [{"label": "Air Fare", "amount": 6000.0}],
    }


class AirLocalFirstTests(unittest.TestCase):
    def test_complete_local_ticket_does_not_need_ai(self):
        self.assertFalse(air._local_air_needs_ai(complete_local_ticket()))

    def test_missing_passenger_name_triggers_ai_verification(self):
        data = complete_local_ticket()
        data["passengers"][0]["name"] = ""
        self.assertTrue(air._local_air_needs_ai(data))

    def test_missing_airport_name_triggers_ai_verification(self):
        data = complete_local_ticket()
        data["segments"][0]["arr_airport"] = ""
        self.assertTrue(air._local_air_needs_ai(data))

    def test_missing_travel_date_triggers_ai_verification(self):
        data = complete_local_ticket()
        data["segments"][0]["dep_date"] = ""
        self.assertTrue(air._local_air_needs_ai(data))


if __name__ == "__main__":
    unittest.main()
