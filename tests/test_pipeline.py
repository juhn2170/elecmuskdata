"""서비스키 없이 돌리는 파이프라인 테스트. 실행: python -m unittest discover -s tests"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import build  # noqa: E402

KST = timezone(timedelta(hours=9))


def item(no: str, lat: float, lon: float, **kw) -> dict:
    base = {
        "mnlssRegltCameraManageNo": no, "ctprvnNm": "경기도", "signguNm": "수원시",
        "roadKnd": "시도", "roadRouteNo": "", "roadRouteNm": "정조로", "roadRouteDrc": "1",
        "rdnmadr": "경기도 수원시 정조로 1", "lnmadr": "", "latitude": str(lat), "longitude": str(lon),
        "itlpc": "정조로 (A동→B동)", "regltSe": "01", "lmttVe": "50", "regltSctnLcSe": "",
        "ovrspdRegltSctnLt": "", "prtcareaType": "", "installationYear": "2020",
        "referenceDate": "2026-10-20", "insttCode": "1", "insttNm": "수원시",
    }
    base.update(kw)
    return base


def sample(n: int = 12_000) -> list[dict]:
    rows = [item(f"N{i}", 37.0 + i * 1e-4, 127.0 + i * 1e-4) for i in range(n)]
    rows.append(item("BAD", 0, 0))                                   # 범위 밖 좌표
    rows.append(item("DUP", 37.5, 127.5, roadRouteDrc="1"))          # 같은 번호·좌표, 방향만 다름
    rows.append(item("DUP", 37.5, 127.5, roadRouteDrc="2", lmttVe="0", regltSe="1+2"))
    return rows


def write_raw(path: str, items: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"total_count": len(items), "items": items}, f, ensure_ascii=False)


class NormalizeTest(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(build.code("01"), "1")
        self.assertEqual(build.code("1+2"), "1,2")
        self.assertEqual(build.code(" 99 "), "99")
        self.assertIsNone(build.code(""))

    def test_direction_text(self):
        self.assertEqual(build.direction_text("정조로 (A동→B동)"), "A동→B동")
        self.assertEqual(build.direction_text("정조로 (A동 -> B동)"), "A동→B동")
        self.assertIsNone(build.direction_text("정조로 앞"))
        # 실데이터에서 본 형태 (2026-11-21)
        self.assertEqual(build.direction_text("학교 앞(세류초교)(매교동→윗버드내사거리"), "매교동→윗버드내사거리")
        self.assertEqual(build.direction_text("동성학교네거리→상동네거리"), "동성학교네거리→상동네거리")
        self.assertEqual(build.direction_text("보호구역,쌍계오거리→쌍계삼거리"), "쌍계오거리→쌍계삼거리")
        self.assertEqual(build.direction_text("담양 → 동강대"), "담양→동강대")

    def test_out_of_range(self):
        self.assertIsNone(build.normalize(item("X", 0, 0)))
        self.assertIsNone(build.normalize(item("X", 37.5, 200)))

    def test_speed_flag(self):
        self.assertEqual(build.normalize(item("X", 37.5, 127.5))["is_speed"], 1)
        self.assertEqual(build.normalize(item("X", 37.5, 127.5, lmttVe="0"))["is_speed"], 0)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.prev = os.path.join(self.d, "prev")
        os.makedirs(self.prev)
        self.raw = os.path.join(self.d, "raw.json")

    def tearDown(self):
        self.tmp.cleanup()

    def run_build(self, items, out):
        write_raw(self.raw, items)
        code = build.build(self.raw, self.prev, out)
        with open(os.path.join(out, "run.json"), encoding="utf-8") as f:
            return code, json.load(f)

    def promote(self, out):
        for name in ("cameras.db", "version.json"):
            os.replace(os.path.join(out, name), os.path.join(self.prev, name))

    def test_first_publish_then_unchanged_then_diff(self):
        items = sample()
        out1 = os.path.join(self.d, "o1")
        code, run = self.run_build(items, out1)
        self.assertEqual((code, run["status"]), (0, "published"))
        self.assertEqual(run["count"], 12_002)
        self.assertEqual(run["dropped_coords"], 1)

        con = sqlite3.connect(os.path.join(out1, "cameras.db"))
        dup = con.execute("SELECT key, route_dir, reglt, is_speed FROM camera "
                          "WHERE manage_no='DUP' ORDER BY key").fetchall()
        self.assertEqual(len(dup), 2)                      # 방향이 다르면 합치지 않음
        self.assertTrue(dup[1][0].endswith("-2"))
        self.assertIn(("2", "1,2", 0), [d[1:] for d in dup])
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertIn("저작자표시", con.execute("SELECT v FROM meta WHERE k='attribution'").fetchone()[0])
        con.close()
        self.promote(out1)

        # 같은 데이터 → 게시 안 함
        code, run = self.run_build(items, os.path.join(self.d, "o2"))
        self.assertEqual((code, run["status"]), (0, "unchanged"))
        self.assertFalse(os.path.exists(os.path.join(self.d, "o2", "cameras.db")))

        # 추가 1, 삭제 1, 변경 1
        items2 = items[1:] + [item("NEW", 36.0, 128.0)]
        items2[0] = dict(items2[0], lmttVe="60")
        out3 = os.path.join(self.d, "o3")
        code, run = self.run_build(items2, out3)
        self.assertEqual(run["status"], "published")
        self.assertEqual((run["added"], run["removed"], run["changed"]), (1, 1, 1))
        with open(os.path.join(out3, "version.json"), encoding="utf-8") as f:
            v = json.load(f)
        self.assertEqual(v["db_size"], os.path.getsize(os.path.join(out3, "cameras.db")))

    def test_drop_guard(self):
        out1 = os.path.join(self.d, "o1")
        self.run_build(sample(), out1)
        self.promote(out1)
        code, run = self.run_build(sample(9_000), os.path.join(self.d, "o2"))  # 25% 감소
        self.assertEqual((code, run["status"]), (2, "blocked"))

    def test_first_publish_minimum(self):
        code, run = self.run_build(sample(100), os.path.join(self.d, "o1"))
        self.assertEqual((code, run["status"]), (2, "blocked"))


class SectionTest(unittest.TestCase):
    def hw(self, no, place, pos, lat=36.5, limit="110"):
        return item(no, lat, 127.0, roadKnd="고속국도", roadRouteNm="서해안고속도로", itlpc=place,
                    regltSctnLcSe=pos, lmttVe=limit)

    def test_route_name(self):
        self.assertEqual(build.route_name("서해안선"), build.route_name("15번 서해안고속도로"))
        self.assertEqual(build.route_name("수도권제2순환선(파주-양주)"), "수도권제2순환")

    def test_match(self):
        cams = [build.normalize(x) for x in (
            self.hw("A", "서해안고속도로 302.5km 시점 (목포→서울)", "1"),
            self.hw("B", "서해안고속도로 312.3km 종점 (목포→서울)", "2", lat=36.6, limit="100"),
            self.hw("C", "서해안고속도로 302.6km 시점 (서울→목포)", "1"),   # 반대 방향
        )]
        build.assign_keys(cams)
        sec = {"route": "서해안선", "direction": "서울", "start_km": 302.8, "end_km": 312.0,
               "start_place": "발안IC 인근", "end_place": "비봉IC 인근", "length_km": 9.2}
        far = dict(sec, start_km=200.0, end_km=210.0)
        s, f = build.match_sections([sec, far], cams)
        keys = {c["manage_no"]: c["key"] for c in cams}
        self.assertEqual((s["start_key"], s["end_key"], s["match"]), (keys["A"], keys["B"], "both"))
        self.assertEqual((s["limit_kmh"], s["ambiguous"]), (100, 0))
        self.assertEqual((f["match"], f["start_key"]), ("none", None))

    def test_section_kept_when_download_fails(self):
        with tempfile.TemporaryDirectory() as d:
            prev, raw, kec = os.path.join(d, "prev"), os.path.join(d, "raw.json"), os.path.join(d, "kec.csv")
            os.makedirs(prev)
            write_raw(raw, sample())
            with open(kec, "w", encoding="utf-8") as f:
                f.write("노선,방향,시점,시점인근시설물,종점,종점인근시설물,연장\n서해안선,서울,1,a,2,b,1\n")
            build.build(raw, prev, os.path.join(d, "o1"), kec)
            for n in ("cameras.db", "version.json"):
                os.replace(os.path.join(d, "o1", n), os.path.join(prev, n))
            code = build.build(raw, prev, os.path.join(d, "o2"))           # CSV 없음
            with open(os.path.join(d, "o2", "run.json"), encoding="utf-8") as f:
                run = json.load(f)
            self.assertEqual((code, run["status"], run["sections"]), (0, "unchanged", 1))


class RunLogTest(unittest.TestCase):
    def test_trim_365_days(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "runs.tsv")
            old = (datetime.now(KST) - timedelta(days=400)).isoformat(timespec="seconds")
            recent = (datetime.now(KST) - timedelta(days=10)).isoformat(timespec="seconds")
            with open(log, "w", encoding="utf-8") as f:
                f.write(f"run_at\tstatus\n{old}\tunchanged\n{recent}\tunchanged\n")
            subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "runlog.py"), d, log],
                           check=True, capture_output=True)
            with open(log, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertTrue(lines[0].startswith("run_at\t"))
            self.assertEqual(len(lines), 3)               # 헤더 + 최근 1 + 이번 실행(실패) 1
            self.assertTrue(lines[1].startswith(recent))
            self.assertIn("failed", lines[2])


if __name__ == "__main__":
    unittest.main()
