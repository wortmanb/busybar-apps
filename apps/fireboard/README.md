# FireBoard temperature dashboard

Stdlib-only Python 3.10+ host app for BusyBar's 72×16 display. Implemented:
direct read-only FireBoard Cloud ingestion, normalized JSON bridge/file, and demo.
No account, hardware or deployment is needed for tests or preview.

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
SOURCE ERROR/CHECK INPUT; no raw errors or account metadata are logged.

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

Overview pages show two temperatures and role F/P/G (or OLD/OFF). Detail pages
show current temperature, gap in degrees and a proximity graph for that probe.
`LO` = configured lower limit (reading minus limit); `HI` = configured upper limit
(limit minus reading). Nearest boundary wins, breached boundary takes precedence;
at or beyond either boundary **HIT** is inclusive. If limits exist, they take display
precedence over heating milestones. `M +7F` means seven degrees to the next heating
milestone; `M HIT` holds at exact equality instead of silently advancing. Above the
last milestone shows DONE. Positive gaps under one degree show `<1`, not HIT.
Bar progress approaches the boundary relative to baseline; it is not a time graph
or notification countdown. No limits/milestones shows NO TARGET; stale/unavailable
readings have no gap/progress. Pages rotate through overview pairs and every probe,
including separately labeled pit/grate pages. Ambient values are not pinned.
This is a visual monitor, not a safety alarm or claim that a notification fired.

## Verification

```sh
/usr/bin/python3 -m unittest discover -s apps/fireboard -v
PATH=/usr/bin:/bin:$PATH npm run preview -- fireboard --png -- --demo
PATH=/usr/bin:/bin:$PATH npm run check -- fireboard --run
npm run build
```

Cloud transport and payload tests use synthetic fixtures and mocks only. Preview
uses the repository emulator/recorder; no real account/hardware/deployment tested.
