import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import http.server
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import app

CONFIG = {"unit": "F", "stale_after": 120, "channels": {
    "food": {"entity_id": "sensor.food", "upper": 165},
    "pit": {"entity_id": "sensor.pit", "role": "pit"}}}
NOW = 1767225600


def state(entity="sensor.food", value="0", unit="°C", **extra):
    return dict(entity_id=entity, state=value, attributes={"unit_of_measurement": unit},
                last_updated="2026-01-01T00:00:00Z", **extra)


@patch.dict(os.environ, {"FIREBOARD_HA_TOKEN": "fixture-token", "FIREBOARD_HA_URL": "https://ha.example"})
class HATests(unittest.TestCase):
    def source(self, responses=None):
        source = app.HomeAssistantSource(copy.deepcopy(CONFIG), MagicMock(return_value=0))
        def respond(request, timeout):
            value = (responses or {}).get(request.full_url.rsplit('/', 1)[-1], state("sensor.pit", "250", "°F"))
            if isinstance(value, Exception):
                raise value
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(value).encode()
            return response
        source.opener = MagicMock()
        source.opener.open.side_effect = respond
        return source

    def test_multiple_units_zero_cache_and_timestamp_aging(self):
        source = self.source({"sensor.food": state(last_changed="2000-01-01T00:00:00Z")})
        data = source.read()
        probes = app.parse(data, CONFIG, NOW)
        self.assertEqual([p.temp for p in probes], [32, 250])
        self.assertFalse(any(p.stale for p in probes))
        self.assertEqual(source.read(), data)
        self.assertEqual(source.opener.open.call_count, 2)
        for call in source.opener.open.call_args_list:
            req = call.args[0]
            self.assertIn(req.full_url, ["https://ha.example/api/states/sensor.food", "https://ha.example/api/states/sensor.pit"])
            self.assertEqual(req.get_method(), "GET")
            self.assertEqual(req.get_header("Authorization"), "Bearer fixture-token")
        source.clock.return_value = 30
        self.assertTrue(app.parse(source.read(), CONFIG, NOW + 121)[0].stale)
        self.assertEqual(source.opener.open.call_count, 4)

    def test_bad_values_and_units_are_isolated(self):
        for value in ["unknown", "unavailable", None, "nan", "inf", {}, True, "bad"]:
            source = self.source({"sensor.food": state(value=value)})
            probes = app.parse(source.read(), CONFIG, NOW)
            self.assertIsNone(probes[0].temp)
            self.assertEqual(probes[1].temp, 250)
        for unit in [None, "W", "C"]:
            self.assertIsNone(app.parse(self.source({"sensor.food": state(unit=unit)}).read(), CONFIG, NOW)[0].temp)
        bad = state()
        bad['attributes']['device_class'] = 'power'
        self.assertIsNone(app.parse(self.source({"sensor.food": bad}).read(), CONFIG, NOW)[0].temp)

    def test_timestamps(self):
        for stamp in [None, "bad", "2020-01-01T00:00:00Z", "2026-01-01T00:01:00Z"]:
            data = state(last_reported=stamp)
            self.assertTrue(app.parse(self.source({"sensor.food": data}).read(), CONFIG, NOW)[0].stale)
        data = state(last_reported="2026-01-01T00:00:00Z")
        data['last_updated'] = "2000-01-01T00:00:00Z"
        self.assertFalse(app.parse(self.source({"sensor.food": data}).read(), CONFIG, NOW)[0].stale)
        del data['last_reported']
        del data['last_updated']
        self.assertTrue(app.parse(self.source({"sensor.food": data}).read(), CONFIG, NOW)[0].stale)

    def test_errors_mismatch_auth_stop_and_backoff(self):
        for bad in [state("sensor.other"), [], None, urllib.error.URLError("secret must not be printed"),
                    urllib.error.HTTPError('url', 404, '', {}, None)]:
            source = self.source({"sensor.food": bad})
            probes = app.parse(source.read(), CONFIG, NOW)
            self.assertIsNone(probes[0].temp)
            self.assertEqual(probes[1].temp, 250)
            self.assertEqual(source.status, "PARTIAL")
            self.assertEqual(source.next_poll, 60)
        for code in [401, 403, 429]:
            source = self.source({"sensor.food": urllib.error.HTTPError('url', code, '', {}, None)})
            source.read()
            count = source.opener.open.call_count
            source.read()
            self.assertEqual(source.opener.open.call_count, count)
            if code != 429:
                source.clock.return_value = 99999
                self.assertEqual(source.read(), {"channels": []})
                self.assertEqual(source.status, "AUTH")
                self.assertEqual(source.opener.open.call_count, count)
            else:
                self.assertGreaterEqual(source.next_poll, 300)

    def test_mapping_and_url_token_safety(self):
        for entity in ["sensor.x/../y", "sensor.x?token=z", "light.foo", "sensor.X", "sensor.x\n", "", None]:
            config = copy.deepcopy(CONFIG)
            config['channels']['food']['entity_id'] = entity
            with self.assertRaises((ValueError, TypeError)):
                app.HomeAssistantSource(config)
        for url in ["http://ha.example", "https://u:p@ha.example", "https://ha.example?token=x", "https://ha.example#x", "https://ha.example/api", "https://ha.example:bad", "http://127.0.0.1"]:
            with patch.dict(os.environ, {"FIREBOARD_HA_URL": url}):
                with self.assertRaises(ValueError):
                    app.HomeAssistantSource(CONFIG)
        with patch.dict(os.environ, {"FIREBOARD_HA_URL": "http://127.0.0.1:8123", "FIREBOARD_HA_INSECURE_LOCAL": "1"}):
            with self.assertWarns(RuntimeWarning):
                app.HomeAssistantSource(CONFIG)
        for token in ["", "x\ny", "x y", "é"]:
            with patch.dict(os.environ, {"FIREBOARD_HA_TOKEN": token}):
                with self.assertRaises(ValueError):
                    app.HomeAssistantSource(CONFIG)
        with self.assertRaises(ValueError):
            app.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example')

    def test_bounded_response(self):
        source = self.source()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'x' * 65537
        source.opener.open.side_effect = None
        source.opener.open.return_value = response
        self.assertTrue(all(p.temp is None for p in app.parse(source.read(), CONFIG, NOW)))
        response.__enter__.return_value.read.assert_called_with(65537)

    def test_real_local_redirect_is_not_followed(self):
        paths = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/must-not-follow')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {'FIREBOARD_HA_URL': 'http://127.0.0.1:' + str(server.server_port),
                                         'FIREBOARD_HA_INSECURE_LOCAL': '1'}):
                with self.assertWarns(RuntimeWarning):
                    source = app.HomeAssistantSource(CONFIG)
                self.assertTrue(all(p.temp is None for p in app.parse(source.read(), CONFIG, NOW)))
                self.assertEqual(paths, ['/api/states/sensor.food', '/api/states/sensor.pit'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_isolated_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'app.py'
            target.write_bytes(Path(app.__file__).read_bytes())
            for mode in ['--cloud', '--source=x', '--demo']:
                result = subprocess.run([sys.executable, str(target), '--ha', mode], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn('fixture-token', result.stderr)
            # Run HA CLI with only app.py present; mocked transport and display.
            (Path(directory) / 'config.json').write_text(json.dumps(CONFIG))
            code = '''import app, json, sys
from unittest.mock import MagicMock, patch
response = MagicMock()
response.__enter__.return_value.read.return_value = json.dumps(%s).encode()
opener = MagicMock()
opener.open.return_value = response
sys.argv = ['app.py', '--ha', '--config', 'config.json', '--test']
with patch('app.urllib.request.build_opener', return_value=opener), patch('app.send') as send:
    app.main()
    assert opener.open.call_count == 2
    assert send.call_count == 2
    assert len(send.call_args_list[0].args[1]) == 96
    assert len(send.call_args_list[-1].args) == 1
''' % repr(state())
            result = subprocess.run([sys.executable, '-c', code], cwd=directory, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
