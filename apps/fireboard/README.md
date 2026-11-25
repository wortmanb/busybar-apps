# FireBoard temperature dashboard

Stdlib-only Python 3.10+ host app for BusyBar's 72×16 display. Implemented:
direct read-only FireBoard Cloud ingestion, normalized JSON bridge/file, and demo.
No account, hardware or deployment is needed for tests or preview.
`app.py` is self-contained: copying/downloading that file alone supports `--help`
and the built-in demo without sibling modules, config files or pip dependencies.
Live modes additionally require your own configuration (and cloud token for cloud).

```sh
/usr/bin/python3 apps/fireboard/app.py --demo --host 127.0.0.1:8080
/usr/bin/python3 apps/fireboard/app.py --source apps/fireboard/sample.json --config apps/fireboard/config.json --host 127.0.0.1:8080
/usr/bin/python3 apps/fireboard/app.py --cloud --config /path/to/private-config.json --host 127.0.0.1:8080
```

Without a source/cloud selection the app uses synthetic demo data. Host defaults
to USB `10.0.4.20`; only select your physical bar when you intend to display there.
`--test` draws once and exits; Ctrl-C/SIGTERM releases display ownership.
`--interval 3` controls page rotation, **not cloud polling**.

## Direct cloud setup

Provision an account token using FireBoard's official supported workflow outside
this app. Supply `FIREBOARD_TOKEN` through the host's environment/secret manager.
Never put tokens/passwords in config, command arguments, URLs, Git, screenshots,
or logs. This app has no login/password storage or automatic token refresh. The
token is an account secret, not a documented read-only scope: this adapter performs
GET only. Restart with a fixed token after AUTH; it deliberately stops retrying.

For discovery, an authorized operator can GET
`https://fireboard.io/api/v1/devices.json` using `Authorization: Token <token>` and
an identifying `User-Agent` in a private API client that reads the token from its
environment. Do not paste raw account responses into issues. Inspect device `uuid`
and `channels[].channel` privately, then configure explicit device/port pairs:

```json
{"unit":"F","stale_after":120,"channels":{
  "food":{"device_uuid":"YOUR-DEVICE-UUID","channel":1,"label":"BEEF","role":"food","upper":203},
  "pit":{"device_uuid":"YOUR-DEVICE-UUID","channel":2,"label":"PIT","role":"pit","lower":225,"upper":275},
  "grate":{"device_uuid":"YOUR-DEVICE-UUID","channel":3,"label":"GRAT","role":"grate"}
}}
```

Use actual separate pit/grate probes, not a Drive setpoint or duplicated reading.
Labels and array positions never identify a source; duplicate labels across devices
are safe. Duplicate matching device UUIDs or ports are unavailable rather than guessed.

The adapter makes one shared fixed-endpoint HTTPS GET every 30 seconds for all
configured channels. It sends an identifying User-Agent, rejects redirects, limits
responses to 1 MiB and times out after 10 seconds. FireBoard documents **17 calls
per five minutes per source IP**; other apps/operators on the same NAT share that
budget. Run only one poller; this cache is per process, not a distributed lock.
429 waits at least five minutes, or longer Retry-After seconds/HTTP-date; transport,
malformed payload and other HTTP failures back off up to 15 minutes. 401/403 stop
requests until restart. Failed polls blank cached readings immediately and show
OFFLINE, AUTH or RATE LIMIT with CHECK INPUT; no raw errors or account metadata are logged.

Only `latest_temps[].channel/temp/degreetype/created` supplies measurements.
Units 1=C, 2=F are recognized; unknown units, malformed/omitted probes and ambiguous
ports become unavailable without hiding other valid probes. No metadata `last_temp`
or `last_templog` is used as a fresh measurement. ISO timestamps must contain a
timezone; missing/unknown/old timestamps remain stale, never reset on cache/render.
No timestamp or unit fallback guesses are made.

