# Local web chat

After starting a configured instance, open `http://127.0.0.1:8080/chat` (or
your configured loopback API port). Paste the private owner API key from `.env`
into the page. The key stays in the tab's memory and is erased when you
disconnect or close the tab. Other scoped API tokens cannot open this chat.

The chat uses the same owner diary and analysis services as Telegram. It has a
separate pending clarification and recent-analysis context, so a reply in one
channel is not treated as an answer to an unfinished question in the other.
Verified facts appear in the same diary on both interfaces. Sensitive custom
tracker facts require separate sharing consent for destination `web:local`;
without it they are excluded from the chat history and model context.

Commands that work without a model:

- `/note text` saves a dated, confirmed note. Use the dashboard to review or
  correct it. An urgent symptom message is intercepted before recording.
- `/history` lists the five latest permitted record types and times. Full
  details stay in the authenticated diary.
- `/pause` and `/resume` control proactive messages across the single-owner
  installation. `/cancel` clears this chat's pending clarification.
- `/help` shows these commands.

When a model is explicitly configured, selected in onboarding, and covered by
current health and diary consent, ordinary text uses the existing agent for
recording, clarification and bounded analysis. The server validates proposed
changes and calculations before saving or presenting them. If the model is
unavailable or its output fails validation, the chat gives a safe notice and
the diary is not changed. Custom tracker forms remain in `/dashboard`.

The page polls while its tab is visible. It marks a reply read only after
rendering that reply in the open tab. A closed browser receives no push
notification, and queued replies are not claimed as delivered. Repeating a
send with the same client message ID returns the existing result; reusing an
ID for different text is rejected. The page renders message text as plain
text, not HTML. This is a local text channel: it does not support voice,
attachments, Telegram-style buttons or background push.

The browser test uses a real local HTTP server and synthetic messages:

```sh
GA_TEST_DATABASE_URL='postgresql+psycopg://garmin:TEST_PASSWORD@127.0.0.1:TEST_PORT/garmin_ai_test' \
  uv run pytest -q tests/test_web_chat.py tests/test_web_chat_browser.py
```
