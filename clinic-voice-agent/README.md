# clinic-voice-agent

A LiveKit voice receptionist for a small clinic in Pakistan. It checks doctor
availability, books, cancels and reschedules appointments, answers clinic
questions, and saves callback requests. It speaks Urdu, English, or a natural
mix of both, and matches the caller's language on every turn.

The agent talks only to n8n. It never talks to Supabase and holds no Supabase key.

```
Patient -> LiveKit room -> Agent (STT -> LLM -> TTS) -> tool -> n8n -> Supabase
```

## Setup

Requires Python 3.10+.

```bash
cd clinic-voice-agent
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# edit .env and fill in the required keys
```

For local testing, set:

```env
N8N_WEBHOOK_BASE_URL=http://localhost:5678/webhook
N8N_WEBHOOK_SECRET=dev-secret
```

## Speech and language

`SPEECH_PROVIDER=auto` uses **Soniox** when `SONIOX_API_KEY` is set. Soniox
handles real-time Urdu and English code-switching for both STT and TTS, so it is
required for Urdu calls. The TTS language switches between `ur` and `en`
automatically from the caller's transcript.

Without a Soniox key, the agent falls back to **Deepgram Nova-3 + Aura**, which
is English-only. Startup logs a clear warning and the prompt disables Urdu
replies instead of silently producing bad speech.

Useful settings in `.env`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `SPEECH_PROVIDER` | `auto` | `auto`, `soniox`, or `deepgram` |
| `SONIOX_API_KEY` | empty | Enables Urdu + English speech |
| `SONIOX_STT_MODEL` | `stt-rt-v5` | Real-time STT model |
| `SONIOX_TTS_MODEL` | `tts-rt-v2` | Real-time TTS model |
| `SONIOX_VOICE` | `Maya` | TTS voice name |
| `STT_LANGUAGE_HINTS` | `ur,en` | Languages Soniox should expect |
| `TTS_SPEED` | `1.0` | Speaking rate, `0.7` to `1.3` |
| `SONIOX_ENDPOINT_DELAY_MS` | `800` | Endpointing delay before a reply |
| `SONIOX_ENDPOINT_SENSITIVITY` | `0.3` | Endpointing sensitivity |
| `SONIOX_ENDPOINT_LATENCY_LEVEL` | `1` | Higher is more aggressive |

## Voice samples

Generate five Urdu and five English samples, plus three mixed-language samples,
without placing a call:

```bash
python test_voices.py
```

Files are written to `voice_tests/`. Use `--voice`, `--model`, `--speed`, or
`--out` to try different settings. `--dry-run` prints the script without calling
Soniox.

## Background audio

A quiet office ambience plays under the call, and soft typing plays only while
the agent is thinking or calling a tool. Disable it with:

```env
ENABLE_BACKGROUND_AUDIO=false
```

Volumes and typing frequency are configurable in `.env`.

## Run

1. Download the VAD and turn-detector models once:

```bash
python main.py download-files
```

2. Start the mock n8n server in one terminal:

```bash
python mock_n8n.py
```

3. Run the agent in another terminal:

```bash
python main.py console
```

Or use `python main.py dev` and connect with the LiveKit Agents Playground.

For browser testing without a SIP caller id, set `TEST_PHONE` in `.env`.

## Webhook contract

All requests are `POST {N8N_WEBHOOK_BASE_URL}/{path}` with
`Content-Type: application/json` and `X-Webhook-Secret: {N8N_WEBHOOK_SECRET}`.
n8n must return HTTP 200 with a JSON object.

| Path | Request | Success response | Failure reasons |
| --- | --- | --- | --- |
| `check-availability` | `doctor_name`, `date` (`YYYY-MM-DD`) | `{"success":true,"slots":["..."]}` | empty slots |
| `book-appointment` | `patient_name`, `phone`, `doctor_name`, `starts_at`, `reason` | `{"success":true,"appointment_id","doctor","starts_at"}` | `slot_taken` |
| `cancel-appointment` | `phone`, `date` | `{"success":true}` | `not_found` |
| `clinic-info` | `key` (`timings`, `address`, `fee`, `emergency`, `closed_days`) | `{"success":true,"value":"..."}` | `invalid_input` |
| `reschedule-appointment` | `phone`, `old_date`, `new_starts_at` | `{"success":true,"starts_at":"..."}` | `not_found`, `slot_taken` |
| `request-callback` | `phone`, `reason` | `{"success":true}` | `system_error` |
| `log-call` | `caller_phone`, `duration_sec`, `outcome`, `transcript` | `{"success":true}` | best effort only |

