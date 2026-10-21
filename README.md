# AI Clinic Receptionist

A LiveKit voice receptionist for **Rahim** clinic. Tina handles appointment
availability, booking, rescheduling, cancellation, clinic information, and
callback requests in concise English.

The agent talks only to n8n. n8n owns the Supabase and calendar integrations, so
the voice worker holds no database credentials.

## Repository layout

```text
.
|-- README.md
`-- clinic-voice-agent/
    |-- main.py
    |-- prompts.py
    |-- tools.py
    |-- config.py
    |-- n8n/
    |   |-- clinic-agent.workflow.json
    |   `-- README.md
    |-- mock_n8n.py
    |-- test_voices.py
    |-- requirements.txt
    |-- .env.example
    `-- README.md
```

## Quick start

```bash
cd clinic-voice-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Fill in LiveKit, Deepgram, Groq, and n8n values.
python main.py download-files
python main.py console
```

For setup details, the webhook contract, architecture diagram, and
troubleshooting, see [`clinic-voice-agent/README.md`](clinic-voice-agent/README.md).

## n8n workflow

The sanitized workflow export and import instructions are in
[`clinic-voice-agent/n8n/`](clinic-voice-agent/n8n/). Reattach your own Supabase,
Google Calendar, and Gmail credentials after import.

Never commit `.env` files, webhook secrets, API keys, or exported workflows that
contain credentials or pinned execution data.
