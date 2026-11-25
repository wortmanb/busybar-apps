import json
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import app
import cloud

CONFIG = {"unit": "F", "channels": {
    "food": {"device_uuid": "device-a", "channel": 1, "label": "FOOD", "upper": 165},
    "pit": {"device_uuid": "device-b", "channel": 1, "label": "PIT", "role": "pit"}}}


def fixture():
    return [{"uuid": uuid, "channels": [{"channel": 1, "channel_label": "duplicate", "last_temp": 900}],
             "latest_temps": [{"channel": 1, "temp": temp, "degreetype": unit,
                               "created": "2026-01-01T00:00:00Z"}]}
            for uuid, temp, unit in [("device-a", 70, 1), ("device-b", 250, 2)]]


class CloudTests(unittest.TestCase):
    def test_normalization(self):
        data = fixture()
        result = cloud.normalize(data, CONFIG)
        probes = app.parse(result, CONFIG, 1767225600)
        self.assertAlmostEqual(probes[0].temp, 158)
        self.assertEqual(probes[1].temp, 250)
        self.assertFalse(probes[0].stale)
        for bad in [None, "", {}, "2026-01-01T00:00:00"]:
            data[0]['latest_temps'][0]['created'] = bad
            self.assertTrue(app.parse(cloud.normalize(data, CONFIG), CONFIG, 1767225600)[0].stale)

    def test_malformed_omitted_duplicate_unknown(self):
        for samples in [None, [], [None], [{"channel": 1, "temp": 50, "degreetype": 0}],
                        [{"channel": 1, "temp": "bad", "degreetype": 2}],
                        fixture()[0]['latest_temps'] * 2]:
            data = fixture()
            data[0]['latest_temps'] = samples
            result = cloud.normalize(data, CONFIG)['channels']
            self.assertIsNone(result[0]['temperature'])
            self.assertEqual(result[1]['temperature'], 250)
        data = fixture()
        data[0]['latest_temps'][0]['temp'] = 0
        self.assertEqual(cloud.normalize(data, CONFIG)['channels'][0]['temperature'], 0)

    @patch.dict(os.environ, {"FIREBOARD_TOKEN": "fixture-secret"})
    @patch('cloud.urllib.request.build_opener')
    def test_cache_auth_and_request(self, build):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(fixture()).encode()
        build.return_value.open.return_value = response
        clock = MagicMock(return_value=0)
        source = cloud.CloudSource(CONFIG, clock)
        source.read()
        source.read()
        self.assertEqual(build.return_value.open.call_count, 1)
        req = build.return_value.open.call_args.args[0]
        self.assertEqual(req.full_url, cloud.ENDPOINT)
        self.assertEqual(req.get_header('Authorization'), 'Token fixture-secret')
        self.assertTrue(req.get_header('User-agent'))
        clock.return_value = 30
        build.return_value.open.side_effect = urllib.error.HTTPError('url', 401, '', {}, None)
        self.assertEqual(source.read(), {'channels': []})
        clock.return_value = 100000
        source.read()
        self.assertEqual(source.status, 'AUTH')
        self.assertEqual(build.return_value.open.call_count, 2)

    @patch.dict(os.environ, {"FIREBOARD_TOKEN": "fixture-secret"})
    @patch('cloud.urllib.request.build_opener')
    def test_throttle_backoff_redirect(self, build):
        for headers, expected in [({}, 300), ({'Retry-After': '600'}, 600),
                                  ({'Retry-After': '10'}, 300)]:
            source = cloud.CloudSource(CONFIG, lambda: 0)
            build.return_value.open.side_effect = urllib.error.HTTPError('url', 429, '', headers, None)
            source.read()
            self.assertEqual(source.next_poll, expected)
            count = build.return_value.open.call_count
            source.read()
            self.assertEqual(build.return_value.open.call_count, count)
        for error in [urllib.error.URLError('offline'), urllib.error.HTTPError('url', 503, '', {}, None)]:
            source = cloud.CloudSource(CONFIG, lambda: 0)
            build.return_value.open.side_effect = error
            source.read()
            self.assertEqual(source.next_poll, 60)
        with self.assertRaises(ValueError):
            cloud.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other')

    def test_proximity(self):
        p = app.Probe('PIT', 'pit', 250, False, [], 32, [('LO', 225), ('HI', 275)])
        self.assertEqual(app.proximity(p, 'F')[0], 'HI 25F')
        for temp, expected in [(225, 'LO HIT'), (200, 'LO HIT'), (275, 'HI HIT'), (300, 'HI HIT'), (274.9, 'HI <1F')]:
            p.temp = temp
            self.assertEqual(app.proximity(p, 'F')[0], expected)
        p.stale = True
        self.assertEqual(app.proximity(p, 'F'), ('--', 0))
        p.stale, p.temp, p.limits, p.targets = False, 165, [], [165, 203]
        self.assertEqual(app.proximity(p, 'F'), ('M HIT', 1))
        p.temp = None
        self.assertEqual(app.proximity(p, 'F'), ('--', 0))
