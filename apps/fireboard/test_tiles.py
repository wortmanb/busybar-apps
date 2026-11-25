import copy
import unittest
import app


class TileTests(unittest.TestCase):
    def setUp(self):
        self.probes = app.parse(app.demo(1000), app.DEMO_CONFIG, 1000)

    def test_pagination_and_anchor(self):
        foods = [copy.deepcopy(self.probes[0]) for _ in range(7)]
        probes = self.probes[2:] + foods
        seen = set()
        for page in range(4):
            tiles = app.tile_sources(probes, page)
            self.assertIs(tiles[0], probes[0])
            self.assertIs(tiles[1], probes[1])
            seen.update(id(p) for p in tiles[2:])
        self.assertEqual(seen, {id(p) for p in foods})
        self.assertNotEqual(app.SPRITES['grate'], app.SPRITES['pit'])

    def test_bounds_and_retained_ids(self):
        baseline = None
        for unit in ('F', 'C'):
            for value in (-999, -10, 0, 203, 9999, 1e20, None):
                self.probes[0].temp = value
                for page in range(6):
                    elements = app.render(self.probes, unit, page, 3660)
                    ids = {e['id']: e['type'] for e in elements}
                    baseline = baseline or ids
                    self.assertEqual(ids, baseline)
                    self.assertLessEqual(len(ids), 100)
                    for e in elements:
                        width = e.get('width', max(1, len(e.get('text', ''))) * 4 - 1)
                        height = e.get('height', 5)
                        self.assertLessEqual(e['x'] + width, 72)
                        self.assertLessEqual(e['y'] + height, 16)
        self.assertEqual({e['id']: e['type'] for e in app.render([], 'F', 0, error='AUTH')}, baseline)

    def test_high_not_low(self):
        pit = self.probes[2]
        pit.temp = 226
        status, progress = app.proximity(pit, 'F')
        self.assertEqual(status, 'HI 49F')
        self.assertLess(progress, 1)
        for temp in (275, 280):
            pit.temp = temp
            self.assertEqual(app.proximity(pit, 'F'), ('HI HIT', 1))
        pit.limits = [('LO', 225)]
        self.assertEqual(app.proximity(pit, 'F'), ('NO TARGET', 0))

    def test_milestone_and_invalid_bars(self):
        p = self.probes[0]
        for temp, expected in ((164, 'M +1F'), (165, 'M HIT'), (204, 'DONE')):
            p.temp = temp
            self.assertEqual(app.proximity(p, 'F')[0], expected)
        for stale, value in ((True, 160), (False, None)):
            p.stale, p.temp = stale, value
            elements = app.render([p], 'F', 0)
            bars = [e for e in elements if e['id'].startswith('t1b')]
            self.assertNotIn(app.GREEN, str(bars))
            self.assertEqual(app.proximity(p, 'F'), ('--', 0))
        p.stale, p.temp = True, 160
        self.assertIn('1!', str(app.render([p], 'F', 0)))
        p.temp = None
        self.assertIn('--', str(app.render([p], 'F', 0)))

    def test_complete_sprites(self):
        # Rasterize emitted rectangles to catch truncation when optimizing ID budget.
        for role, sprite in app.SPRITES.items():
            probe = copy.deepcopy(self.probes[0])
            probe.role = role
            slot = 0 if role == 'pit' else 1
            canvas = {}
            for e in app.render([probe], 'F', 0):
                if not e['id'].startswith(f't{slot}p'):
                    continue
                ink = e['fill_colors'][0]
                if ink == '#00000000':
                    continue
                for y in range(e['y'], e['y'] + e['height']):
                    for x in range(e['x'], e['x'] + e['width']):
                        canvas[x - slot * 18, y] = ink
            for y, row in enumerate(sprite):
                for x, pixel in enumerate(row):
                    self.assertEqual(canvas.get((x, y), app.BLACK), app.PALETTE[pixel])

    def test_elapsed(self):
        self.assertEqual(app.elapsed_label(0), '00:00')
        self.assertEqual(app.elapsed_label(3660), '01:01')
        self.assertEqual(app.elapsed_label(360000), '99h+')
        self.assertIn('0101', str(app.render(self.probes[:1], 'F', 0, 3660)))
        self.assertNotIn('0101', str(app.render(self.probes, 'F', 0, 3660)))
        self.assertEqual(app.render(self.probes, 'F', 0, 123), app.render(self.probes, 'F', 0, 123))
