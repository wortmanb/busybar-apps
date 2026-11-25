"""Exercise the gallery download in isolation, with no device or account."""
import copy
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import app


class StandaloneTests(unittest.TestCase):
    def test_single_file_cli_all_modes(self):
        requests = []

        class Display(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.command, self.path, json.loads(
                    self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200)
                self.end_headers()

            def do_DELETE(self):
                requests.append((self.command, self.path, None))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        with tempfile.TemporaryDirectory() as directory, \
                http.server.ThreadingHTTPServer(('127.0.0.1', 0), Display) as server:
            target = Path(directory) / 'app.py'
            shutil.copyfile(Path(app.__file__), target)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['app.py'])
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            env = {k: v for k, v in os.environ.items() if not k.startswith(('PYTHON', 'FIREBOARD'))}

            def run(*args):
                result = subprocess.run(['/usr/bin/python3', '-I', str(target), *args],
                                        cwd=directory, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result

            host = f'127.0.0.1:{server.server_port}'
            try:
                self.assertIn('--cloud', run('--help').stdout)
                self.assertEqual(requests, [])
                for args in [[], ['--demo']]:
                    run(*args, '--test', '--host', host)
                    self.assertIn("'text': '158'", str(requests[-2]))
                    self.assertIn("'text': '1F'", str(requests[-2]))
                    self.assertEqual(requests[-1][0], 'DELETE')
                config = copy.deepcopy(app.DEMO_CONFIG)
                config['unit'] = 'C'
                # Optional demo config and normalized file mode, both in Celsius.
                config_path = Path(directory) / 'config.json'
                for settings in config['channels'].values():
                    for key in ('lower', 'upper'):
                        if key in settings:
                            settings[key] = app.convert(settings[key], 'F', 'C')
                    settings['targets'] = [app.convert(t, 'F', 'C') for t in settings.get('targets', [])]
                config_path.write_text(json.dumps(config))
                run('--demo', '--config', str(config_path), '--test', '--host', host)
                self.assertIn("'text': '70'", str(requests[-2]))
                self.assertIn("'text': '1C'", str(requests[-2]))
                data_path = Path(directory) / 'input.json'
                data_path.write_text(json.dumps(app.demo(0)))
                run('--source', str(data_path), '--config', str(config_path), '--test', '--host', host)
                self.assertIn("'text': '1!'", str(requests[-2]))
            finally:
                server.shutdown()
                worker.join()

    @mock.patch.dict(os.environ, {'FIREBOARD_TOKEN': 'synthetic-token'})
    @mock.patch('app.urllib.request.build_opener')
    @mock.patch('app.send')
    @mock.patch('app.load_source')
    @mock.patch('sys.argv', ['app.py', '--cloud', '--config', 'private.json', '--test'])
    def test_cloud_cli_mocked(self, load, send, build):
        load.return_value = {'unit': 'F', 'channels': {
            'food': {'device_uuid': 'synthetic', 'channel': 1, 'label': 'FOOD'}}}
        build.return_value.open.return_value.__enter__.return_value.read.return_value = b'[]'
        app.main()
        self.assertIn('--', str(send.call_args_list[0]))
        self.assertEqual(send.call_args_list[-1].args, ('10.0.4.20',))
