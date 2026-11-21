"""OpenStreetMap 도로로 카메라마다 '이 카메라로 이어지는 길'을 뽑아 roads.db 를 만든다 (ElecMusk 기획서 4.1 4-6).

사용: python scripts/roads.py OSM_PBF CAMERAS_DB OUT_DIR

만드는 것
  OUT_DIR/roads.db      approach(key, polys): 카메라 키 → 그 카메라에 닿는 도로 조각들 (진행 방향 순서, Encoded Polyline 1e-5,
                        조각끼리는 공백으로 구분). meta: 버전, OSM 기준 시각, 출처, 거리 예산.
  OUT_DIR/roads.json    버전, 건수, 크기, SHA-256 (앱은 이것만 먼저 확인)

방법
  1. 차가 다니는 길(highway=motorway~residential, *_link, living_street)만, 카메라 근처(약 2km)만 읽는다.
  2. 길을 교차점에서 잘라 방향 있는 간선으로 만든다 (oneway, 고속도로·회전교차로는 한 방향).
  3. 카메라에서 25m 안의 길에 붙인다. 가장 가까운 길보다 15m 안쪽이면 여럿에 붙인다
     (상하행이 따로 그려진 길에서 카메라 좌표가 반대쪽에 가까워도 놓치지 않게. 반대 차로는 앱의 4-1이 거른다).
  4. 붙인 지점에서 거꾸로, 차가 달려서 그 지점에 닿는 길을 거리 예산만큼 모은다 (고속도로 1200m, 일반 600m, 어린이보호 400m).
     카메라가 어느 방향을 찍는지 모르므로 양방향 길이면 양쪽에서 오는 길을 다 넣는다.
  25m 안에 길이 없는 카메라는 넣지 않는다 (앱이 지금 방식으로 판단).
"""
from __future__ import annotations

import array
import gzip
import shutil
import hashlib
import heapq
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    import osmium   # pip install osmium (pyosmium). 테스트는 없어도 돈다 (Graph·approach 만 씀)
except ImportError:  # pragma: no cover
    osmium = None

KST = timezone(timedelta(hours=9))
FORMAT = 1
DRIVE = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link", "living_street", "road",
}
# 거리 예산 (m): 앱의 가장 먼 알림 거리 + 여유 (기획서 4.1: 고속도로 1000, 일반 500, 어린이보호 300)
BUDGET = {"highway": 1200.0, "general": 600.0, "school": 400.0}
# 길 등급: 링크는 본선과 같은 등급. 카메라 길보다 한 등급 아래까지만 거꾸로 따라간다
# (시내 골목까지 다 넣으면 파일이 커지고, 골목에서 큰길 카메라를 알리게 됨)
RANK = {
    "motorway": 7, "motorway_link": 7, "trunk": 6, "trunk_link": 6, "primary": 5, "primary_link": 5,
    "secondary": 4, "secondary_link": 4, "tertiary": 3, "tertiary_link": 3,
    "unclassified": 2, "road": 2, "residential": 1, "living_street": 0,
}
SNAP_M = 25.0
SNAP_EXTRA_M = 15.0
SNAP_MAX = 4
SIMPLIFY_M = 3.0
CELL = 0.01          # 카메라 주변 영역 격자 (약 1km)
AREA_CELLS = 2       # 카메라 칸에서 이만큼 둘레까지 읽는다
SEG_CELL = 0.002     # 붙이기용 선분 격자 (약 200m)
ATTRIBUTION = "도로: © OpenStreetMap contributors (ODbL 1.0, https://www.openstreetmap.org/copyright)"
E7 = 10_000_000


def road_class(limit: int, kind: str | None) -> str:
    if 1 <= limit <= 30:
        return "school"
    if kind == "고속국도" or limit >= 90:
        return "highway"
    return "general"


def meters(lat1, lon1, lat2, lon2):
    k = 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon2 - lon1) * k, (lat2 - lat1) * 110_540.0)


# ---------- 1. 읽기 ----------

