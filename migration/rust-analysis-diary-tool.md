# Diary reads during model analysis

The Rust analysis loop can read diary entries through the `events` tool using the stored owner, question revision and conversation. The destination comes from the question's current channel. Telegram reads require the confirmed private pairing. The reader checks the diary category, personal goal revision, and shared conversation context. It applies tracker sharing before returning facts and supplies the stored tracker versions for the next model request.

`check_analysis_diary_tool.py` compared the tool description and three scoped results with the pinned Python tool on disposable TimescaleDB. It verified that a model cannot add a channel or invoke a write through tool arguments. Native checks exercised a foreign owner, removing diary consent, changing the Telegram pairing, forgetting shared context, changing a goal and editing the question. Each case stopped further diary reads.
