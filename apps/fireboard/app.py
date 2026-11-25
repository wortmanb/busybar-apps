#!/usr/bin/env python3
"""FireBoard probe dashboard using explicit bridge JSON or credential-free demo data."""
import argparse
import json
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

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
    temp: float | None
    stale: bool
    targets: list
    baseline: float


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
        probes.append(Probe(label[:4], role, temp, stale, targets, baseline))
    if not probes:
        raise ValueError("configure at least one visible channel")
    return probes


def milestone(probe):
    """Next strictly higher heating target; progress within current milestone interval."""
    if probe.temp is None or probe.stale or not probe.targets:
        return None, 0
    previous = probe.baseline
    for target in probe.targets:
        if target > probe.temp:
            return target, max(0, min(1, (probe.temp - previous) / (target - previous)))
        previous = target
    return None, 1


def text(eid, value, x, y, color=WHITE):
    return dict(id=eid, type="text", text=value, x=x, y=y, font="tiny", color=color)


def display_number(value):
    return f"{value:.0f}" if -999 <= value <= 9999 else "OVR"


def render(probes, unit, page):
    # Overview pairs followed by one detail page per probe. All text <=18 tiny glyphs.
    overview_count = (len(probes) + 1) // 2
    page %= overview_count + len(probes)
    elements = []
    if page < overview_count:
        for i, probe in enumerate(probes[page * 2:page * 2 + 2]):
            value = "--" if probe.temp is None else f"{display_number(probe.temp)}{unit}"
            flag = "OLD" if probe.stale else "OFF" if probe.temp is None else probe.role[:1].upper()
            elements.append(text(str(i), f"{probe.label} {value} {flag}", 0, i * 8,
                                 AMBER if probe.stale else RED if probe.temp is None else WHITE))
    else:
        probe = probes[page - overview_count]
        target, progress = milestone(probe)
        value = "--" if probe.temp is None else f"{display_number(probe.temp)}{unit}"
        elements.append(text("0", f"{probe.label} {value}", 0, 0))
        status = "STALE" if probe.stale else "DISCONNECTED" if probe.temp is None else (
            f"NEXT {display_number(target)}{unit}" if target is not None else "DONE" if probe.targets else "NO TARGET")
        elements.append(text("1", status, 0, 7, AMBER if probe.stale else GREEN))
        elements.append(dict(id="2", type="rectangle", x=0, y=14, width=72, height=2,
                             border_width=0, fill="solid", fill_colors=["#223344FF"]))
        elements.append(dict(id="3", type="rectangle", x=0, y=14, width=max(1, round(72 * progress)),
                             height=2, border_width=0, fill="solid",
                             fill_colors=[GREEN if progress else "#223344FF"]))
    if page < overview_count:
        if len(elements) == 1:
            elements.append(text("1", "", 0, 8))
        # Firmware retains element IDs between draws: erase previous detail bars.
        for eid in ("2", "3"):
            elements.append(dict(id=eid, type="rectangle", x=0, y=14,
                                 width=72, height=2, border_width=0,
                                 fill="solid", fill_colors=["#000000FF"]))
    return elements


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirects disabled to protect bridge credentials")


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
    "3": {"label": "PIT", "role": "pit"},
    "4": {"label": "GRAT", "role": "grate"}}}


def demo(now):
    return {"unit": "F", "updated_at": now, "channels": [
        {"id": str(i + 1), "temperature": temp} for i, temp in enumerate([158, 172, 250, 238])]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="10.0.4.20")
    parser.add_argument("--source", help="normalized JSON file or bridge URL")
    parser.add_argument("--config", help="channel configuration JSON (required with --source)")
    parser.add_argument("--demo", action="store_true", help="explicit demo (also default without source)")
    parser.add_argument("--test", action="store_true", help="draw one demo frame and exit")
    parser.add_argument("--interval", type=float, default=3, help="page duration, seconds (minimum 1)")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < 1:
        parser.error("interval must be finite and >= 1")
    if args.source and (not args.config or args.demo):
        parser.error("--source requires --config and cannot combine with --demo")
    config = load_source(args.config) if args.config else DEMO_CONFIG
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    page = 0
    try:
        while True:
            try:
                now = time.time()
                probes = parse(load_source(args.source) if args.source else demo(now), config, now)
                elements = render(probes, config.get("unit", "F"), page)
            except (OSError, ValueError, KeyError, TypeError):
                # Never display cached readings as current after an ingestion error.
                elements = [text("0", "SOURCE ERROR", 0, 0, RED), text("1", "CHECK INPUT", 0, 8, AMBER)]
                for eid in ("2", "3"):
                    elements.append(dict(id=eid, type="rectangle", x=0, y=14,
                                         width=72, height=2, border_width=0,
                                         fill="solid", fill_colors=["#000000FF"]))
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
