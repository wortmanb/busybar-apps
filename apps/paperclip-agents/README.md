# Paperclip Agents

A standard-library-only BUSY Bar dashboard for agent activity, inbox counts,
task mix, and spending. Run `python3 app.py --help` for connection and service
options, or `python3 app.py --demo` for scripted data.

## Display ownership

- Starts without taking the display if no agent is `running` (including an empty
  company, or an unavailable Paperclip server). Inbox items, paused/error agents,
  and in-progress task counts alone do not acquire it.
- A fresh successful poll with a running agent acquires the display automatically.
- When a poll first reports no running agents, a **600-second grace period**
  begins. The dashboard continues rendering during that period. New running
  activity cancels the countdown; a later idle transition starts a full grace.
- At the grace boundary, the app calls `BusyBar.clear()`:
  `DELETE /api/display/draw?application_name=paperclip-agents`. It does not send
  blank frames or clear other applications. Once released, no further draw or
  clear requests are sent until activity resumes.
- Paperclip polling continues while released (`--poll`, default 15 seconds), so
  resumed activity reacquires the display on the next successful poll.
- Failed polls start inactivity rather than renewing a cached running state.
  Without further polls, running data expires after the existing 120-second
  freshness window, followed by the grace period. An already-stale snapshot
  cannot activate the display. Inactivity, freshness elapsed time, and poll
  scheduling use a monotonic clock, independent of wall-clock adjustments.
- `--once` / `--test` polls once and exits successfully without drawing when
  idle/unavailable. When active it draws one frame and exits as before (no
  immediate clear; elements retain their existing 30-second dead-man timeout).
- Device token errors remain fatal. Priority conflicts keep waiting in normal
  mode and fail in `--once` mode. `--wait-for-bar` retains backoff retries for
  unreachable devices, including an unsuccessful release. No remote deployment
  is needed to run the tests.

The macOS LaunchAgent uses the same behavior and continues running while idle.
See the service setup and secret-handling notes in `app.py`'s module docstring.
An already-installed service must be restarted to load code updates.

## Regression tests

From the repository root, with pytest installed:

```sh
python3 -m pytest apps/paperclip-agents/tests -q
```

Or in an ephemeral environment using uv:

```sh
uv run --with pytest python -m pytest apps/paperclip-agents/tests -q
```

Tests use fake clocks and mock both HTTP clients: no hardware, network service,
service installation, or credentials are required.
