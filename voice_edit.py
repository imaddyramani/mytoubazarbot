"""On-device voice transcription; no remote model or API key."""
from pathlib import Path
import os
import threading

_model=None
_lock=threading.Lock()


def transcribe_voice_note(path, api_key=None, model=None, mime_type='audio/ogg'):
    global _model
    source=Path(path)
    if not source.exists(): raise FileNotFoundError(str(source))
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError('Local voice reader is unavailable. Rebuild from the updated requirements or send the instruction as text.') from exc
    with _lock:
        if _model is None:
            _model=WhisperModel(os.getenv('LOCAL_WHISPER_MODEL','tiny'),device='cpu',compute_type='int8')
        segments,_=_model.transcribe(str(source),beam_size=1,vad_filter=True)
        text=' '.join(str(x.text or '').strip() for x in segments if str(x.text or '').strip()).strip()
    if not text: raise RuntimeError('The voice note could not be understood. Please resend clearly or type the instruction.')
    return text
