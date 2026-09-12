# GA-29: data dashboard, first delivery

Open `/dashboard` on the existing API origin. The initial view explicitly shows synthetic demonstration data. Connect using an existing instance token with `read:health` and/or `read:diary`. Missing scopes are shown per section. Authentication, event storage and export permission checks use the existing API.

The date range is 1–31 inclusive days in UTC and applies to the diary; source freshness describes the current state. Daily summaries retain their calendar semantics. Unknown coverage, parser errors and failed downloads remain distinct. The first 500 diary rows are shown with an explicit truncation notice; narrow the period to inspect the remainder. JSON export includes all event types for the selected period, regardless of the on-screen type filter.

The token is held only in the tab's closure, never browser storage or URLs. Disconnect aborts pending requests and clears rendered records. Authenticated responses and dashboard assets use `Cache-Control: no-store`. All data is rendered as text, under a same-origin CSP. This does not provide a new account system or Internet deployment.

Validation uses disposable PostgreSQL data and the in-app browser: authentication, disconnect, type filtering, empty state, and responsive layout at 1440 and 390 CSS pixels. No real Garmin, Telegram, or personal health data is used.

## Visual reference and fidelity

The implementation follows the generated concept: white canvas and dark typography; restrained teal actions; a horizontal header; a prominent title and date filters; a flat source table above the diary. It uses the project's FastAPI packaging with static CSS/JavaScript, without a separate frontend service.

Copy differences reflect the implemented contracts: source freshness is labelled current; sleep is a calendar summary rather than an invented timestamp; the diary source row in the reference is replaced by an explicit diary section; confirmation status is visible; export scope and truncation are explained. Mobile filters stack and tables scroll within the page. Interactive forms support keyboard focus and live status messages.

Further GA-29 work includes richer time-series and historical coverage views, plus review/edit interactions. This delivery is read-only.
