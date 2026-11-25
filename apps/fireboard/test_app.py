import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch, MagicMock

import app


class Tests(unittest.TestCase):
    def probes(self, data=None, config=None):
        return app.parse(app.demo(1000) if data is None else data, config or app.DEMO_CONFIG, 1000)

    def test_conversion(self):
        self.assertAlmostEqual(app.convert(212, 'F', 'C'), 100)
        self.assertAlmostEqual(app.convert(0, 'C', 'F'), 32)
        for bad in [True, float('nan'), float('inf'), '32']:
            with self.assertRaises(ValueError):
                app.convert(bad, 'F', 'C')

    def test_milestones(self):
        p = self.probes()[0]
        self.assertEqual(app.milestone(p)[0], 165)
        p.temp = 165
        self.assertEqual(app.milestone(p), (165, 1))
        p.temp = 184
        self.assertEqual(app.milestone(p), (203, .5))
        p.temp = 203
        self.assertEqual(app.milestone(p), (203, 1))
        p.targets = []
        self.assertEqual(app.milestone(p), (None, 0))

    def test_missing_stale(self):
        data = app.demo(1)
        self.assertTrue(self.probes(data)[0].stale)
        data = app.demo(1000)
        data['channels'][0]['temperature'] = None
        self.assertIsNone(self.probes(data)[0].temp)
        data['channels'][1]['connected'] = False
        self.assertIsNone(self.probes(data)[1].temp)
        data['channels'] = []
        self.assertIsNone(self.probes(data)[0].temp)
        del data['updated_at']
        self.assertTrue(self.probes(data)[0].stale)

    def test_units_and_timestamp_override(self):
        data = app.demo(1)
        data['channels'][0].update(unit='C', temperature=70, updated_at=1000)
        p = self.probes(data)[0]
        self.assertAlmostEqual(p.temp, 158)
        self.assertFalse(p.stale)
        data['channels'][0]['updated_at'] = 2000
        self.assertTrue(self.probes(data)[0].stale)

    def test_invalid_inputs(self):
        for data in [{}, {'channels': None}, {'channels': [{'id': 1}, {'id': 1}]}]:
            with self.assertRaises((ValueError, TypeError, KeyError)):
                self.probes(data)

    def test_render_pages(self):
        probes = self.probes()
        for page in range(20):
            elements = app.render(probes, 'F', page)
            self.assertEqual(len({e['id'] for e in elements}), len(elements))
            for e in elements:
                self.assertTrue(0 <= e['x'] < 72 and 0 <= e['y'] < 16)
                if e['type'] == 'text':
                    self.assertLessEqual(len(e['text']), 18)
        self.assertEqual(app.tile_sources(probes, 0)[0].role, 'pit')
        self.assertEqual(app.tile_sources(probes, 0)[1].role, 'grate')

    @patch('app.urllib.request.urlopen')
    def test_http_display(self, call):
        app.send('127.0.0.1:8080', [])
        req = call.call_args.args[0]
        self.assertEqual(json.loads(req.data)['application_name'], 'fireboard')
        self.assertEqual(req.method, 'POST')
        app.send('127.0.0.1:8080')
        self.assertEqual(call.call_args.args[0].method, 'DELETE')
        call.side_effect = urllib.error.HTTPError('url', 409, 'busy', {}, None)
        app.send('localhost', [])
        call.side_effect = urllib.error.HTTPError('url', 500, 'error', {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            app.send('localhost', [])

    @patch.dict(os.environ, {'FIREBOARD_BRIDGE_TOKEN': 'test-token'})
    @patch('app.urllib.request.build_opener')
    def test_bridge(self, build):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"channels": []}'
        build.return_value.open.return_value = response
        self.assertEqual(app.load_source('https://example.invalid/data'), {'channels': []})
        req = build.return_value.open.call_args.args[0]
        self.assertEqual(req.get_header('Authorization'), 'Bearer test-token')
        with self.assertRaises(ValueError):
            app.load_source('http://example.invalid/data')
        with self.assertRaises(ValueError):
            app.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other')

    @patch('app.urllib.request.build_opener')
    def test_bridge_errors(self, build):
        response = MagicMock()
        build.return_value.open.return_value = response
        response.__enter__.return_value.read.return_value = b'x' * 1048577
        with self.assertRaises(ValueError):
            app.load_source('https://example.invalid/data')
        build.return_value.open.side_effect = urllib.error.URLError('offline')
        with self.assertRaises(urllib.error.URLError):
            app.load_source('https://example.invalid/data')

    @patch('app.send')
    @patch('app.load_source', side_effect=[app.DEMO_CONFIG, ValueError('bad json')])
    @patch('sys.argv', ['app.py', '--source', 'input.json', '--config', 'config.json', '--test'])
    def test_source_error_and_cleanup(self, load, send):
        app.main()
        self.assertIn('SOURCE ERROR', str(send.call_args_list[0]))
        self.assertEqual(len(send.call_args_list[0].args[1]), len(app.render([], 'F', 0)))
        self.assertEqual(send.call_args_list[-1].args, ('10.0.4.20',))

    def test_odd_overview_and_overflow(self):
        probes = self.probes()[:1]
        probes[0].temp = 1e100
        elements = app.render(probes, 'F', 0)
        self.assertEqual(len({e['id'] for e in elements}), len(elements))
        self.assertIn('OVR', str(elements))

    @patch('builtins.open', return_value=io.StringIO('{"channels": []}'))
    def test_file(self, _):
        self.assertEqual(app.load_source('input.json'), {'channels': []})


if __name__ == '__main__':
    unittest.main()
