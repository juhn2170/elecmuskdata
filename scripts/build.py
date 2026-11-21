"""내려받은 원본(raw.json)을 정제해서 cameras.db, version.json, changes.json, run.json 을 만든다.

사용: python scripts/build.py RAW_JSON PREV_DIR OUT_DIR
  PREV_DIR: 지난번 Release의 cameras.db, version.json 이 있는 폴더 (없으면 첫 게시)

종료 코드
  0  게시할 새 데이터 있음 (run.json status=published)
  0  바뀐 것 없음        (run.json status=unchanged, 게시 안 함)
  2  검사 실패           (run.json status=blocked, 게시 안 함)
"""
from __future__ import annotations

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
    "이용허락범위: 저작자표시. 원본을 정제·가공한 데이터임."
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
  start_key TEXT, end_key TEXT, length_km REAL, limit_kmh INTEGER, source TEXT
);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""


def write_db(path: str, rows: list[dict], meta: dict) -> None:
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    try:
        con.executescript(SCHEMA)
        con.executemany(
            f"INSERT INTO camera ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
            [[r[c] for c in COLUMNS] for r in rows],
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


def write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------- 실행 ----------

def build(raw_path: str, prev_dir: str, out_dir: str, now: datetime | None = None) -> int:
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

    new_hashes = {r["key"]: row_hash({c: r[c] for c in COLUMNS}) for r in rows}
    data_hash = hashlib.sha256("\n".join(f"{k}:{v}" for k, v in sorted(new_hashes.items()))
                               .encode("utf-8")).hexdigest()

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
        return finish("unchanged", f"{count}건, 바뀐 것 없음", 0)

    version = now.strftime("%Y%m%d.%H%M")
    ref_dates: dict[str, str] = {}
    for r in rows:
        name = r["inst_name"] or "(기관 없음)"
        if r["ref_date"] and r["ref_date"] > ref_dates.get(name, ""):
            ref_dates[name] = r["ref_date"]
    speed_count = sum(r["is_speed"] for r in rows)

    db_path = os.path.join(out_dir, "cameras.db")
    write_db(db_path, rows, {
        "schema_version": str(SCHEMA_VERSION),
        "version": version,
        "generated_at": run["run_at"],
        "source_counts": {"data.go.kr": count},
        "dropped_coords": str(dropped),
        "ref_dates_by_institution": dict(sorted(ref_dates.items())),
        "attribution": ATTRIBUTION,
    })

    changes = {"version": version, "previous": (prev_version or {}).get("version"),
               "added": added, "removed": removed, "changed": changed}
    write_json(os.path.join(out_dir, "changes.json"), changes)
    write_json(os.path.join(out_dir, "version.json"), {
        "schema_version": SCHEMA_VERSION,
        "version": version,
        "generated_at": run["run_at"],
        "count": count,
        "speed_count": speed_count,
        "data_hash": data_hash,
        "db_sha256": sha256_file(db_path),
        "db_size": os.path.getsize(db_path),
        "added": added, "removed": removed, "changed": changed,
        "attribution": ATTRIBUTION,
    })
    run["version"] = version
    return finish("published", f"{count}건 (추가 {added}, 삭제 {removed}, 변경 {changed})", 0)


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 64
    return build(*sys.argv[1:4])


if __name__ == "__main__":
    sys.exit(main())
