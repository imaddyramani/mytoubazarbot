"""Telegram voice transcription isolated to Groq; all other AI stays on xKiro."""
from pathlib import Path
import os
import httpx


def transcribe_voice_note(path, api_key=None, model=None, mime_type='audio/ogg'):
    source=Path(path)
    if not source.exists(): raise FileNotFoundError(str(source))
    key=os.getenv('GROQ_API_KEY','').strip()
    if not key:
        raise RuntimeError('GROQ_API_KEY is required for voice notes.')
    stt_model=os.getenv('GROQ_STT_MODEL','whisper-large-v3-turbo').strip() or 'whisper-large-v3-turbo'
    timeout=max(15.0,min(float(os.getenv('VOICE_TIMEOUT_SECONDS','45')),90.0))
    prompt=(
        'Travel agency instruction. Preserve Indian guest and hotel names, dates, '
        'PNR, room, EB, fare, baggage, itinerary, Hindi and Hinglish wording.'
    )
    with source.open('rb') as audio, httpx.Client(timeout=timeout) as client:
        response=client.post(
            'https://api.groq.com/openai/v1/audio/transcriptions',
            headers={'Authorization':f'Bearer {key}'},
            data={'model':stt_model,'response_format':'json','temperature':'0','prompt':prompt},
            files={'file':(source.name,audio,mime_type or 'audio/ogg')},
        )
    if response.status_code>=400:
        raise RuntimeError(f'Groq voice transcription failed (HTTP {response.status_code}).')
    try: text=str(response.json().get('text') or '').strip()
    except Exception as exc: raise RuntimeError('Groq returned an invalid voice response.') from exc
    if not text: raise RuntimeError('The voice note could not be understood. Please resend clearly or type the instruction.')
    return text
