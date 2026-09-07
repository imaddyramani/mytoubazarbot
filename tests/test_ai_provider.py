import os
import unittest
from unittest.mock import patch

import ai_provider


class AIProviderTests(unittest.TestCase):
    def setUp(self):
        ai_provider._MODEL_CACHE.clear()

    @patch.dict(os.environ, {"AI_PROVIDER": "local"}, clear=False)
    @patch("ai_provider._request")
    def test_local_mode_never_calls_remote(self, request):
        self.assertIsNone(ai_provider.complete_json("system", "source", {"type": "object"}))
        request.assert_not_called()

    @patch.dict(os.environ, {
        "AI_PROVIDER": "xkiro",
        "AI_FALLBACK_PROVIDER": "groq",
        "XKIRO_API_KEY": "test-xkiro",
        "GROQ_API_KEY": "test-groq",
        "GROQ_MODEL": "test-model",
    }, clear=False)
    def test_groq_is_used_after_xkiro_failure(self):
        calls=[]

        def fake_request(provider, *args, **kwargs):
            calls.append(provider)
            if provider == "xkiro":
                raise ai_provider.AIProviderError("xKiro unavailable")
            return ({"answer": "ok"}, "test-model")

        schema={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
        with patch("ai_provider._request", side_effect=fake_request):
            result=ai_provider.complete_json("system", "source", schema)
        self.assertEqual(result, {"answer": "ok"})
        self.assertEqual(calls, ["xkiro", "groq"])

    def test_validation_coerces_safe_scalar_types(self):
        schema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
            "required": ["name", "count"],
        }
        self.assertEqual(ai_provider._validate({"name": 123, "count": "2"}, schema), {"name": "123", "count": 2})


if __name__ == "__main__":
    unittest.main()
