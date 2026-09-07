import unittest
from unittest.mock import patch

import editor


class AIEditorTests(unittest.TestCase):
    def test_smart_edit_changes_only_requested_nested_field(self):
        original={
            "client_name":"Mr Test",
            "days":[
                {"day":"1","title":"Arrival","description":"Old arrival"},
                {"day":"2","title":"Tour","description":"Keep this"},
            ],
            "package_costs":[{"per_adult":"15000"}],
        }
        remote={
            "operations":[{"action":"set","path":"days.0.description","value":"Airport pickup and hotel check-in"}],
            "fare_changed":"false",
            "fare":0,
        }
        with patch("editor.complete_json",return_value=remote):
            result,fare=editor.apply_edit("package",original,"make the arrival wording warmer",current_fare=19000)
        self.assertEqual(result["days"][0]["description"],"Airport pickup and hotel check-in")
        self.assertEqual(result["days"][1]["description"],"Keep this")
        self.assertEqual(result["package_costs"],original["package_costs"])
        self.assertEqual(fare,19000)

    def test_smart_edit_rejects_unknown_or_private_paths(self):
        remote={
            "operations":[
                {"action":"set","path":"admin_token","value":"bad"},
                {"action":"set","path":"days.0._secret","value":"bad"},
            ],
            "fare_changed":"false",
            "fare":0,
        }
        with patch("editor.complete_json",return_value=remote):
            with self.assertRaises(ValueError):
                editor.apply_edit("package",{"days":[{"description":"safe"}]},"do something unsupported")

    def test_smart_edit_can_append_one_inclusion(self):
        remote={
            "operations":[{"action":"append","path":"inclusions","value":"Private airport transfer"}],
            "fare_changed":"false",
            "fare":0,
        }
        with patch("editor.complete_json",return_value=remote):
            result,_=editor.apply_edit("package",{"inclusions":["Breakfast"]},"include a private airport transfer")
        self.assertEqual(result["inclusions"],["Breakfast","Private airport transfer"])

    def test_total_nights_does_not_change_customer_fare(self):
        with patch("editor.complete_json") as completion:
            result,fare=editor.apply_edit("hotel",{"nights":"2"},"set total nights to 3",current_fare=12000)
        self.assertEqual(result["nights"],"3")
        self.assertEqual(fare,12000)
        completion.assert_not_called()

    def test_gds_pnr_does_not_overwrite_airline_pnr(self):
        original={"airline_pnr":"AIR123","gds_pnr":"OLD123"}
        result,_=editor.apply_edit("flight",original,"set GDS PNR to NEW123")
        self.assertEqual(result["airline_pnr"],"AIR123")
        self.assertEqual(result["gds_pnr"],"NEW123")

    def test_hotel_confirmation_updates_schema_field(self):
        result,_=editor.apply_edit("hotel",{"reservation_id":"OLD"},"set confirmation number to NEW456")
        self.assertEqual(result["reservation_id"],"NEW456")
        self.assertNotIn("booking_id",result)


if __name__ == "__main__":
    unittest.main()