class Roads(osmium.SimpleHandler if osmium else object):
    def __init__(self, area: set[tuple[int, int]]):
        super().__init__()
        self.keep = area
        self.ways = []   # (nodes array q, lat array i, lon array i, fwd, bwd, highway)
        self.seen = 0

    def way(self, w):
        hw = w.tags.get("highway")
        if hw not in DRIVE:
            return
        if w.tags.get("area") == "yes" or w.tags.get("access") in ("no", "private"):
            return
        self.seen += 1
        try:
            locs = [(n.ref, n.location.lat, n.location.lon) for n in w.nodes]
        except osmium.InvalidLocationError:
            locs = [(n.ref, n.location.lat, n.location.lon) for n in w.nodes if n.location.valid()]
        if len(locs) < 2:
            return
        if not any((int(la / CELL), int(lo / CELL)) in self.keep for _, la, lo in locs):
            return
        ow = w.tags.get("oneway")
        oneway = ow in ("yes", "true", "1") or hw == "motorway" or w.tags.get("junction") in ("roundabout", "circular")
        if ow == "no":
            oneway = False
        if ow == "-1":
            fwd, bwd = False, True
        elif oneway:
            fwd, bwd = True, False
        else:
            fwd, bwd = True, True
        self.ways.append((
            array.array("q", (r for r, _, _ in locs)),
            array.array("i", (round(la * E7) for _, la, _ in locs)),
            array.array("i", (round(lo * E7) for _, _, lo in locs)),
            fwd, bwd, hw,
        ))


# ---------- 2. 간선 ----------

class Graph:
    def __init__(self, ways):
        use = defaultdict(int)
        for nodes, *_ in ways:
            for r in nodes:
                use[r] += 1
            use[nodes[0]] += 1
            use[nodes[-1]] += 1
        # 방향 간선: (시작 노드, 끝 노드, 좌표 [(lat, lon)], 길이, 원래 간선 번호)
        self.edges = []
        self.base = []           # 방향 없는 간선: (좌표, 방향 간선 번호 (정, 역))
        self.incoming = defaultdict(list)
        for nodes, lats, lons, fwd, bwd, hw in ways:
            start = 0
            for i in range(1, len(nodes)):
                if use[nodes[i]] >= 2 or i == len(nodes) - 1:
                    pts = [(lats[k] / E7, lons[k] / E7) for k in range(start, i + 1)]
                    length = sum(meters(*pts[k - 1], *pts[k]) for k in range(1, len(pts)))
                    b = len(self.base)
                    ids = [None, None]
                    rank = RANK[hw]
                    if fwd:
                        ids[0] = self._add(nodes[start], nodes[i], pts, length, b, rank)
                    if bwd:
                        ids[1] = self._add(nodes[i], nodes[start], pts[::-1], length, b, rank)
                    self.base.append((pts, ids, rank))
                    start = i
        # 붙이기용 선분 격자
        self.cells = defaultdict(list)
        for b, (pts, _, _) in enumerate(self.base):
            for k in range(1, len(pts)):
                (a1, o1), (a2, o2) = pts[k - 1], pts[k]
                for cy in range(int(min(a1, a2) / SEG_CELL), int(max(a1, a2) / SEG_CELL) + 1):
                    for cx in range(int(min(o1, o2) / SEG_CELL), int(max(o1, o2) / SEG_CELL) + 1):
                        self.cells[(cy, cx)].append((b, k))

    def _add(self, u, v, pts, length, b, rank):
        e = len(self.edges)
        self.edges.append((u, v, pts, length, b, rank))
        self.incoming[v].append(e)
        return e

    def snaps(self, lat, lon):
        """카메라에서 SNAP_M 안의 길: [(방향 없는 간선, 선분 k, 선분 위 비율 t, 거리)] 가까운 순, 간선마다 하나."""
        cy, cx = int(lat / SEG_CELL), int(lon / SEG_CELL)
        kx = 111_320.0 * math.cos(math.radians(lat))
        best = {}
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for b, k in self.cells.get((cy + dy, cx + dx), ()):
                    (a1, o1), (a2, o2) = self.base[b][0][k - 1], self.base[b][0][k]
                    x1, y1 = (o1 - lon) * kx, (a1 - lat) * 110_540.0
                    x2, y2 = (o2 - lon) * kx, (a2 - lat) * 110_540.0
                    vx, vy = x2 - x1, y2 - y1
                    L2 = vx * vx + vy * vy
                    t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(x1 * vx + y1 * vy) / L2))
                    d = math.hypot(x1 + t * vx, y1 + t * vy)
                    if d <= SNAP_M and (b not in best or d < best[b][3]):
                        best[b] = (b, k, t, d)
        out = sorted(best.values(), key=lambda s: s[3])
        if not out:
            return []
        return [s for s in out if s[3] <= out[0][3] + SNAP_EXTRA_M][:SNAP_MAX]