The agent also returns `invalid_input` (bad date, time, phone or missing value)
without calling n8n, and `system_error` for 401, other non-200 responses,
timeouts, connection errors or invalid JSON.

## Files

```
clinic-voice-agent/
|-- main.py            # AgentSession, speech providers, background audio, call logging
|-- prompts.py         # language-matching system prompt, greeting, doctor list
|-- tools.py           # validation and n8n webhook tools
|-- config.py          # .env loading, provider resolution and settings
|-- test_voices.py     # Urdu/English Soniox voice samples
|-- mock_n8n.py        # local fake webhooks on port 5678
|-- .env.example
|-- .gitignore
|-- requirements.txt
`-- README.md
```

## Troubleshooting

- **401 from n8n**: `N8N_WEBHOOK_SECRET` does not match the n8n credential.
- **Timeouts**: n8n is not reachable at `N8N_WEBHOOK_BASE_URL`. Check the URL,
  tunnel and that the workflow is active.
- **No Urdu voice**: set `SONIOX_API_KEY`. Deepgram fallback is English-only and
  logs a warning at startup.
- **Soniox 401**: check `SONIOX_API_KEY` and that the key has TTS and STT access.
- **Soniox voice not found**: list voices in the Soniox dashboard and set
  `SONIOX_VOICE` to an available name.
- **Groq 429**: the free daily token limit is hit. Wait, or try a smaller model.
- **Groq 404 model not found**: Groq retires models. Run
  `curl https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"`
  and set `GROQ_MODEL` to an available model. The default is
  `openai/gpt-oss-20b`; `openai/gpt-oss-120b` is a stronger alternative.
- **Turn detector model errors**: run `python main.py download-files` again.
- **Always "unknown" caller**: browser calls have no SIP id. Set `TEST_PHONE`.

## Notes and assumptions

- `livekit-agents` is pinned to `~=1.8.8`. The code uses
  `TurnHandlingOptions(turn_detection=MultilingualModel())`, the multilingual
  text turn detector. LiveKit now marks that detector as deprecated in favour of
  the audio `inference.TurnDetector`; it still ships in 1.x, and keeping it makes
  `download-files` download the detector model. A future upgrade to 2.x should
  switch to the audio detector.
- The original default `llama-3.3-70b-versatile` was retired by Groq, so the
  default is now `openai/gpt-oss-20b` (tools verified on Groq).
- Soniox is the only tested stack here that supports real-time Urdu/English
  code-switching end to end. Azure real-time STT supports `ur-IN`, not `ur-PK`;
  Deepgram `multi` does not include Urdu; Google STT supports `ur-PK` only with
  Chirp models in specific regions.
- The agent is instructed to be honest that it is an AI. It never claims to be
  human or invents personal experiences.
- Latency tuning: `openai/gpt-oss-20b` is the default fast Groq model,
  preemptive generation is enabled (adds some token usage), and endpointing is
  set to `0.35s` minimum / `2.0s` maximum. Keep webhook responses under ~1s for
  the best experience.
- A successful reschedule is recorded as the `booked` call outcome because the
  agreed outcome list has no `rescheduled` value.
- Caller phone priority: `sip.phoneNumber`, then `TEST_PHONE`, then `unknown`.
  Console mode is a fake job with no remote participant, so it skips the wait
  and uses `TEST_PHONE` (or `unknown`).
- Pakistani phone forms `0300...`, `92300...`, `+92300...` and `00923...` are
  normalised to `+92...`. Spaces, dashes, dots and parentheses are stripped.
- The mock server accepts three fixed slots tomorrow (09:00, 10:00, 11:00).
  Booking a 10:xx slot returns `slot_taken` so the error path can be tested.
  If `N8N_WEBHOOK_SECRET` is unset, the mock uses `dev-secret`.
- `Asia/Karachi` has no daylight saving, so all generated times use `+05:00`.
- Call logging is best effort, never raises, and waits at most five seconds.
