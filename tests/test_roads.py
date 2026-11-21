"""roads.py 의 길 잇기 시험 (osmium 없이, 작은 가짜 도로망)."""
import math
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import roads  # noqa: E402

LAT0, LON0 = 37.0, 127.0
KX = 111_320.0 * math.cos(math.radians(LAT0))


def pt(north_m, east_m):
    return LAT0 + north_m / 110_540.0, LON0 + east_m / KX


def way(wid, ids, pts, hw="primary", fwd=True, bwd=True, lanes=0):
    import array
    return (wid, array.array("q", ids), array.array("i", (round(p[0] * roads.E7) for p in pts)),
            array.array("i", (round(p[1] * roads.E7) for p in pts)), fwd, bwd, hw, lanes, False, False)


def north_of(g, e):
    """방향 간선 시작점의 북쪽 거리(m)."""
    return round((g.edges[e][2][0][0] - LAT0) * 110_540.0)


class RoadsTest(unittest.TestCase):
    def setUp(self):
        # 남북으로 곧은 큰길 (노드 1~4, 0·400·800·1200m), 400m 지점에서 동쪽으로 골목 (노드 2→10)
        self.ways = [
            way(100, [1, 2, 3, 4], [pt(0, 0), pt(400, 0), pt(800, 0), pt(1200, 0)]),
            way(101, [2, 10], [pt(400, 0), pt(400, 300)], hw="residential"),
            # 한 방향 고속도로 (노드 20→21), 북쪽으로만
            way(102, [20, 21], [pt(0, 2000), pt(1000, 2000)], hw="motorway", bwd=False),
        ]
        self.g = roads.Graph(self.ways)

    def test_two_way_road_both_sides(self):
        lat, lon = pt(1000, 5)   # 큰길 1000m 지점, 5m 옆
        es = roads.approach(self.g, lat, lon, 600)
        starts = sorted(north_of(self.g, e) for e in es)
        # 남쪽에서 오는 간선(400→800, 800→1200 중 카메라 토막)과 북쪽에서 오는 간선(1200→800)
        self.assertIn(400, starts)
        self.assertIn(1200, starts)

    def test_lower_class_side_street_not_included(self):
        lat, lon = pt(1000, 0)
        es = roads.approach(self.g, lat, lon, 1200)
        self.assertTrue(all(self.g.edges[e][6] == 100 for e in es))   # 골목(way 101)은 들어가지 않음

    def test_oneway_only_from_behind(self):
        lat, lon = pt(800, 2000)
        es = roads.approach(self.g, lat, lon, 1200)
        self.assertEqual(len(es), 1)
        self.assertEqual(north_of(self.g, next(iter(es))), 0)

    def test_budget_limits_edges(self):
        lat, lon = pt(1200, 0)
        es = roads.approach(self.g, lat, lon, 300)
        # 카메라 토막(400→1200, 1200→400)만. 0→400 간선은 예산(300m) 밖
        self.assertEqual(sorted(north_of(self.g, e) for e in es), [400, 1200])

    def test_no_road_nearby(self):
        lat, lon = pt(600, 500)
        self.assertEqual(roads.approach(self.g, lat, lon, 600), set())

    def test_turn_restriction(self):
        # 동서 큰길(way 200, 노드 30-31-32)에서 노드 31 로 남북 큰길(way 201, 31→33 북쪽)로 들어가는 좌·우회전,
        # 서쪽에서 오는 차(200: 30→31)는 201 로 들어가면 안 됨
        ways = [
            way(200, [30, 31, 32], [pt(0, -400), pt(0, 0), pt(0, 400)]),
            way(201, [31, 33], [pt(0, 0), pt(600, 0)], fwd=True, bwd=False),
        ]
        g = roads.Graph(ways, [(200, 31, 201, "no")])
        es = roads.approach(g, *pt(500, 0), 1200)
        self.assertEqual(len(es), 1)   # 카메라 토막만, 동서 길에서는 못 들어옴
        g2 = roads.Graph(ways)
        self.assertEqual(len(roads.approach(g2, *pt(500, 0), 1200)), 3)   # 제한이 없으면 양쪽에서 들어옴

    def test_only_restriction(self):
        # OSM 회전 제한의 from·to 길은 via 교차점에서 끝나야 한다: 동서 길을 서쪽(200)·동쪽(202)으로 나눔
        ways = [
            way(200, [30, 31], [pt(0, -400), pt(0, 0)]),
            way(202, [31, 32], [pt(0, 0), pt(0, 400)]),
            way(201, [31, 33], [pt(0, 0), pt(600, 0)], fwd=True, bwd=False),
        ]
        # 서쪽에서 오면 직진만 (200 → 202)
        g = roads.Graph(ways, [(200, 31, 202, "only")])
        es = roads.approach(g, *pt(500, 0), 1200)
        starts = sorted(round((g.edges[e][2][0][1] - LON0) * KX) for e in es)
        self.assertEqual(starts, [0, 400])   # 카메라 토막 + 동쪽에서 오는 간선 (서쪽에서는 못 들어옴)

    def test_write_db_roundtrip(self):
        # 큰길 옆 40m 나란한 길은 둘레 길로 들어간다
        ways = self.ways + [way(103, [40, 41], [pt(0, 40), pt(1200, 40)], hw="residential")]
        g = roads.Graph(ways)
        appr = {"CAM": roads.approach(g, *pt(1000, 0), 600)}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.db")
            n, _ = roads.write_db(g, appr, path, {"format": "2"})
            con = sqlite3.connect(path)
            self.assertEqual(con.execute("SELECT count(*) FROM edge").fetchone()[0], n)
            flags = [r[0] for r in con.execute("SELECT flags FROM edge")]
            self.assertTrue(any(f & 0xF == roads.RANK["residential"] for f in flags))   # 나란한 길 포함
            self.assertTrue(all(f & 0xF != roads.RANK["motorway"] for f in flags))      # 2km 떨어진 고속도로는 없음
            blob = con.execute("SELECT edges FROM approach WHERE key='CAM'").fetchone()[0]
            self.assertEqual(len(decode_varints(blob)), len(appr["CAM"]))
            con.close()

    def test_varints(self):
        self.assertEqual(decode_varints(roads.varints([5, 300, 1, 70000])), [1, 5, 300, 70000])


def decode_varints(b):
    out, cur, shift, prev = [], 0, 0, 0
    for x in b:
        cur |= (x & 0x7F) << shift
        if x & 0x80:
            shift += 7
        else:
            prev += cur
            out.append(prev)
            cur, shift = 0, 0
    return out


if __name__ == "__main__":
    unittest.main()
