# R27 native Telegram voice processing

The native voice worker accepts an authenticated private Telegram delivery through the same inbox as text. The original provider envelope remains available for ownership and replay checks. The worker stores the combined text and releases its lease in one transaction so the ordinary safety and diary workers can resume. A speech-derived entry keeps the `telegram_voice` source.

Known local caption forms get their turn before download. The worker checks audio onboarding, model configuration and the selected tracker version before sending audio. It checks again before saving the transcript and when later workers use it. Structured model requests derived from speech require both audio and the usual text categories at send time. Long ordinary voice transcripts use the bounded, overlapping safety fragments from the frozen implementation.

## Verification

- The frozen Python source passed 69 tests for voice, captions, audio and transcript recovery on a dedicated PostgreSQL database.
- The native worker completed the loopback Postman simulation from authenticated polling input through the normal model adapter, durable storage and diary reply. Synthetic inputs covered one write after duplicate delivery and lease expiry, 601-second rejection and delayed retry after a 429.
- Withdrawing audio while the file download was in progress prevented the model call. Withdrawing it after the transcript was stored kept the Telegram channel connected and prevented use of the saved text.
- A long synthetic transcript placed the urgent result in the third bounded fragment. The worker sent emergency guidance and skipped diary extraction.
- The end-to-end worker proof uses synthetic Telegram and Gemini responses. Live providers, the largest audio payload under the combined release workload, and the remaining caption and command compatibility cases are separate release checks.

Run the voice worker test `runs_voice_ingress_transcription_and_cached_diary_processing` against a migrated, seeded disposable PostgreSQL database whose name ends in `_test`. Use the local simulation in `postman/simulations/telegram-voice.sim.yaml` and set `GA_TEST_TELEGRAM_VOICE_ORIGIN` to the loopback routing prefix. The existing Telegram SDK wire capture is in `migration/check_telegram_voice_download.py`.