Primary docs independently checked for this implementation:
[Cloud API](https://docs.fireboard.io/app/app-api/) (Token auth, required User-Agent,
latest_temps freshness, rate limit) and
[alerts](https://docs.fireboard.io/app/app-alerts/) (timing/buffer/repeat behavior).
Cloud responses and hardware have **not** been tested against a real account.

## Bridge and file contract

```json
{"unit":"F","updated_at":1700000000,"channels":[
  {"id":"1","temperature":158,"connected":true},
  {"id":"2","temperature":null}
]}
```

`updated_at` is Unix seconds for the measurement, not retrieval time. Channels may
override unit/timestamp. Missing probes/null/connected:false mean unavailable;
missing timestamps, age > stale_after (default 120s) or >30 seconds in the future
mean stale. Samples in this repository deliberately demonstrate old data.
Use atomic file replacement. `FIREBOARD_BRIDGE_TOKEN` is optional Bearer auth,
environment only, requires HTTPS. Redirects are rejected and URL responses limited
to 1 MiB. Bridge/file input is read every page, not cloud-cached; choose an interval
appropriate to your bridge. Failed reads show SOURCE ERROR rather than cached data.

## Thresholds and display

`channels` maps bridge IDs (or local cloud aliases) to ASCII labels (first four
characters), role food/pit/grate/ignore, optional `lower`/`upper`, optional heating
`targets`, and `baseline` (default 32 F / 0 C). All config values are in display
`unit` F/C; convert config thresholds/baseline if changing units. Targets must be
above baseline and are sorted/deduplicated; lower must be below upper.

**Thresholds are explicitly local, manually configured limits, not downloaded
FireBoard alerts.** Mirror desired current thresholds manually into lower/upper.
Automatic cloud alert discovery is intentionally not implemented: enabled state,
units, session applicability, schedule and buffer behavior cannot safely be proven
from arbitrary historic alert metadata. Keep the official app as alerting authority.
No alert writes, notifications, ETA prediction, BLE or direct device control.

The default view is four 18-pixel tiles: original 7×7 flame (pit) anchored at
x=0, a distinct grate icon at x=18 when configured, then steak icons for food.
Each tile has a small **1-based visible config-order index plus F/C** beside the
icon, numeric temperature beneath, and a 16×2 proximity bar. Index is not a cloud
port: use config order to identify probes, including across food pages. Configuration accepts
at most 99 visible channels for unambiguous two-digit indices. Missing pit leaves
the left slot empty rather than relabeling food as pit. Numbers round to integer;
-999 through 9999 fit, otherwise OVR. No long labels obscure the icons.

Only food slots paginate (two per page with grate, three without), every
`--interval` seconds, wrapping in config order. Pit never moves. Multiple grates
or additional pit sources rotate independently through the distinct ambient slot;
none are silently dropped. Their indices and icons identify each measured source.
An otherwise unused far-right tile shows T and compact **HHMM** elapsed app-tracking
time (e.g. 0105 = 1h05m). It uses monotonic time since process tracking began,
resets on restart, and caps at `99h+` after 99:59. This is **not a cloud cook-session
timer**. It is omitted on full pages to prioritize probe readability.

Bars always mean **high-temperature proximity**, never nearest/low boundary.
An explicit `upper` takes precedence; `lower` is retained configuration but is not
visualized as a heating goal. Without upper, the next inclusive heating target is
used: equality holds that milestone rather than silently advancing. Between targets
the progress baseline is the previous target; for upper it is configured baseline.
Progress is clamped 0–1. Green fill approaches the high goal; a full **red** bar
marks equality/crossing (or completion above the final target). After crossing an
intermediate milestone the next one becomes the goal. Endpoints only mean no high
target; no notifications are sent or implied.

Stale numeric values are amber with a dim icon and index+`!` badge; unavailable
values are red `--`. Both replace the filled bar with a thin colored hairline,
never stale progress. Source errors clear all tile elements and display the error
instead. All 96 element IDs/types are reused across pages, blanks and errors to
avoid retained-element ghosts and stay below firmware's 100-element limit.
This is a visual monitor, not a safety alarm or claim that a notification fired.

## Verification

```sh
/usr/bin/python3 -m unittest discover -s apps/fireboard -v
PATH=/usr/bin:/bin:$PATH npm run preview -- fireboard --png -- --demo
PATH=/usr/bin:/bin:$PATH npm run check -- fireboard --run
npm run build
```

Cloud transport and payload tests use synthetic fixtures and mocks only. The
single-file regression copies only app.py into a temporary directory, runs isolated
Python subprocesses for help/default/demo (F and C) and file mode against a local
HTTP display stub, and checks display cleanup. Cloud CLI mode is mocked; cache
aging and HTTP-date Retry-After also have regression coverage. Preview uses the
repository emulator/recorder; no real account/hardware/deployment tested.
