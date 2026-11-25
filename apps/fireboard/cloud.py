"""Read-only FireBoard Cloud snapshot adapter. Never logs account data or secrets."""
import copy
import datetime
import email.utils
import json
import os
import time
import urllib.error
import urllib.request

ENDPOINT = "https://fireboard.io/api/v1/devices.json"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("redirect disabled")


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def normalize(devices, config):
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
                if unit and type(value) in (int, float):
                    import math
                    if math.isfinite(value):
                        reading.update(temperature=value, unit=unit, updated_at=timestamp(sample.get("created")))
        result.append(reading)
    return {"channels": result}


class CloudSource:
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
        result = copy.deepcopy(self.cache)
        if self.status != "OK":
            # Blank immediately: no unbounded retention of failed-source samples.
            result = {"channels": []}
        return result

    def backoff(self, now):
        self.failures += 1
        self.next_poll = now + min(900, 30 * 2 ** min(self.failures, 5))
