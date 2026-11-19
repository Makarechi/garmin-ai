# Local commands from a saved Telegram transcript

A voice transcript recognized as an owner control enters the same durable control queue as its typed form. The control checks the original authenticated delivery, current lease, exact saved transcript and audio consent before it changes owner state. Diary commands remain ordered with the diary inbox. Unrecognized transcript commands receive a local reply after the existing tracker setup and form handlers have had their turn.

The frozen reference processed recorded `/pause`, `/history` and an unknown command locally. The native Linux arm64 worker exercised those cases and a recorded `/resume` with audio consent withdrawn after transcription. It kept questions paused, replied once per delivery and sent zero transcript interpretations. Duplicate ingress retained the same receipt. The synthetic worker passed at a 48 MiB limit with zero swap and zero OOM events. The test uses disposable storage and synthetic Telegram and model outputs.

The remaining Telegram release checks include broader caption and tracker contexts, legacy backlog transfer and an authorized private-bot smoke test.