# ---------- 3. 카메라로 이어지는 길 ----------

def cut_tail(pts, keep):
    """좌표 목록의 끝에서 keep m 만큼만."""
    out = [pts[-1]]
    left = keep
    for k in range(len(pts) - 1, 0, -1):
        seg = meters(*pts[k - 1], *pts[k])
        if seg >= left:
            f = left / seg if seg > 0 else 0
            a, b = pts[k], pts[k - 1]
            out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
            break
        out.append(pts[k - 1])
        left -= seg
    return out[::-1]


def approach(g: Graph, lat, lon, budget):
    pieces = []
    used = set()
    snaps = g.snaps(lat, lon)
    if not snaps:
        return pieces
    min_rank = max(g.base[b][2] for b, *_ in snaps) - 1
    for b, k, t, _ in snaps:
        pts, ids, _ = g.base[b]
        p = (pts[k - 1][0] + (pts[k][0] - pts[k - 1][0]) * t, pts[k - 1][1] + (pts[k][1] - pts[k - 1][1]) * t)
        for direction, e in enumerate(ids):
            if e is None:
                continue
            # 방향 간선 시작 → 붙인 지점
            head = pts[:k] + [p] if direction == 0 else pts[::-1][: len(pts) - k] + [p]
            s = sum(meters(*head[i - 1], *head[i]) for i in range(1, len(head)))
            if s >= budget:
                pieces.append(cut_tail(head, budget))
                continue
            pieces.append(head)
            # 거꾸로 거리 예산만큼
            start = g.edges[e][0]
            best = {start: budget - s}
            heap = [(-(budget - s), start)]
            while heap:
                r, node = heapq.heappop(heap)
                r = -r
                if r < best.get(node, -1):
                    continue
                for pe in g.incoming.get(node, ()):
                    if pe in used:
                        continue
                    u, _, ppts, length, pb, rank = g.edges[pe]
                    # 붙인 길 자체로 되돌아가는 것(막다른 길 끝의 유턴)은 넣지 않는다
                    if pb == b or rank < min_rank:
                        continue
                    if length <= r:
                        used.add(pe)
                        pieces.append(ppts)
                        left = r - length
                        if left > best.get(u, -1):
                            best[u] = left
                            heapq.heappush(heap, (-left, u))
                    elif r > 5:
                        pieces.append(cut_tail(ppts, r))
    return pieces


# ---------- 줄이기·부호화 ----------

