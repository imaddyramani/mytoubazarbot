# xKiro setup on Northflank

The bot remains local-first. xKiro improves Tour writing and repairs incomplete local Air, Bus, Hotel, or Tour extraction. A remote failure never replaces a valid local result or prevents printing.

## Add the API key

1. Open your Northflank project and select the MyTourBazar service.
2. Open **Environment**.
3. Add the following as **runtime variables** (ENV), not build arguments.
4. Save the changes and restart/redeploy the service.

```env
AI_PROVIDER=xkiro
AI_FALLBACK_PROVIDER=none
AI_TIMEOUT_SECONDS=80
EXTRACTION_TIMEOUT_SECONDS=300
AI_MAX_INPUT_CHARS=1000000
AI_MAX_VISION_PAGES=24
AI_VISION_BATCH_PAGES=6
XKIRO_API_KEY=YOUR_XKIRO_KEY
XKIRO_TEXT_MODEL=qwen/qwen3.5-plus:free
XKIRO_VISION_MODEL=qwen/qwen3.5-plus:free
```

Keep your existing `BOT_TOKEN` and `ADMIN_USER_IDS` variables unchanged.

The two Qwen values lock both text and image/PDF recovery to xKiro's free Qwen model. If xKiro renames or removes that model, remove both model variables temporarily; the bot will then read xKiro's live catalogue and select a suitable free model.

Optional GROQ fallback:

```env
GROQ_API_KEY=YOUR_GROQ_KEY
GROQ_MODEL=YOUR_CURRENT_GROQ_TEXT_MODEL_ID
GROQ_VISION_MODEL=YOUR_CURRENT_GROQ_VISION_MODEL_ID
```

To enable GROQ fallback, set `AI_FALLBACK_PROVIDER=groq` and add all GROQ variables above. Otherwise an xKiro failure simply returns the local result.

## Recommended Northflank secret group

Instead of placing secrets directly on one service, you may create a Northflank secret group containing the runtime variables above and restrict it to the MyTourBazar service. After editing the group, use **restart dependents** so the running container receives the new values.

## Disable remote AI

```env
AI_PROVIDER=local
```

## Safety and performance

- Supplier PDF/image text is extracted locally first.
- Complete selectable supplier text is sent to Qwen first. PDF/image pages are rendered in low-memory batches of `AI_VISION_BATCH_PAGES`; `AI_MAX_VISION_PAGES` is the deployment safety ceiling for scanned pages.
- Remote text input is capped by `AI_MAX_INPUT_CHARS` (up to 1,000,000 characters). Split unrelated bookings rather than combining them into one request.
- Air, Bus, Hotel and Tour use Qwen as the primary structuring engine. Local rules remain only for validation, calculations, rendering and provider-outage fallback.
- Requests are capped below xKiro's blocking-request limit.
- JSON is validated and malformed output is retried once.
- xKiro quota, permission, timeout, or provider errors fall back safely.
- Configure a monthly API-key spending limit in the xKiro dashboard before adding wallet funds.
- Never commit `.env`, Telegram tokens, or API keys to GitHub.
