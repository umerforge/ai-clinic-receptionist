# clinic-voice-agent

A LiveKit voice receptionist for a small clinic in Pakistan. It checks doctor
availability, books, cancels and reschedules appointments, answers clinic
questions, and saves callback requests. It speaks clear, concise English.

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

## Speech

Speech is English-only: **Deepgram Nova-3** for STT and **Deepgram Aura** for
TTS. `DEEPGRAM_API_KEY` is required. The agent keeps replies short and clear so
the voice stays intelligible over the phone.

Useful settings in `.env`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `DEEPGRAM_API_KEY` | required | Deepgram STT and TTS |
| `TTS_MODEL` | `aura-2-thalia-en` | Deepgram Aura voice |
| `BACKGROUND_AMBIENCE_FILE` | `assets/hospital-ambience.mp3` | Looping call ambience |
| `BACKGROUND_MIX_VOLUME` | `0.12` | Ambience level under the agent voice |

## Voice samples

Generate five English samples without placing a call:

```bash
python test_voices.py
```

Files are written to `voice_tests/`. Use `--model` or `--out` to try another
Aura voice. `--dry-run` prints the samples without calling Deepgram.

## Background audio

The ElevenLabs hospital ambience in `assets/hospital-ambience.mp3` loops under
the call at low volume, and soft typing plays only while the agent is thinking or
calling a tool. The clip is mixed into the **same audio track** as the agent's
voice, so a browser or SIP client only has to play the primary agent track.

Replace `assets/hospital-ambience.mp3` with another recording, or point
`BACKGROUND_AMBIENCE_FILE` at a different file. If the file is missing, the agent
falls back to the built-in office ambience and logs a warning. Disable all
background audio with:

```env
ENABLE_BACKGROUND_AUDIO=false
```

`BACKGROUND_MIX_VOLUME` controls the ambience under the agent voice on real
calls and defaults to `0.12`. The ambience automatically ducks to half volume
while the agent speaks, keeping the voice clear. `BACKGROUND_AMBIENCE_VOLUME`
controls the louder local playback in console mode and defaults to `0.30`. Keep
the call mix around `0.10`–`0.18` so speech stays clear.

Console mode does not publish a room track, so the agent plays the same ambience
through your local speakers when you run `python main.py console`. If console
mode still has no ambience, check the
`console ambience playing locally: ...` log line and your Mac's output device.

Troubleshooting tip: the agent logs `speech=deepgram ...` at startup, and if the
ambience file is missing it logs `background ambience file not found`. The MP3
was normalized to a `-3 dB` peak and crossfaded for seamless looping. If it is
still too quiet on a call, raise `BACKGROUND_MIX_VOLUME` to `0.15` or `0.18`.

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
|-- main.py            # AgentSession, English speech, background audio, call logging
|-- prompts.py         # English-only system prompt, greeting, doctor list
|-- tools.py           # validation and n8n webhook tools
|-- config.py          # .env loading and settings
|-- test_voices.py     # English Deepgram Aura voice samples
|-- assets/
|   `-- hospital-ambience.mp3  # looping ElevenLabs call ambience
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
- **Deepgram 401**: check `DEEPGRAM_API_KEY` and that it has STT and TTS access.
- **Deepgram voice not found**: set `TTS_MODEL` to an available Aura voice model.
- **Groq 429**: the free daily token limit is hit. Wait, or try a smaller model.
- **Groq 404 model not found**: Groq retires models. Run
  `curl https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"`
  and set `GROQ_MODEL` to an available model. The default is
  `openai/gpt-oss-20b`; `openai/gpt-oss-120b` is a stronger alternative.
- **Turn detector model errors**: run `python main.py download-files` again.
- **Always "unknown" caller**: browser calls have no SIP id. Set `TEST_PHONE`.

## Notes and assumptions

- `livekit-agents` is pinned to `~=1.8.8`. The code uses
  `TurnHandlingOptions(turn_detection=EnglishModel())`, the English text turn
  detector. LiveKit now marks this detector as deprecated in favour of the audio
  `inference.TurnDetector`; it still ships in 1.x, and keeping it makes
  `download-files` download the detector model. A future upgrade to 2.x should
  switch to the audio detector.
- The original default `llama-3.3-70b-versatile` was retired by Groq, so the
  default is now `openai/gpt-oss-20b` (tools verified on Groq).
- Speech is English-only by design. If a caller uses another language, the agent
  politely says it can continue in English.
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
