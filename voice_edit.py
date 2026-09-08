"""Reliable on-device Telegram voice transcription; no external STT API."""
from pathlib import Path
import os
import threading

_model=None
_lock=threading.Lock()


def _load_model():
    global _model
    if _model is not None: return _model
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise RuntimeError('Local voice reader is unavailable. Rebuild using the updated Dockerfile.') from exc
    name=os.getenv('LOCAL_WHISPER_MODEL','tiny').strip() or 'tiny'
    cache=os.getenv('WHISPER_CACHE_DIR','/app/.cache/whisper').strip() or None
    kwargs={
        'device':'cpu','compute_type':'int8','cpu_threads':max(1,int(os.getenv('WHISPER_CPU_THREADS','2'))),
        'num_workers':1,
    }
    if cache: kwargs['download_root']=cache
    try:
        _model=WhisperModel(name,local_files_only=True,**kwargs)
    except Exception:
        # Local desktop installs may not have used the Docker build cache yet.
        _model=WhisperModel(name,local_files_only=False,**kwargs)
    return _model


def transcribe_voice_note(path, api_key=None, model=None, mime_type='audio/ogg'):
    global _model
    source=Path(path)
    if not source.exists(): raise FileNotFoundError(str(source))
    with _lock:
        reader=_load_model()
        segments,_=reader.transcribe(
            str(source),beam_size=1,best_of=1,vad_filter=False,
            condition_on_previous_text=False,
        )
        text=' '.join(str(x.text or '').strip() for x in segments if str(x.text or '').strip()).strip()
    if not text: raise RuntimeError('The voice note could not be understood. Please resend clearly or type the instruction.')
    return text
