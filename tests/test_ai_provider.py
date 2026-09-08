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

    @patch.dict(os.environ, {
        "AI_PROVIDER":"xkiro","XKIRO_API_KEY":"test-key",
        "AI_VISION_BATCH_PAGES":"6","AI_MAX_VISION_PAGES":"24",
    }, clear=False)
    def test_visual_document_is_processed_in_page_batches(self):
        schema={"type":"object","properties":{"rows":{"type":"array"}},"required":["rows"]}
        calls=[]
        def fake_request(provider,*args,**kwargs):
            calls.append(kwargs.get('vision_offset'))
            return ({"rows":[{"name":f"P{kwargs.get('vision_offset')}"}]},"qwen-test")
        with patch("ai_provider._vision_unit_count",return_value=13), patch("ai_provider._request",side_effect=fake_request):
            result=ai_provider.complete_json("system","complete text",schema,image_paths=["supplier.pdf"])
        self.assertEqual(calls,[0,6,12])
        self.assertEqual([row['name'] for row in result['rows']],["P0","P6","P12"])

    def test_text_rich_later_pdf_pages_are_not_duplicated_as_images(self):
        import tempfile
        from pathlib import Path
        import fitz
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'supplier.pdf'
            document=fitz.open()
            for text in ('Booking header '+('A'*200),'Fare table '+('B'*200),''):
                page=document.new_page()
                if text: page.insert_textbox(fitz.Rect(50,50,545,790),text,fontsize=8)
            document.save(path); document.close()
            self.assertEqual(ai_provider._pdf_visual_indexes(path),[0,2])


if __name__ == "__main__":
    unittest.main()
