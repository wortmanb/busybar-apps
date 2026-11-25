#!/usr/bin/env python3
"""FireBoard probe dashboard using explicit bridge JSON or credential-free demo data."""
import argparse
import copy
import datetime
import email.utils
import json
import ipaddress
import re
import warnings
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

APP = "fireboard"
WHITE, GREEN, AMBER, RED = "#FFFFFFFF", "#00DD88FF", "#FFAA00FF", "#FF4444FF"


def number(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("expected finite number")
    return float(value)


def convert(value, source, target):
    if source not in ("F", "C") or target not in ("F", "C"):
        raise ValueError("units must be F or C")
    value = number(value)
    return value if source == target else (value - 32) * 5 / 9 if target == "C" else value * 9 / 5 + 32


@dataclass
class Probe:
    label: str
    role: str
    temp: Optional[float]
    stale: bool
    targets: list
    baseline: float
    limits: list = field(default_factory=list)


def parse(data, config, now):
    if not isinstance(data, dict) or not isinstance(config, dict):
        raise ValueError("input and config must be JSON objects")
    unit = config.get("unit", "F")
    convert(0, unit, unit)
    age = number(config.get("stale_after", 120))
    if age <= 0:
        raise ValueError("stale_after must be positive")
    channels = data["channels"]
    if not isinstance(channels, list):
        raise ValueError("channels must be a list")
    indexed = {}
    for channel in channels:
        if not isinstance(channel, dict):
            raise ValueError("channel must be an object")
        key = str(channel["id"])
        if key in indexed:
            raise ValueError("duplicate channel id")
        indexed[key] = channel
    probes = []
    if not isinstance(config.get("channels"), dict):
        raise ValueError("config channels must be an object")
    for key, settings in config["channels"].items():
        if not isinstance(settings, dict):
            raise ValueError("channel settings must be an object")
        role = settings.get("role", "food")
        if role not in ("food", "pit", "grate", "ignore"):
            raise ValueError("unknown role")
        if role == "ignore":
            continue
        raw = indexed.get(str(key), {})
        source = raw.get("unit", data.get("unit"))
        temp = raw.get("temperature")
        temp = None if temp is None or raw.get("connected") is False else convert(temp, source, unit)
        stamp = raw.get("updated_at", data.get("updated_at"))
        stale = stamp is None or now - number(stamp) > age or number(stamp) > now + 30
        targets = sorted(set(number(t) for t in settings.get("targets", [])))
        baseline = number(settings.get("baseline", 32 if unit == "F" else 0))
        if targets and targets[0] <= baseline:
            raise ValueError("targets must exceed baseline")
        label = str(settings.get("label", key)).upper()
        if not label or not label.isascii() or not all(c.isalnum() or c == " " for c in label):
            raise ValueError("labels must contain ASCII letters, digits or spaces")
        limits = [(direction, number(settings[name])) for direction, name in
                  (("LO", "lower"), ("HI", "upper")) if settings.get(name) is not None]
        if len(limits) == 2 and limits[0][1] >= limits[1][1]:
            raise ValueError("lower must be less than upper")
        probes.append(Probe(label[:4], role, temp, stale, targets, baseline, limits))
    if not probes or len(probes) > 99:
        raise ValueError("configure 1 to 99 visible channels")
    return probes


def milestone(probe):
    """Next inclusive heating target; equality remains visibly HIT."""
    if probe.temp is None or probe.stale or not probe.targets:
        return None, 0
    previous = probe.baseline
    for target in probe.targets:
        if target >= probe.temp:
            return target, max(0, min(1, (probe.temp - previous) / (target - previous)))
        previous = target
    return None, 1


def proximity(probe, unit):
    if probe.stale or probe.temp is None:
        return "--", 0
    high = [t for direction, t in probe.limits if direction == "HI"]
    if high:
        target, direction = high[0], "HI"
        gap = target - probe.temp
        progress = max(0, min(1, 1 - max(0, gap) / max(1, abs(target - probe.baseline))))
        return (f"{direction} HIT" if gap <= 0 else
                f"{direction} {'<1' if gap < 1 else display_number(gap)}{unit}"), progress
    target, progress = milestone(probe)
    if target is None:
        return ("DONE" if probe.targets else "NO TARGET"), progress
    gap = target - probe.temp
    return ("M HIT" if gap == 0 else f"M +{'<1' if gap < 1 else display_number(gap)}{unit}"), progress


def text(eid, value, x, y, color=WHITE):
    return dict(id=eid, type="text", text=value, x=x, y=y, font="tiny", color=color)


def display_number(value):
    return f"{value:.0f}" if -999 <= value <= 9999 else "OVR"


BLACK, TRACK = "#000000FF", "#223344FF"
# Original seven-pixel sprites, using the same crisp rectangle aesthetic as robots.
SPRITES = {
    "pit": ("...r...", "..rr...", ".rroo..", ".rooor.", "rroyorr", "rroyorr", ".rrrrr."),
    "food": ("..ppp..", ".prrrp.", "prwwrrp", "prwwrrp", "prrrrrp", ".prrrp.", "..ppp.."),
    "grate": (".......", "sssssss", ".s.s.s.", "sssssss", ".s.s.s.", "sssssss", "......."),
}
PALETTE = {".": BLACK, "r": "#EF553FFF", "o": "#FF9900FF",
           "y": "#FFDD55FF", "p": "#FFAA99FF", "w": WHITE, "s": "#88AABBFF"}


def rectangle(eid, x, y, width, height, color):
    return dict(id=eid, type="rectangle", x=x, y=y, width=width, height=height,
                border_width=0, fill="solid", fill_colors=[color])


def elapsed_label(elapsed):
    minutes = max(0, int(elapsed)) // 60
    return f"{minutes // 60:02}:{minutes % 60:02}" if minutes < 6000 else "99h+"


def tile_sources(probes, page):
    pits = [p for p in probes if p.role == "pit"]
    ambient = [p for p in probes if p.role == "grate"] + pits[1:]
    foods = [p for p in probes if p.role == "food"]
    capacity = 2 if ambient else 3
    pages = max(1, (len(foods) + capacity - 1) // capacity)
    start = (page % pages) * capacity
    # Extra ambient sources rotate independently; the primary pit never moves.
    return ([pits[0] if pits else None] +
            ([ambient[page % len(ambient)]] if ambient else []) +
            foods[start:start + capacity])


def render(probes, unit, page, elapsed=0, error=None):
    """Four 18px tiles, fixed IDs/types (including blanks) for retained firmware."""
    sources = tile_sources(probes, page)
    elements = []
    for slot in range(4):
        x, prefix = slot * 18, f"t{slot}"
        probe = sources[slot] if slot < len(sources) else None
        sprite = SPRITES[probe.role] if probe and not error else (".......",) * 7
        unavailable = probe is not None and probe.temp is None
        stale = probe is not None and probe.stale and not unavailable
        color = RED if unavailable or error else AMBER if stale else WHITE
        elements.append(rectangle(prefix + "bg", x, 0, 18, 16, BLACK))
        runs = []
        for y, row in enumerate(sprite):
            dx = 0
            while dx < 7:
                end = dx + 1
                while end < 7 and row[end] == row[dx]:
                    end += 1
                if row[dx] != ".":
                    ink = "#775522FF" if stale else PALETTE[row[dx]]
                    previous = next((i for i, r in enumerate(runs)
                                     if r[0] == x + dx and r[2] == end - dx
                                     and r[4] == ink and r[1] + r[3] == y), None)
                    if previous is not None:
                        rx, ry, rw, rh, _ = runs[previous]
                        runs[previous] = (rx, ry, rw, rh + 1, ink)
                    else:
                        runs.append((x + dx, y, end - dx, 1, ink))
                dx = end
        assert len(runs) <= 19, "sprite exceeds element budget"
        for i in range(19):
            rx, ry, width, height, ink = runs[i] if i < len(runs) else (x, 0, 1, 1, "#00000000")
            elements.append(rectangle(f"{prefix}p{i}", rx, ry, width, height, ink))
        index = str(next((i + 1 for i, p in enumerate(probes) if p is probe), 0))
        badge = (index if len(index) <= 2 else "+") + unit if probe else ""
        value = "--" if unavailable else display_number(probe.temp) if probe else ""
        if error:
            badge, value = "", (error[:18] if slot == 3 else "")
        elif stale:
            badge = index + "!"
        elif probe is None and slot == 3:
            badge, value = "T", elapsed_label(elapsed)
        elements.append(text(prefix + "id", badge, x + 7, 0, color))
        # A 5-glyph timer uses the entire final tile only with a compact font.
        if len(value) > 4 and not error:
            value = value.replace(":", "")  # HHMM; T badge identifies elapsed hours/minutes
        elements.append(text(prefix + "v", value, 0 if error else x, 8, color))
        status, progress = proximity(probe, unit) if probe and not error else ("", 0)
        bar_color = RED if "HIT" in status or status == "DONE" else GREEN
        track = TRACK if probe and not error else BLACK
        width, ink = round(16 * progress), bar_color
        height = 2
        if unavailable or stale:
            width, ink, height = 16, color, 1  # hairline, never a live filled bar
        elif status == "NO TARGET":
            width, ink = 1, TRACK
            track = BLACK
        elements.append(rectangle(prefix + "b0", x, 14, 16, 2, track))
        elements.append(rectangle(prefix + "b1", x, 14, max(1, width), height,
                                  ink if width else track))
    return elements


ENDPOINT = "https://fireboard.io/api/v1/devices.json"


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def normalize(devices, config):
    """Map cloud samples to explicit local aliases; never infer ports or units."""
    if not isinstance(devices, list):
        raise ValueError("invalid device list")
    result = []
    for key, settings in config["channels"].items():
        matches = [d for d in devices if isinstance(d, dict) and d.get("uuid") == settings["device_uuid"]]
        reading = {"id": key, "temperature": None}
        if len(matches) == 1:
            samples = matches[0].get("latest_temps", [])
            samples = samples if isinstance(samples, list) else []
            samples = [s for s in samples if isinstance(s, dict) and type(s.get("channel")) is int
                       and s["channel"] == settings["channel"]]
            # Ambiguous duplicate ports are unavailable, never selected by label/order.
            if len(samples) == 1:
                sample = samples[0]
                unit = {1: "C", 2: "F"}.get(sample.get("degreetype")) if type(sample.get("degreetype")) is int else None
                value = sample.get("temp")
                if unit and type(value) in (int, float) and math.isfinite(value):
                    reading.update(temperature=value, unit=unit, updated_at=timestamp(sample.get("created")))
        result.append(reading)
    return {"channels": result}


class CloudSource:
    """Read-only, paced cloud snapshots. Never logs account data or secrets."""

    def __init__(self, config, clock=time.monotonic):
        self.config, self.clock = config, clock
        self.token = os.environ.get("FIREBOARD_TOKEN")
        if not self.token or any(c.isspace() for c in self.token):
            raise ValueError("FIREBOARD_TOKEN required")
        for settings in config["channels"].values():
            if not isinstance(settings.get("device_uuid"), str) or not settings["device_uuid"]:
                raise ValueError("device_uuid required")
            if type(settings.get("channel")) is not int or settings["channel"] < 1:
                raise ValueError("positive channel number required")
        self.next_poll, self.failures, self.stopped = 0, 0, False
        self.cache = {"channels": []}
        self.status = "STARTING"
        self.opener = urllib.request.build_opener(NoRedirect)

    def read(self):
        now = self.clock()
        if not self.stopped and now >= self.next_poll:
            self.next_poll = now + 30
            request = urllib.request.Request(ENDPOINT, headers={
                "Authorization": "Token " + self.token, "Accept": "application/json",
                "User-Agent": "BusyBar-FireBoard/1.0 (read-only dashboard)"})
            try:
                with self.opener.open(request, timeout=10) as response:
                    body = response.read(1048577)
                if len(body) > 1048576:
                    raise ValueError("response too large")
                self.cache = normalize(json.loads(body), self.config)
                self.failures, self.status = 0, "OK"
            except urllib.error.HTTPError as exc:
                self.status = "OFFLINE"
                if exc.code in (401, 403):
                    self.stopped, self.status = True, "AUTH"
                elif exc.code == 429:
                    self.status = "RATE LIMIT"
                    delay = 300
                    value = exc.headers.get("Retry-After", "") if exc.headers else ""
                    try:
                        delay = max(delay, float(value))
                    except ValueError:
                        try:
                            delay = max(delay, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
                        except (ValueError, TypeError, OverflowError):
                            pass
                    self.next_poll = now + delay
                else:
                    self.backoff(now)
            except (OSError, ValueError, TypeError, KeyError):
                self.status = "OFFLINE"
                self.backoff(now)
        # Blank immediately: no unbounded retention of failed-source samples.
        return copy.deepcopy(self.cache) if self.status == "OK" else {"channels": []}

    def backoff(self, now):
        self.failures += 1
        self.next_poll = now + min(900, 30 * 2 ** min(self.failures, 5))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirects disabled to protect credentials")


class HomeAssistantSource:
    """Only mapped state GETs; timestamps describe HA reporting, not sampling."""

    def __init__(self, config, clock=time.monotonic):
        self.config, self.clock = config, clock
        self.token = os.environ.get("FIREBOARD_HA_TOKEN", "")
        if not self.token or not self.token.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in self.token):
            raise ValueError("FIREBOARD_HA_TOKEN must be a nonempty printable token")
        base = os.environ.get("FIREBOARD_HA_URL", "https://homeassistant.local:8123")
        try:
            url = urllib.parse.urlsplit(base)
            url.port  # Validate malformed ports without including the URL in errors.
            if (url.scheme not in ("https", "http") or not url.hostname or url.username is not None
                    or url.password is not None or "?" in base or "#" in base
                    or url.path not in ("", "/") or any(c.isspace() for c in base)
                    or "\\" in base):
                raise ValueError()
            if url.scheme == "http":
                local = url.hostname == "localhost"
                try:
                    address = ipaddress.ip_address(url.hostname)
                    local = address.is_loopback or address in ipaddress.ip_network("10.0.0.0/8") or address in ipaddress.ip_network("172.16.0.0/12") or address in ipaddress.ip_network("192.168.0.0/16")
                except (ValueError, TypeError):
                    pass
                if not local or os.environ.get("FIREBOARD_HA_INSECURE_LOCAL") != "1":
                    raise ValueError()
                warnings.warn("Home Assistant token will travel over unencrypted local HTTP", RuntimeWarning)
        except ValueError:
            raise ValueError("HA URL requires HTTPS, no credentials/path/query/fragment; local HTTP needs explicit opt-in") from None
        self.base = base.rstrip("/")
        self.interval = number(config.get("ha_poll_interval", 30))
        if self.interval < 5 or self.interval > 3600:
            raise ValueError("ha_poll_interval must be 5 to 3600 seconds")
        if not isinstance(config.get("channels"), dict) or not 1 <= len(config["channels"]) <= 99:
            raise ValueError("configure 1 to 99 HA mappings")
        for settings in config["channels"].values():
            if (not isinstance(settings, dict) or not isinstance(settings.get("entity_id"), str)
                    or not re.fullmatch(r"sensor\.[a-z0-9_]+", settings["entity_id"])):
                raise ValueError("each HA channel requires a sensor entity_id")
        self.opener = urllib.request.build_opener(NoRedirect)
        self.next_poll, self.failures, self.stopped = 0, 0, False
        self.status, self.cache = "STARTING", {"channels": []}

    def read(self):
        now = self.clock()
        if self.stopped or now < self.next_poll:
            return copy.deepcopy(self.cache)
        readings, states, failed, rate_limited = {}, {}, False, False
        retry_after = 300
        self.next_poll = now + self.interval
        for alias, settings in self.config["channels"].items():
            entity = settings["entity_id"]
            reading = {"id": alias, "temperature": None}
            if entity not in states:
                request = urllib.request.Request(self.base + "/api/states/" + entity, headers={
                    "Authorization": "Bearer " + self.token, "Accept": "application/json"})
                try:
                    with self.opener.open(request, timeout=5) as response:
                        body = response.read(65537)
                    if len(body) > 65536:
                        raise ValueError("oversize state")
                    state = json.loads(body)
                    if not isinstance(state, dict) or state.get("entity_id") != entity:
                        raise ValueError("entity mismatch")
                    states[entity] = state
                except urllib.error.HTTPError as exc:
                    if exc.fp is not None:
                        exc.close()
                    failed = True
                    states[entity] = {}
                    if exc.code in (401, 403):
                        self.stopped, self.status, self.cache = True, "AUTH", {"channels": []}
                        return copy.deepcopy(self.cache)
                    if exc.code == 429:
                        try:
                            retry_after = min(3600, max(300, number(float(exc.headers.get("Retry-After", 300)))))
                        except (ValueError, TypeError, AttributeError):
                            pass
                        rate_limited = True
                        break
                except (OSError, ValueError, TypeError):
                    failed, states[entity] = True, {}
            state = states[entity]
            attrs = state.get("attributes")
            attrs = attrs if isinstance(attrs, dict) else {}
            native_unit = attrs.get("unit_of_measurement")
            unit = "C" if native_unit == "°C" else "F" if native_unit == "°F" else None
            try:
                raw = state.get("state")
                if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
                    raise ValueError()
                value = number(float(raw))
                if unit and attrs.get("device_class", "temperature") == "temperature":
                    reading.update(temperature=value, unit=unit)
            except (ValueError, TypeError, OverflowError):
                pass
            # An invalid preferred timestamp remains unknown; never substitute fetch time.
            reading["updated_at"] = timestamp(state.get("last_reported", state.get("last_updated")))
            readings[alias] = reading
        self.cache = {"channels": list(readings.values())}
        if failed:
            self.failures += 1
            self.next_poll = now + max(retry_after if rate_limited else self.interval,
                                       min(900, self.interval * 2 ** min(self.failures, 5)))
            self.status = "RATE LIMIT" if rate_limited else "PARTIAL"
        else:
            self.failures, self.status = 0, "OK"
        return copy.deepcopy(self.cache)


def load_source(source):
    if source.startswith(("https://", "http://")):
        parsed = urllib.parse.urlsplit(source)
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("URL credentials and fragments are not supported")
        token = os.environ.get("FIREBOARD_BRIDGE_TOKEN")
        if token and parsed.scheme != "https":
            raise ValueError("bridge token requires HTTPS")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(source, headers=headers)
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            payload = response.read(1048577)
        if len(payload) > 1048576:
            raise ValueError("bridge response exceeds 1 MiB")
        return json.loads(payload)
    with open(source, encoding="utf-8") as stream:
        return json.load(stream)


def send(host, elements=None):
    base = "http://" + host.removeprefix("http://").rstrip("/")
    path = "/api/display/draw"
    if elements is None:
        path += "?" + urllib.parse.urlencode({"application_name": APP})
    body = {"application_name": APP}
    if elements is not None:
        body["elements"] = elements
    request = urllib.request.Request(base + path,
                                     data=json.dumps(body).encode() if elements is not None else None,
                                     method="POST" if elements is not None else "DELETE",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5):
            pass
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            raise


DEMO_CONFIG = {"unit": "F", "channels": {
    "1": {"label": "BEEF", "targets": [165, 203]},
    "2": {"label": "PORK", "targets": [160, 195, 203]},
    "3": {"label": "PIT", "role": "pit", "lower": 225, "upper": 275},
    "4": {"label": "GRAT", "role": "grate"}}}


def demo(now):
    return {"unit": "F", "updated_at": now, "channels": [
        {"id": str(i + 1), "temperature": temp} for i, temp in enumerate([158, 172, 250, 238])]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="10.0.4.20")
    parser.add_argument("--source", help="normalized JSON file or bridge URL")
    parser.add_argument("--config", help="channel configuration JSON (required with --source)")
    parser.add_argument("--ha", action="store_true", help="Home Assistant REST (env FIREBOARD_HA_TOKEN/URL)")
    parser.add_argument("--cloud", action="store_true", help="direct FireBoard cloud (env FIREBOARD_TOKEN)")
    parser.add_argument("--demo", action="store_true", help="explicit demo (also default without source)")
    parser.add_argument("--test", action="store_true", help="draw one demo frame and exit")
    parser.add_argument("--interval", type=float, default=3, help="page duration, seconds (minimum 1)")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 1:
        parser.error("interval must be finite and >= 1")
    if sum(bool(x) for x in (args.source, args.cloud, args.demo, args.ha)) > 1:
        parser.error("choose only one of --source, --cloud, --demo, --ha")
    if (args.source or args.cloud or args.ha) and not args.config:
        parser.error("--source/--cloud/--ha requires --config")
    config = load_source(args.config) if args.config else DEMO_CONFIG
    cloud = HomeAssistantSource(config) if args.ha else CloudSource(config) if args.cloud else None
    last_status = None
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    page = 0
    started = time.monotonic()
    try:
        while True:
            try:
                now = time.time()
                data = cloud.read() if cloud else load_source(args.source) if args.source else demo(now)
                probes = parse(data, config, now)
                elements = render(probes, config.get("unit", "F"), page, time.monotonic() - started)
                if cloud and cloud.status != last_status:
                    print("Source status: " + cloud.status, file=sys.stderr)
                    last_status = cloud.status
                if cloud and cloud.status not in ("OK", "PARTIAL", "RATE LIMIT"):
                    raise ValueError("cloud unavailable")
            except (OSError, ValueError, KeyError, TypeError):
                # Never display cached readings as current after an ingestion error.
                elements = render([], config.get("unit", "F"), page,
                                  error=cloud.status if cloud and cloud.status != "OK" else "SOURCE ERROR")
            try:
                send(args.host, elements)
            except (OSError, ValueError):
                print("Display unavailable", file=sys.stderr)
            if args.test:
                break
            page += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            send(args.host)
        except OSError:
            pass


if __name__ == "__main__":
    main()
