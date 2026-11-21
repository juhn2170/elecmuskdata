"""roads.py 의 길 잇기 시험 (osmium 없이, 작은 가짜 도로망)."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import roads  # noqa: E402

LAT0, LON0 = 37.0, 127.0
KX = 111_320.0 * math.cos(math.radians(LAT0))


def pt(north_m, east_m):
    return LAT0 + north_m / 110_540.0, LON0 + east_m / KX


def way(ids, pts, hw="primary", fwd=True, bwd=True):
    import array
    return (array.array("q", ids), array.array("i", (round(p[0] * roads.E7) for p in pts)),
            array.array("i", (round(p[1] * roads.E7) for p in pts)), fwd, bwd, hw)


class RoadsTest(unittest.TestCase):
    def setUp(self):
        # 남북으로 곧은 큰길 (노드 1~4, 0·400·800·1200m), 400m 지점에서 동쪽으로 골목 (노드 2→10)
        self.ways = [
            way([1, 2, 3, 4], [pt(0, 0), pt(400, 0), pt(800, 0), pt(1200, 0)]),
            way([2, 10], [pt(400, 0), pt(400, 300)], hw="residential"),
            # 한 방향 고속도로 (노드 20→21), 북쪽으로만
            way([20, 21], [pt(0, 2000), pt(1000, 2000)], hw="motorway", bwd=False),
        ]
        self.g = roads.Graph(self.ways)

    def test_two_way_road_both_sides(self):
        lat, lon = pt(1000, 5)   # 큰길 1000m 지점, 5m 옆
        pieces = roads.approach(self.g, lat, lon, 600)
        # 남쪽에서 오는 길(400~1000m)과 북쪽에서 오는 길(1200→1000m)
        norths = sorted(round((p[0][0] - LAT0) * 110_540.0) for p in pieces)
        self.assertEqual(norths[0], 400)
        self.assertIn(1200, norths)
        # 모든 조각은 카메라 쪽 끝에서 끝나거나 다음 조각으로 이어진다
        ends = [roads.meters(*p[-1], lat, lon) for p in pieces]
        self.assertLess(min(ends), 6)

    def test_lower_class_side_street_not_included(self):
        lat, lon = pt(1000, 0)
        pieces = roads.approach(self.g, lat, lon, 1200)
        easts = [abs(q[1] - LON0) * KX for p in pieces for q in p]
        self.assertLess(max(easts), 1)   # 골목(동쪽 300m)은 들어가지 않음

    def test_oneway_only_from_behind(self):
        lat, lon = pt(800, 2000)
        pieces = roads.approach(self.g, lat, lon, 1200)
        self.assertEqual(len(pieces), 1)
        self.assertAlmostEqual((pieces[0][0][0] - LAT0) * 110_540.0, 0, delta=1)

    def test_budget_cuts_long_road(self):
        lat, lon = pt(1200, 0)
        pieces = roads.approach(self.g, lat, lon, 300)
        total = sum(roads.meters(*p[i - 1], *p[i]) for p in pieces for i in range(1, len(p)))
        self.assertAlmostEqual(total, 300, delta=2)

    def test_no_road_nearby(self):
        lat, lon = pt(600, 500)
        self.assertEqual(roads.approach(self.g, lat, lon, 600), [])

    def test_encode_continues_deltas(self):
        a = [pt(0, 0), pt(100, 0)]
        b = [pt(100, 0), pt(200, 50)]
        s = roads.encode_all([a, b])
        self.assertEqual(len(s.split(" ")), 2)
        self.assertLess(len(s.split(" ")[1]), 12)   # 둘째 조각 첫 점은 짧은 차이값


if __name__ == "__main__":
    unittest.main()
