import unittest
from unittest.mock import patch

import smart_assistant


class SmartAssistantTests(unittest.TestCase):
    def test_agent_plan_uses_one_remote_call_for_chat(self):
        remote={
            "action":"chat", "kind":"unknown", "reference":"", "instruction":"help",
            "reason":"Owner asked for help.", "needs_user_input":"",
            "answer":"Send the supplier PDF and choose the matching print workflow.",
        }
        with patch("smart_assistant.complete_json",return_value=remote) as completion:
            result=smart_assistant.agent_plan("How should I start?")
        self.assertEqual(result["action"],"chat")
        self.assertTrue(result["answer"])
        completion.assert_called_once()

    def test_local_classification_avoids_remote_when_confident(self):
        source="Airline Flight Number E-ticket Airport GDS PNR Baggage"
        with patch("smart_assistant.complete_json") as completion:
            result=smart_assistant.classify([],source)
        self.assertEqual(result["kind"],"flight")
        completion.assert_not_called()


if __name__ == "__main__":
    unittest.main()
