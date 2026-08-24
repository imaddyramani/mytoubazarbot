from pathlib import Path
from google import genai
from google.genai import types
from ai_retry import call_with_high_demand_retry

VOICE_PROMPT = '''You are transcribing a MyTourBazar travel-agent voice note. It may contain instructions for creating a new Tour batch or edits to an existing saved itinerary/document.
Return only a clean text transcription/instruction for the Tour creation/editing engine.
Preserve every number, price, airport/city code, date, time, hotel name, room type, room category, passenger category and service number exactly as spoken.
When the speaker discusses Tour costing, preserve the intent as the FINAL CUSTOMER SELLING RATE (Adult/CWB/CNB/EB/Child) and never rewrite it as markup. Understand mixed Hindi/English/Hinglish naturally.
Travel sectors may mix flight, train and bus. Keep each distinct journey/sector on its own line when the speaker describes multiple sectors.
Do not invent information, do not summarize away details, and do not add prefixes the speaker did not request.'''


def transcribe_voice_note(path, api_key, model, mime_type='audio/ogg'):
    p=Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    client=genai.Client(api_key=api_key)
    contents=[VOICE_PROMPT, types.Part.from_bytes(data=p.read_bytes(), mime_type=mime_type or 'audio/ogg')]
    response=call_with_high_demand_retry(lambda: client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(temperature=0),
    ))
    text=str(response.text or '').strip()
    if not text:
        raise RuntimeError('Gemini returned an empty voice transcription.')
    return text