def simplify(pts, tol=SIMPLIFY_M):
    if len(pts) <= 2:
        return pts
    lat0 = pts[0][0]
    kx = 111_320.0 * math.cos(math.radians(lat0))
    xy = [((lo - pts[0][1]) * kx, (la - lat0) * 110_540.0) for la, lo in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        vx, vy = x2 - x1, y2 - y1
        L = math.hypot(vx, vy)
        far, fk = -1.0, -1
        for k in range(i + 1, j):
            x, y = xy[k]
            d = abs(vx * (y1 - y) - vy * (x1 - x)) / L if L > 0 else math.hypot(x - x1, y - y1)
            if d > far:
                far, fk = d, k
        if far > tol:
            keep[fk] = True
            stack += [(i, fk), (fk, j)]
    return [p for p, k in zip(pts, keep) if k]


def encode_all(pieces):
    """조각들을 Encoded Polyline 으로. 차이값은 조각을 넘어 이어진다 (조각 첫 점도 앞 조각 끝에서의 차이라 짧다). 조각 사이는 공백."""
    out = []
    prev = [0, 0]
    for p in pieces:
        out.append(encode(p, prev))
    return " ".join(out)


def encode(pts, prev):
    out = []
    plat, plon = prev
    for la, lo in pts:
        ila, ilo = round(la * 1e5), round(lo * 1e5)
        for v in (ila - plat, ilo - plon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ila, ilo
    prev[0], prev[1] = plat, plon
    return "".join(out)


# ---------- main ----------

def main(pbf, cameras_db, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    con = sqlite3.connect(cameras_db)
    cams = con.execute("SELECT key, lat, lon, limit_kmh, road_kind FROM camera WHERE limit_kmh > 0").fetchall()
    con.close()
    area = set()
    for _, la, lo, _, _ in cams:
        cy, cx = int(la / CELL), int(lo / CELL)
        for dy in range(-AREA_CELLS, AREA_CELLS + 1):
            for dx in range(-AREA_CELLS, AREA_CELLS + 1):
                area.add((cy + dy, cx + dx))
    h = Roads(area)
    h.apply_file(pbf, locations=True, idx="flex_mem")
    print(f"길 {h.seen:,}개 중 카메라 근처 {len(h.ways):,}개", flush=True)
    g = Graph(h.ways)
    del h
    print(f"간선 {len(g.base):,}개, 방향 간선 {len(g.edges):,}개", flush=True)

    db_path = os.path.join(out_dir, "roads.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    out = sqlite3.connect(db_path)
    out.executescript("""
        CREATE TABLE approach (key TEXT PRIMARY KEY, polys TEXT NOT NULL);
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    snapped = 0
    points = 0
    for key, la, lo, limit, kind in cams:
        pieces = approach(g, la, lo, BUDGET[road_class(limit, kind)])
        if not pieces:
            continue
        snapped += 1
        simple = [simplify(p) for p in pieces]
        points += sum(len(p) for p in simple)
        out.execute("INSERT INTO approach VALUES (?, ?)", (key, encode_all(simple)))
    osm_time = osmium.io.Reader(pbf, osmium.osm.osm_entity_bits.NOTHING).header().get("osmosis_replication_timestamp", "")
    version = datetime.now(KST).strftime("%Y%m%d.%H%M")
    meta = {
        "format": str(FORMAT), "version": version, "osm_time": osm_time, "attribution": ATTRIBUTION,
        "budget_highway": str(BUDGET["highway"]), "budget_general": str(BUDGET["general"]), "budget_school": str(BUDGET["school"]),
        "snap_m": str(SNAP_M),
    }
    out.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    out.commit()
    out.execute("VACUUM")
    out.close()
    size = os.path.getsize(db_path)
    sha = hashlib.sha256(open(db_path, "rb").read()).hexdigest()
    # 앱은 압축본을 받아 풀고, 푼 파일의 SHA-256 을 확인한다
    gz_path = db_path + ".gz"
    with open(db_path, "rb") as src, gzip.open(gz_path, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst)
    cam_version = ""
    try:
        con = sqlite3.connect(cameras_db)
        cam_version = dict(con.execute("SELECT k, v FROM meta").fetchall()).get("version", "")
        con.close()
    except sqlite3.Error:
        pass
    info = {"format": FORMAT, "version": version, "osm_time": osm_time, "cameras_version": cam_version,
            "count": snapped, "cameras": len(cams), "points": points,
            "db_size": size, "db_sha256": sha, "gz_size": os.path.getsize(gz_path), "attribution": ATTRIBUTION}
    json.dump(info, open(os.path.join(out_dir, "roads.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(info), flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:4])
