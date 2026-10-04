# Interactive synthetic demo

Run from the repository root with Python 3.13 or newer:

```sh
python3 scripts/serve_demo.py
```

Open `http://127.0.0.1:8765/dashboard`. The server binds to `127.0.0.1` and serves only the dashboard page, JavaScript and CSS. It exposes no personal API, accepts no writes and uses no database, Garmin account, Telegram bot or model key. Its content security policy blocks network connections from the page. No package installation is needed for this demo server.

Try this sequence:

1. The example starts with fictional coffee, wellbeing and focus records. The sleep row shows an unknown freshness state, and the missing days remain unknown.
2. Expand **Создать трекер** and add at least two fields, such as a numeric amount and a choice. Preview, then enable the tracker. In demo mode this changes memory in the current tab only; reminders and integrations are not enabled.
3. Use the new tracker button to add a fictional record. The diary and **Анализ примера** update immediately. The summary is a deterministic calculation from confirmed records in the selected date range, not an AI response or health conclusion.
4. Select **Исправить** on the new row, change its number and save. The row and calculation update in place.
5. Select **Сбросить демо** or reload the tab to return to the original fictional profile. Exported JSON is marked `synthetic: true` and includes the current demo records only.

Use fictional input. The starter profile, generated tracker definitions and edits are held in a separate in-memory demo state; they never enter the personal database. The regular `/dashboard` page on a configured API instance keeps personal operations behind authentication and shows the demo before connection. Switching from personal mode to demo discards the page's token and loaded personal records.

This demo shows the interaction, not the real AI, Garmin synchronization, Telegram delivery, server-side tracker validation or durable storage. The browser acceptance test in `tests/test_dashboard_demo_browser.py` clicks through creation, correction, recalculation, reset, reload and mobile width, and checks that the standalone demo requests only its static files.
