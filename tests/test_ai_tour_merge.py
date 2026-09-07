import unittest

from smart_assistant import merge_ai_package


class TourMergeTests(unittest.TestCase):
    def test_ai_polishes_days_without_replacing_supplier_cost_or_transit(self):
        local={
            "client_name": "Mr Test",
            "days": [{"day": "1", "title": "Arrival", "description": "Arrive", "date": "1 Oct", "stay": "", "meal_plan": "", "optional_activities": []}],
            "inclusions": ["Breakfast included"],
            "exclusions": ["Flights excluded"],
            "hotels": [],
            "transit": [{"flight_number": "6E 1"}],
            "package_costs": [{"per_adult": "15999"}],
        }
        remote={
            "client_name": "Wrong Name",
            "days": [{"day": "1", "title": "Warm Arrival", "description": "A polished arrival paragraph.", "date": "Wrong", "stay": "Hotel", "meal_plan": "Breakfast", "optional_activities": ["Market walk (optional)"]}],
            "inclusions": ["Private sightseeing"],
            "exclusions": ["Personal expenses"],
            "transit": [{"flight_number": "MADE UP"}],
            "package_costs": [{"per_adult": "999"}],
        }
        result=merge_ai_package(local,remote)
        self.assertEqual(result["client_name"], "Mr Test")
        self.assertEqual(result["transit"][0]["flight_number"], "6E 1")
        self.assertEqual(result["package_costs"][0]["per_adult"], "15999")
        self.assertEqual(result["days"][0]["date"], "1 Oct")
        self.assertEqual(result["days"][0]["description"], "A polished arrival paragraph.")
        self.assertIn("Breakfast included", result["inclusions"])
        self.assertIn("Private sightseeing", result["inclusions"])


if __name__ == "__main__":
    unittest.main()
