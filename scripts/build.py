"""내려받은 원본(raw.json)을 정제해서 cameras.db, version.json, changes.json, patch.json.gz, run.json 을 만든다.

사용: python scripts/build.py RAW_JSON PREV_DIR OUT_DIR [KEC_CSV]
  PREV_DIR: 지난번 Release의 cameras.db, version.json 이 있는 폴더 (없으면 첫 게시)
  KEC_CSV:  한국도로공사 구간단속 CSV (없으면 지난 게시본의 구간을 유지)

종료 코드
  0  게시할 새 데이터 있음 (run.json status=published)
  0  바뀐 것 없음        (run.json status=unchanged, 게시 안 함)
  2  검사 실패           (run.json status=blocked, 게시 안 함)
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
SCHEMA_VERSION = 1
DROP_LIMIT = 0.20          # 지난번보다 20% 넘게 줄면 게시 안 함
MIN_ROWS = 10_000          # 첫 게시 때의 최소 건수 (2026-11 실측 44,650건)
LAT_RANGE = (33.0, 39.0)   # 대한민국 범위 (제주~강원 북부)
LON_RANGE = (124.0, 132.0)
GRID = 100                 # 0.01도 격자 (위도 약 1.1km, 경도 약 0.9km)
ATTRIBUTION = (
    "출처: 공공데이터포털(data.go.kr) 전국무인교통단속카메라표준데이터. "
    "이용허락범위: 저작자표시. "
    "구간단속: 공공데이터포털 한국도로공사_구간단속현황 (이용허락범위 제한 없음). "
    "원본을 정제·가공한 데이터임."
)

ARROW = re.compile(r"\(([^()]*?(?:→|->|⇒|➝|➡)[^()]*?)\)")
ARROW_BARE = re.compile(r"[^(),:]*(?:→|->|⇒|➝|➡)[^(),:]*")


# ---------- 정규화 ----------

def text(v) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def code(v) -> str | None:
    """'01' → '1', '1+2' / '1,2' → '1,2'. 숫자가 없으면 원문 유지."""
    s = text(v)
    if s is None:
        return None
    parts = [str(int(p)) for p in re.findall(r"\d+", s)]
    return ",".join(parts) if parts else s


def number(v) -> float | None:
    s = text(v)
    if s is None:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def integer(v) -> int | None:
    f = number(v)
    return int(round(f)) if f is not None else None


def date(v) -> str | None:
    s = text(v)
    if s is None:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return s


def direction_text(place: str | None) -> str | None:
    if not place:
        return None
    m = ARROW.search(place)
    if m:
        s = m.group(1)
    else:
        # 괄호 없이 쓰거나 괄호를 닫지 않은 경우: 화살표 앞뒤로 구분자((, ), 쉼표, 콜론)까지
        m = ARROW_BARE.search(place)
        if not m:
            return None
        s = m.group(0)
    return re.sub(r"\s*(?:→|->|⇒|➝|➡)\s*", "→", s).strip() or None


def normalize(item: dict) -> dict | None:
    lat, lon = number(item.get("latitude")), number(item.get("longitude"))
    if lat is None or lon is None:
        return None
    if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
        return None
    place = text(item.get("itlpc"))
    limit = integer(item.get("lmttVe"))
    return {
        "manage_no": text(item.get("mnlssRegltCameraManageNo")),
        "sido": text(item.get("ctprvnNm")),
        "sigungu": text(item.get("signguNm")),
        "road_kind": text(item.get("roadKnd")),
        "road_no": text(item.get("roadRouteNo")),
        "road_name": text(item.get("roadRouteNm")),
        "route_dir": code(item.get("roadRouteDrc")),
        "addr_road": text(item.get("rdnmadr")),
        "addr_lot": text(item.get("lnmadr")),
        "place": place,
        "direction_text": direction_text(place),
        "lat": round(lat, 7),
        "lon": round(lon, 7),
        "reglt": code(item.get("regltSe")),
        "reglt_raw": text(item.get("regltSe")),
        "limit_kmh": limit,
        "is_speed": 1 if (limit or 0) > 0 else 0,   # 알림 대상: 제한속도 > 0 (기획서 5.4)
        "section_pos": code(item.get("regltSctnLcSe")),
        "section_len": number(item.get("ovrspdRegltSctnLt")),
        "protect_area": text(item.get("prtcareaType")),
        "install_year": integer(item.get("installationYear")),
        "ref_date": date(item.get("referenceDate")),
        "inst_code": text(item.get("insttCode")),
        "inst_name": text(item.get("insttNm")) or text(item.get("institutionNm")),
        "grid_y": math.floor(lat * GRID),
        "grid_x": math.floor(lon * GRID),
    }


# ---------- 자체 키 ----------

def assign_keys(rows: list[dict]) -> None:
    """자체 키 = 관리번호 + 시도 + 좌표 (기획서 5.3). 겹치면 정해진 순서로 -2, -3 을 붙인다."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        base = f"{r['manage_no']}|{r['sido']}|{r['lat']:.6f}|{r['lon']:.6f}"
        k = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
        groups.setdefault(k, []).append(r)
    for k, group in groups.items():
        group.sort(key=lambda r: json.dumps(
            [r["route_dir"], r["reglt"], r["limit_kmh"], r["place"], r["addr_road"], r["ref_date"]],
            ensure_ascii=False, default=str))
        for i, r in enumerate(group):
            r["key"] = k if i == 0 else f"{k}-{i + 1}"


