# Voice choices

The dashboard mounts `GET /voice/voices` and `PUT /voice/voices` under
`/api/plugins/hermex-push`, behind its existing authentication gate.
Both accept an optional `profile` query. Missing, empty and `current` mean
the dashboard's active profile; named profiles use Hermes's profile resolver.

GET returns `{ok: true, live: {voice, choices}, cascade: {voice, choices}}`.
PUT takes `{mode: "live" | "cascade", voice}` and returns the same envelope.
It updates only `voice.gpt_live.voice` or `tts.openai.voice`, using the host's
config writer and mutation lock. It reads raw YAML without expanding secrets.
Unknown voices/modes return 400; missing profiles return 404.

Audio, streaming speech and GPT-Live use the host's built-in audio routes.
This module has no session marks, glossary, transcription or archive routes.
