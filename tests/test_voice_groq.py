import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from voice_edit import transcribe_voice_note


class GroqVoiceTests(unittest.TestCase):
    def test_voice_uses_only_groq_transcription_endpoint(self):
        response=MagicMock(status_code=200)
        response.json.return_value={'text':'change guest name to Amit Sharma'}
        client=MagicMock()
        client.__enter__.return_value=client
        client.post.return_value=response
        with tempfile.TemporaryDirectory() as folder:
            audio=Path(folder)/'note.ogg'
            audio.write_bytes(b'OggS-test')
            with patch.dict(os.environ,{'GROQ_API_KEY':'test-key'},clear=False), \
                 patch('voice_edit.httpx.Client',return_value=client):
                text=transcribe_voice_note(audio,mime_type='audio/ogg')
        self.assertEqual(text,'change guest name to Amit Sharma')
        url=client.post.call_args.args[0]
        self.assertEqual(url,'https://api.groq.com/openai/v1/audio/transcriptions')
        self.assertEqual(client.post.call_args.kwargs['data']['model'],'whisper-large-v3-turbo')


if __name__=='__main__':
    unittest.main()