def row_hash(r: dict) -> str:
    return hashlib.sha1(json.dumps(r, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


# ---------- DB ----------

COLUMNS = [
    "key", "manage_no", "sido", "sigungu", "road_kind", "road_no", "road_name", "route_dir",
    "addr_road", "addr_lot", "place", "direction_text", "lat", "lon", "reglt", "reglt_raw",
    "limit_kmh", "is_speed", "section_pos", "section_len", "protect_area", "install_year",
    "ref_date", "inst_code", "inst_name", "grid_y", "grid_x",
]

SCHEMA = """
CREATE TABLE camera (
  key TEXT PRIMARY KEY,
  manage_no TEXT, sido TEXT, sigungu TEXT,
  road_kind TEXT, road_no TEXT, road_name TEXT, route_dir TEXT,
  addr_road TEXT, addr_lot TEXT, place TEXT, direction_text TEXT,
  lat REAL NOT NULL, lon REAL NOT NULL,
  reglt TEXT, reglt_raw TEXT, limit_kmh INTEGER, is_speed INTEGER NOT NULL,
  section_pos TEXT, section_len REAL, protect_area TEXT, install_year INTEGER,
  ref_date TEXT, inst_code TEXT, inst_name TEXT,
  grid_y INTEGER NOT NULL, grid_x INTEGER NOT NULL
);
CREATE INDEX camera_grid ON camera(grid_y, grid_x);
CREATE TABLE section (
  id INTEGER PRIMARY KEY,
  route TEXT, direction TEXT, start_km REAL, end_km REAL,
  start_place TEXT, end_place TEXT, length_km REAL,
  start_key TEXT, end_key TEXT, limit_kmh INTEGER,
  match TEXT NOT NULL, ambiguous INTEGER NOT NULL, source TEXT NOT NULL, method TEXT
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""


# ---------- 구간단속 (한국도로공사) ----------

SECTION_COLUMNS = ["route", "direction", "start_km", "end_km", "start_place", "end_place", "length_km",
                   "start_key", "end_key", "limit_kmh", "match", "ambiguous", "source", "method"]
# 이정 표기: "302.5km", "6.8k", "96.4㎞". "IC 2km후", "1km 전방"처럼 거리를 말하는 표기는 뺀다.
KM = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:km|㎞|k)(?![a-z가-힣])(?!\s*(?:전|후|앞))", re.I)
MATCH_KM = 1.0   # 이정(km) 차이 허용 범위

# 같은 노선을 가리키는 다른 이름 (노선 번호가 같고 이정이 이어지는 것만).
# 카메라 데이터는 옛 이름·구간 이름을, 도로공사 데이터는 현재 노선 이름을 쓴다.
ROUTE_ALIAS = {
    "통영대전": "통영대전중부", "중부": "통영대전중부", "대전통영중부": "통영대전중부",
    "대전통영": "통영대전중부", "통영대전중부": "통영대전중부",
    "대전당진": "당진영덕", "당진대전": "당진영덕", "청주영덕": "당진영덕", "상주영덕": "당진영덕",
    "청원상주": "당진영덕",
    "대구포항": "새만금포항", "익산장수": "새만금포항", "익산포항": "새만금포항",
    "천안논산": "호남",
    "서울외곽순환": "수도권제1순환", "제2순환": "수도권제2순환",
    "부산외곽순환도로": "부산외곽순환", "구리포천": "세종포천",
    "남해고속도로제2지": "남해제2지", "중앙고속도로지": "중앙지", "호남지선": "호남지",
    "중부내륙지선로": "중부내륙지",
}
# 노선 이름이 비어 있을 때("45번 고속도로", "고속도로")만 쓰는 노선 번호
ROUTE_NO = {
    "1": "경부", "10": "남해", "12": "광주대구", "14": "함양울산", "15": "서해안", "16": "울산",
    "20": "새만금포항", "25": "호남", "27": "순천완주", "29": "세종포천", "30": "당진영덕",
    "35": "통영대전중부", "37": "제2중부", "40": "평택제천", "45": "중부내륙", "50": "영동",
    "55": "중앙", "60": "서울양양", "65": "동해", "100": "수도권제1순환", "104": "남해제2지",
    "251": "호남지", "400": "수도권제2순환", "451": "중부내륙지", "551": "중앙지", "600": "부산외곽순환",
}
PLACE_ROUTE = re.compile(r"([가-힣0-9]{2,}?)간?(?:고속도로|고속국도|선)(?=[\s(\d_]|$)")
FACILITY = re.compile(r"(?:IC|JCT|JC|SA|TG|TN|터널|분기점|나들목|휴게소|요금소|졸음쉼터|교)$", re.I)


def route_name(s: str | None) -> str:
    """'서해안선', '서해안고속도로', '15번 서해안고속도로' → '서해안'. 옛 이름은 현재 노선 이름으로 바꾼다."""
    s = re.sub(r"\(.*?\)", "", s or "")
    s = re.sub(r"^\d+번\s*", "", s.strip()).replace(" ", "").replace("-", "")
    s = re.sub(r"(고속도로|고속국도)$", "", s)
    s = ROUTE_ALIAS.get(s, s)
    s = re.sub(r"선$", "", s)
    return ROUTE_ALIAS.get(s, s)


def camera_routes(c: dict) -> set[str]:
    """카메라가 속한 노선 이름들. 도로노선명, 설치장소에 적힌 노선명, (이름이 없으면) 노선번호에서 모은다."""
    out = set()
    name = route_name(c["road_name"])
    if name and not name.endswith("로"):
        out.add(name)
    for m in PLACE_ROUTE.finditer((c["place"] or "").replace(" ", "").replace("-", "")):
        r = route_name(m.group(1))
        if r and r != "고속":
            out.add(r)
    if not out and is_highway(c):
        no = re.sub(r"\D", "", c["road_no"] or "") or (re.match(r"(\d+)번", c["road_name"] or "") or [None, None])[1]
        if no in ROUTE_NO:
            out.add(ROUTE_NO[no])
    return out


def is_highway(c: dict) -> bool:
    return c["road_kind"] == "고속국도" or "고속도로" in (c["road_name"] or "")


DIRECTION_WORD = re.compile(r"([가-힣]{2,})\s*(?:방향|방면)")


def facility(s: str | None) -> str | None:
    """'덕평SA 인근' → '덕평', '양서2 TN' → '양서2'. 두 글자 미만이면 None."""
    s = re.sub(r"(인근|입구|출구|부근|\s)", "", s or "")
    s = FACILITY.sub("", s)
    return s.lower() if len(s) >= 2 else None


def distance_km(a: dict, b: dict) -> float:
    p1, p2 = math.radians(a["lat"]), math.radians(b["lat"])
    dp, dl = p2 - p1, math.radians(b["lon"] - a["lon"])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def fits_length(a: dict, b: dict, length: float | None) -> bool:
    """시점·종점 카메라 사이 직선거리가 구간 길이와 맞는지. 도로는 굽으므로 직선거리는 더 짧을 수 있다."""
    if not length:
        return False
    d = distance_km(a, b)
    return 0.5 * length <= d <= length * 1.1 + 2.0


def section_len_km(c: dict) -> float | None:
    v = c["section_len"]
    if not v:
        return None
    return v / 1000 if v > 100 else v   # 원본에 m로 적힌 것(15834.0)이 섞여 있다


def match_sections(sections: list[dict], cameras: list[dict]) -> list[dict]:
    """도로공사 데이터에는 좌표가 없다. 표준데이터의 구간단속 카메라(시점 1, 종점 2) 중에서 찾는다 (기획서 5.3, 🟡).

    1) km: 같은 노선 + 설치장소에 적힌 이정이 1km 이내 + 시점/종점 구분이 맞고 + 방향이 반대가 아닌 것.
    2) pair: 한쪽만 찾았으면, 같은 노선의 반대쪽(시점↔종점) 카메라 중 직선거리가 구간 길이와 맞는 것.
    3) name: 둘 다 못 찾았으면, 설치장소·방향 문구에 시점·종점 시설물 이름(덕평SA → '덕평')이 있고
       직선거리가 구간 길이와 맞는 시점·종점 카메라 쌍.
    후보가 여러 개면 가장 그럴듯한 것을 쓰고 ambiguous=1로 표시한다. method에 찾은 방법을 남긴다."""
    pool = []
    for c in cameras:
        if not is_highway(c) or c["section_pos"] not in ("1", "2", None):
            continue
        routes = camera_routes(c)
        if not routes:
            continue
        m = KM.search(c["place"] or "")
        text_ = ((c["place"] or "") + " " + (c["direction_text"] or "")).replace(" ", "").lower()
        pool.append({"c": c, "routes": routes, "km": float(m.group(1)) if m else None, "text": text_})

    # 노선마다 도로공사 데이터에 나오는 방향(목적지) 이름. 경부선이면 {서울, 부산}.
    # 합친 이름(통영대전중부)이 아니라 도로공사 노선 이름별로 모은다: 중부선은 {남이, 하남}, 통영대전선은 {대전, 통영}.
    directions: dict[str, set[str]] = {}
    for s in sections:
        if s["direction"]:
            directions.setdefault(s["route"], set()).add(s["direction"])

    def opposite_direction(c: dict, kec_route: str, d: str | None) -> bool:
        """방향 문구 'A→B'에서 KEC 방향(목적지)이 왼쪽에만 있거나,
        설치장소에 같은 노선의 다른 방향 이름('부산방향')만 적혀 있으면 반대 방향."""
        if not d:
            return False
        dt = c["direction_text"]
        if dt and "→" in dt:
            left, right = dt.split("→", 1)
            if d in left and d not in right:
                return True
        words = DIRECTION_WORD.findall(c["place"] or "")
        others = directions.get(kec_route, set()) - {d}
        return bool(words) and not any(d in w for w in words) and any(o in w for w in words for o in others)

    def usable(p: dict, s: dict, pos: str, strict: bool = False) -> bool:
        """strict: 시점/종점 구분이 적힌 카메라만 (km로 찾지 않은 경우)."""
        c = p["c"]
        pos_ok = c["section_pos"] == pos or (not strict and c["section_pos"] is None)
        return route_name(s["route"]) in p["routes"] and pos_ok and not opposite_direction(c, s["route"], s["direction"])

    def order(score: float, c: dict) -> tuple:
        """점수가 같으면 시점/종점이 적힌 카메라, 그다음 키 순서."""
        return (round(score, 6), c["section_pos"] is None, c["key"])

    def by_km(s: dict, pos: str) -> list[tuple[float, dict]]:
        km = s["start_km"] if pos == "1" else s["end_km"]
        if km is None:
            return []
        found = [(abs(p["km"] - km), p["c"]) for p in pool
                 if p["km"] is not None and abs(p["km"] - km) <= MATCH_KM and usable(p, s, pos)]
        return sorted(found, key=lambda x: order(*x))

    def by_pair(s: dict, known: dict, pos: str) -> list[tuple[float, dict]]:
        """known의 짝. 구간 길이와 카메라에 적힌 구간 길이가 가까울수록, 직선거리가 길이에 가까울수록 앞.
        한쪽을 이정으로 찾았으니 이 노선은 이정 체계가 같다: 짝에 이정이 적혀 있으면 그것도 맞아야 한다."""
        L = s["length_km"]
        want_km = s["end_km"] if pos == "2" else s["start_km"]
        found = []
        for p in pool:
            c = p["c"]
            if c is known or not usable(p, s, pos, strict=True) or not fits_length(known, c, L):
                continue
            if p["km"] is not None and want_km is not None and abs(p["km"] - want_km) > 2 * MATCH_KM:
                continue
            sl = section_len_km(c)
            if sl is not None and abs(sl - L) > max(1.5, 0.2 * L):
                continue
            found.append(((abs(sl - L) if sl is not None else 1.5) + abs(distance_km(known, c) - 0.9 * L) / 10, c))
        return sorted(found, key=lambda x: order(*x))

    def by_name(s: dict) -> list[tuple[float, dict, dict]]:
        a, b = facility(s["start_place"]), facility(s["end_place"])
        if not a or not b or a == b:
            return []
        starts = [p for p in pool if usable(p, s, "1", strict=True) and (a in p["text"] or b in p["text"])]
        ends = [p for p in pool if usable(p, s, "2", strict=True) and (a in p["text"] or b in p["text"])]
        found = []
        for ps in starts:
            for pe in ends:
                cs, ce = ps["c"], pe["c"]
                joined = ps["text"] + pe["text"]
                if a not in joined or b not in joined or not fits_length(cs, ce, s["length_km"]):
                    continue
                # 방향 문구가 있으면 '시점→종점' 순서여야 한다 (반대면 반대 방향 구간)
                dt = (cs["direction_text"] or ce["direction_text"] or "").replace(" ", "").lower()
                if "→" in dt:
                    left, right = dt.split("→", 1)
                    if not (a in left and b in right):
                        continue
                found.append((abs(distance_km(cs, ce) - 0.9 * s["length_km"]) / 10, cs, ce))
        return sorted(found, key=lambda x: (*order(x[0], x[1]), x[2]["section_pos"] is None, x[2]["key"]))

    def spread(cands: list[dict]) -> bool:
        """후보가 서로 다른 곳에 있는지 (같은 자리의 차로별 카메라는 하나로 본다)."""
        return any(distance_km(cands[0], c) > 0.3 for c in cands[1:])

    out = []
    for s in sections:
        cs, ce = by_km(s, "1"), by_km(s, "2")
        start, end = (cs[0][1] if cs else None), (ce[0][1] if ce else None)
        ambiguous = spread([c for _, c in cs]) or spread([c for _, c in ce])
        method = "km" if start or end else None
        if start and not end:
            cand = by_pair(s, start, "2")
            if cand:
                end, ambiguous, method = cand[0][1], ambiguous or spread([c for _, c in cand]), "km+pair"
        elif end and not start:
            cand = by_pair(s, end, "1")
            if cand:
                start, ambiguous, method = cand[0][1], ambiguous or spread([c for _, c in cand]), "km+pair"
        elif not start and not end:
            cand = by_name(s)
            if cand:
                _, start, end = cand[0]
                ambiguous = spread([x[1] for x in cand]) or spread([x[2] for x in cand])
                method = "name"
        limits = [c["limit_kmh"] for c in (start, end) if c and c["limit_kmh"]]
        out.append(dict(s, start_key=start and start["key"], end_key=end and end["key"],
                        limit_kmh=min(limits) if limits else None,
                        match="both" if start and end else "start" if start else "end" if end else "none",
                        ambiguous=int(ambiguous), source="한국도로공사", method=method))
    return out


def read_kec(path: str) -> list[dict]:
    import csv
    with open(path, encoding="utf-8", newline="") as f:
        out = []
        for r in csv.DictReader(f):
            r = {k.strip(): text(v) for k, v in r.items() if k}
            out.append({"route": r.get("노선"), "direction": r.get("방향"),
                        "start_km": number(r.get("시점")), "end_km": number(r.get("종점")),
                        "start_place": r.get("시점인근시설물"), "end_place": r.get("종점인근시설물"),
                        "length_km": number(r.get("연장"))})
        return out


def prev_sections(prev_dir: str) -> list[dict]:
    path = os.path.join(prev_dir, "cameras.db")
    if not os.path.exists(path):
        return []
    con = sqlite3.connect(path)
    try:
        con.row_factory = sqlite3.Row
        cols = {r[1] for r in con.execute("PRAGMA table_info(section)")}
        if not set(SECTION_COLUMNS) - {"method"} <= cols:
            return []
        select = ",".join(c if c in cols else f"NULL AS {c}" for c in SECTION_COLUMNS)
        return [dict(r) for r in con.execute(f"SELECT {select} FROM section ORDER BY id")]
    finally:
        con.close()


def write_db(path: str, rows: list[dict], meta: dict, sections: list[dict] | None = None) -> None:
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        con.executemany(
            f"INSERT INTO camera ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
            [[r[c] for c in COLUMNS] for r in rows],
        )
        con.executemany(
            f"INSERT INTO section ({','.join(SECTION_COLUMNS)}) VALUES ({','.join('?' * len(SECTION_COLUMNS))})",
            [[s[c] for c in SECTION_COLUMNS] for s in sections or []],
        )
        con.executemany("INSERT INTO meta VALUES (?, ?)",
                        [(k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
                         for k, v in meta.items()])
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
        con.execute("VACUUM")
    finally:
        con.close()


def read_prev(prev_dir: str) -> tuple[dict | None, dict[str, str]]:
    version, hashes = None, {}
    vpath, dpath = os.path.join(prev_dir, "version.json"), os.path.join(prev_dir, "cameras.db")
    if os.path.exists(vpath):
        with open(vpath, encoding="utf-8") as f:
            version = json.load(f)
    if os.path.exists(dpath):
        con = sqlite3.connect(dpath)
        try:
            con.row_factory = sqlite3.Row
            for row in con.execute(f"SELECT {','.join(COLUMNS)} FROM camera"):
                r = dict(row)
                hashes[r["key"]] = row_hash(r)
        finally:
            con.close()
    return version, hashes


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# 바뀐 행이 이보다 많으면 패치를 만들지 않는다 (앱은 전체를 받는다). 전체 DB는 약 13.6MB, 행 하나는 JSON으로 약 0.5KB.
PATCH_MAX_ROWS = 5_000
PATCH_FILE = "patch.json.gz"


def write_patch(path: str, prev_version: str, version: str, rows: list[dict], upsert_keys: set[str],
                delete_keys: set[str], sections: list[dict], meta: dict) -> dict:
    """지난 게시본(prev_version)에서 이번 게시본으로 가는 증분 (기획서 5.6).
    앱은 자기 DB 버전이 prev_version일 때만 쓴다. 구간(section)은 작아서(수백 행) 통째로 넣는다."""
    patch = {
        "format": 1,
        "from": prev_version,
        "to": version,
        "columns": COLUMNS,
        "upsert": [[r[c] for c in COLUMNS] for r in rows if r["key"] in upsert_keys],
        "delete": sorted(delete_keys),
        "section_columns": SECTION_COLUMNS,
        "sections": [[s[c] for c in SECTION_COLUMNS] for s in sections],
        "meta": {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in meta.items()},
        "count": len(rows),
    }
    data = json.dumps(patch, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.GzipFile(path, "wb", mtime=0) as f:
        f.write(data)
    return {"from": prev_version, "file": PATCH_FILE, "sha256": sha256_file(path), "size": os.path.getsize(path),
            "upsert": len(patch["upsert"]), "delete": len(patch["delete"])}


def write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------- 실행 ----------

def build(raw_path: str, prev_dir: str, out_dir: str, kec_path: str | None = None,
          now: datetime | None = None) -> int:
    now = now or datetime.now(KST)
    os.makedirs(out_dir, exist_ok=True)
    with open(raw_path, encoding="utf-8") as f:
        raw = json.load(f)
    items = raw["items"]

    rows, dropped = [], 0
    for item in items:
        r = normalize(item)
        if r is None:
            dropped += 1
        else:
            rows.append(r)
    assign_keys(rows)
    rows.sort(key=lambda r: r["key"])
    # 중복 제거는 여러 출처가 같은 장비를 가리킬 때만 한다 (기획서 5.3). 지금은 출처가 하나라 하지 않는다.

    # 구간단속: 도로공사 CSV를 못 받았으면 지난 게시본의 구간을 그대로 쓴다.
    if kec_path and os.path.exists(kec_path):
        sections, section_source = match_sections(read_kec(kec_path), rows), "새로 받음"
    else:
        sections, section_source = prev_sections(prev_dir), "지난 게시본 유지"
    section_matched = sum(1 for s in sections if s["match"] == "both")

    new_hashes = {r["key"]: row_hash({c: r[c] for c in COLUMNS}) for r in rows}
    data_hash = hashlib.sha256(
        ("\n".join(f"{k}:{v}" for k, v in sorted(new_hashes.items())) + "\n#section\n"
         + "\n".join(row_hash({c: s[c] for c in SECTION_COLUMNS}) for s in sections)).encode("utf-8")
    ).hexdigest()

    prev_version, prev_hashes = read_prev(prev_dir)
    added = len(new_hashes.keys() - prev_hashes.keys())
    removed = len(prev_hashes.keys() - new_hashes.keys())
    changed = sum(1 for k in new_hashes.keys() & prev_hashes.keys() if new_hashes[k] != prev_hashes[k])
    count = len(rows)
    prev_count = (prev_version or {}).get("count")

    run = {
        "run_at": now.isoformat(timespec="seconds"),
        "source_total": raw.get("total_count", len(items)),
        "count": count, "dropped_coords": dropped,
        "added": added, "removed": removed, "changed": changed,
        "sections": len(sections), "sections_matched": section_matched, "section_source": section_source,
    }

    def finish(status: str, note: str, code_: int) -> int:
        run.update(status=status, note=note)
        write_json(os.path.join(out_dir, "run.json"), run)
        print(f"{status}: {note}")
        return code_

    # 검사
    if prev_count:
        if count < prev_count * (1 - DROP_LIMIT):
            return finish("blocked", f"카메라 수 {prev_count} → {count} (20% 넘게 감소)", 2)
    elif count < MIN_ROWS:
        return finish("blocked", f"첫 게시인데 {count}건뿐 (최소 {MIN_ROWS})", 2)

    if prev_version and prev_version.get("data_hash") == data_hash:
        # 바뀐 게 없어도 "오늘 원본과 대조해 최신임"을 앱에 알린다 (version.json만 다시 올림, 버전은 그대로).
        write_json(os.path.join(out_dir, "version.json"), dict(prev_version, checked_at=run["run_at"]))
        return finish("unchanged", f"{count}건, 바뀐 것 없음", 0)

    version = now.strftime("%Y%m%d.%H%M")
    ref_dates: dict[str, str] = {}
    for r in rows:
        name = r["inst_name"] or "(기관 없음)"
        if r["ref_date"] and r["ref_date"] > ref_dates.get(name, ""):
            ref_dates[name] = r["ref_date"]
    speed_count = sum(r["is_speed"] for r in rows)

    db_path = os.path.join(out_dir, "cameras.db")
    meta = {
        "schema_version": str(SCHEMA_VERSION),
        "version": version,
        "generated_at": run["run_at"],
        "source_counts": {"data.go.kr": count, "한국도로공사_구간단속현황": len(sections)},
        "dropped_coords": str(dropped),
        "ref_dates_by_institution": dict(sorted(ref_dates.items())),
        "attribution": ATTRIBUTION,
    }
    write_db(db_path, rows, meta, sections)

    # 증분 (지난 게시본이 있고 바뀐 행이 많지 않을 때만)
    patch_info = None
    patch_path = os.path.join(out_dir, PATCH_FILE)
    if os.path.exists(patch_path):
        os.remove(patch_path)
    upsert_keys = {k for k in new_hashes if prev_hashes.get(k) != new_hashes[k]}
    delete_keys = prev_hashes.keys() - new_hashes.keys()
    if prev_version and prev_hashes and prev_version.get("version") and \
            len(upsert_keys) + len(delete_keys) <= PATCH_MAX_ROWS:
        patch_info = write_patch(patch_path, prev_version["version"], version, rows, upsert_keys,
                                 delete_keys, sections, meta)

    changes = {"version": version, "previous": (prev_version or {}).get("version"),
               "added": added, "removed": removed, "changed": changed}
    write_json(os.path.join(out_dir, "changes.json"), changes)
    write_json(os.path.join(out_dir, "version.json"), {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "generated_at": run["run_at"],
        "checked_at": run["run_at"],
        "count": count,
        "speed_count": speed_count,
        "data_hash": data_hash,
        "db_sha256": sha256_file(db_path),
        "db_size": os.path.getsize(db_path),
        "added": added, "removed": removed, "changed": changed,
        "sections": len(sections), "sections_matched": section_matched,
        "patch": patch_info,
        "attribution": ATTRIBUTION,
    })
    run["version"] = version
    return finish("published", f"{count}건 (추가 {added}, 삭제 {removed}, 변경 {changed})", 0)


def main() -> int:
    if len(sys.argv) not in (4, 5):
        print(__doc__)
        return 64
    return build(*sys.argv[1:5])


if __name__ == "__main__":
    sys.exit(main())
