"""OpenStreetMap 도로로 카메라마다 '이 카메라로 이어지는 길'과 그 둘레 길 연결망을 뽑아 roads2.db 를 만든다
(ElecMusk 기획서 4.1 4-6, 형식 2).

사용: python scripts/roads.py OSM_PBF CAMERAS_DB OUT_DIR

만드는 것
  OUT_DIR/roads2.db     길 연결망 (카메라 길 + 그 둘레 약 50~100m 의 다른 길). 앱이 지도 맞추기(위치를 길에 붙이기)에 쓴다.
                        edge(id, u, v, flags, lanes, geom)  간선 하나 = 교차점 사이 길 한 토막.
                          u, v: 양 끝 교차점 번호. geom: u→v 순서 좌표 (Encoded Polyline 1e-5, 첫 점은 절대값).
                          flags: 0~3비트 길 등급(RANK), 4비트 u→v 통행 가능, 5비트 v→u 통행 가능, 6비트 터널, 7비트 다리·고가.
                          lanes: OSM 차로 수 (없으면 0).
                        tile(t, edges)       타일(TILE도, 약 550m) 번호 gy*100000+gx → 그 타일을 지나는 간선 id (오름차순 차이값 varint).
                        approach(key, edges) 카메라 키 → 차가 달려 그 카메라에 닿는 방향 간선 (id*2 + 방향(0: u→v, 1: v→u),
                                             오름차순 차이값 varint).
                        meta: 버전, OSM 기준 시각, 출처, 거리 예산, 형식.
  OUT_DIR/roads2.json   버전, 건수, 크기, SHA-256 (앱은 이것만 먼저 확인)

방법
  1. 차가 다니는 길(highway=motorway~residential, *_link, living_street)만, 카메라 근처(약 2km)만 읽는다. 회전 제한(restriction)도 읽는다.
  2. 길을 교차점에서 잘라 간선으로 만든다 (oneway, 고속도로·회전교차로는 한 방향).
  3. 카메라에서 25m 안의 길에 붙인다. 가장 가까운 길보다 15m 안쪽이면 여럿에 붙인다
     (상하행이 따로 그려진 길에서 카메라 좌표가 반대쪽에 가까워도 놓치지 않게. 반대 차로는 앱의 4-1이 거른다).
  4. 붙인 간선에서 거꾸로, 차가 달려서 그 지점에 닿는 간선을 거리 예산만큼 모은다 (고속도로 1200m, 일반 600m, 어린이보호 400m).
     카메라 길보다 한 등급 아래 길까지만. 회전 제한(좌회전 금지 등)으로 들어올 수 없는 길과 유턴은 넣지 않는다.
     카메라가 어느 방향을 찍는지 모르므로 양방향 길이면 양쪽에서 오는 길을 다 넣는다
     (길 옆 어느 쪽에 있는지로 방향을 고르는 방법은 OSM 방향 표시와 79%만 맞아 쓰지 않는다, 기획서 4.1 4-6 ⑧).
  5. 카메라 길 둘레 FINE 격자 ±1칸 안의 다른 길도 넣는다 (앱이 "카메라 길과 다른 길 중 어디에 더 가까운가"로 판단).
  25m 안에 길이 없는 카메라는 approach 에 넣지 않는다 (앱이 지금 방식으로 판단).
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
FORMAT = 2
DRIVE = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link", "living_street", "road",
}
# 거리 예산 (m): 앱의 가장 먼 알림 거리 + 여유 (기획서 4.1: 고속도로 1000, 일반 500, 어린이보호 300)
BUDGET = {"highway": 1200.0, "general": 600.0, "school": 400.0}
# 길 등급: 링크는 본선과 같은 등급. 카메라 길보다 한 등급 아래까지만 거꾸로 따라간다
# (시내 골목까지 다 넣으면 골목에서 큰길 카메라를 알리게 됨)
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
FINE = 0.0005        # 둘레 길 고르기 격자 (약 50m), 카메라 길 칸 ±1칸
TILE = 0.005         # 앱이 읽는 타일 (약 550m)
ATTRIBUTION = "도로: © OpenStreetMap contributors (ODbL 1.0, https://www.openstreetmap.org/copyright)"
E7 = 10_000_000
DB_NAME = "roads2.db"
JSON_NAME = "roads2.json"


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

def parse_lanes(v):
    try:
        return max(0, min(15, int(float(v.split(";")[0]))))
    except (ValueError, AttributeError):
        return 0


class Roads(osmium.SimpleHandler if osmium else object):
    def __init__(self, area: set[tuple[int, int]]):
        super().__init__()
        self.keep = area
        # (way id, nodes array q, lat array i, lon array i, fwd, bwd, highway, lanes, tunnel, bridge)
        self.ways = []
        self.restrictions = []   # (from way, via node, to way, "no"|"only")
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
        fwd, bwd = direction_of(w.tags.get("oneway"), hw, w.tags.get("junction"))
        self.ways.append((
            w.id,
            array.array("q", (r for r, _, _ in locs)),
            array.array("i", (round(la * E7) for _, la, _ in locs)),
            array.array("i", (round(lo * E7) for _, _, lo in locs)),
            fwd, bwd, hw,
            parse_lanes(w.tags.get("lanes")),
            w.tags.get("tunnel") not in (None, "no"),
            w.tags.get("bridge") not in (None, "no") or parse_lanes(w.tags.get("layer")) > 0,   # 위로 올라간 길 (layer 1 이상)
        ))

    def relation(self, r):
        if r.tags.get("type") != "restriction":
            return
        kind = r.tags.get("restriction") or r.tags.get("restriction:motorcar")
        if not kind or "motorcar" in (r.tags.get("except") or ""):
            return
        kind = "no" if kind.startswith("no_") else "only" if kind.startswith("only_") else None
        if kind is None:
            return
        frm = via = to = None
        for m in r.members:
            if m.role == "from" and m.type == "w":
                frm = m.ref
            elif m.role == "via" and m.type == "n":
                via = m.ref
            elif m.role == "to" and m.type == "w":
                to = m.ref
        if frm and via and to:   # via 가 길(way)인 제한은 드물고 복잡해서 뺀다
            self.restrictions.append((frm, via, to, kind))


def direction_of(ow, hw, junction):
    oneway = ow in ("yes", "true", "1") or hw == "motorway" or junction in ("roundabout", "circular")
    if ow == "no":
        oneway = False
    if ow == "-1":
        return False, True
    if oneway:
        return True, False
    return True, True


# ---------- 2. 간선 ----------

class Graph:
    def __init__(self, ways, restrictions=()):
        use = defaultdict(int)
        for _, nodes, *_ in ways:
            for r in nodes:
                use[r] += 1
            use[nodes[0]] += 1
            use[nodes[-1]] += 1
        # 방향 간선: (시작 노드, 끝 노드, 좌표 [(lat, lon)], 길이, 방향 없는 간선 번호, 등급, way id)
        self.edges = []
        # 방향 없는 간선: (좌표, 방향 간선 번호 (정, 역), 등급, way id, 시작 노드, 끝 노드, 차로, 터널, 다리)
        self.base = []
        self.incoming = defaultdict(list)
        for wid, nodes, lats, lons, fwd, bwd, hw, lanes, tunnel, bridge in ways:
            start = 0
            for i in range(1, len(nodes)):
                if use[nodes[i]] >= 2 or i == len(nodes) - 1:
                    pts = [(lats[k] / E7, lons[k] / E7) for k in range(start, i + 1)]
                    length = sum(meters(*pts[k - 1], *pts[k]) for k in range(1, len(pts)))
                    b = len(self.base)
                    ids = [None, None]
                    rank = RANK[hw]
                    if fwd:
                        ids[0] = self._add(nodes[start], nodes[i], pts, length, b, rank, wid)
                    if bwd:
                        ids[1] = self._add(nodes[i], nodes[start], pts[::-1], length, b, rank, wid)
                    self.base.append((pts, ids, rank, wid, nodes[start], nodes[i], lanes, tunnel, bridge))
                    start = i
        # 회전 제한: no = {(from, via, to)}, only = {(from, via): {to}}
        self.no_turn = set()
        self.only_turn = defaultdict(set)
        for frm, via, to, kind in restrictions:
            if kind == "no":
                self.no_turn.add((frm, via, to))
            else:
                self.only_turn[(frm, via)].add(to)
        # 붙이기용 선분 격자
        self.cells = defaultdict(list)
        for b, (pts, *_) in enumerate(self.base):
            for k in range(1, len(pts)):
                (a1, o1), (a2, o2) = pts[k - 1], pts[k]
                for cy in range(int(min(a1, a2) / SEG_CELL), int(max(a1, a2) / SEG_CELL) + 1):
                    for cx in range(int(min(o1, o2) / SEG_CELL), int(max(o1, o2) / SEG_CELL) + 1):
                        self.cells[(cy, cx)].append((b, k))

    def _add(self, u, v, pts, length, b, rank, wid):
        e = len(self.edges)
        self.edges.append((u, v, pts, length, b, rank, wid))
        self.incoming[v].append(e)
        return e

    def turn_allowed(self, from_way, via, to_way):
        if (from_way, via, to_way) in self.no_turn:
            return False
        only = self.only_turn.get((from_way, via))
        return only is None or to_way in only

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

def approach(g: Graph, lat, lon, budget):
    """차가 달려 카메라에 닿는 방향 간선 번호 집합 (g.edges 번호)."""
    out = set()
    snaps = g.snaps(lat, lon)
    if not snaps:
        return out
    min_rank = max(g.base[b][2] for b, *_ in snaps) - 1
    for b, k, t, _ in snaps:
        pts, ids, *_ = g.base[b]
        p = (pts[k - 1][0] + (pts[k][0] - pts[k - 1][0]) * t, pts[k - 1][1] + (pts[k][1] - pts[k - 1][1]) * t)
        for direction, e in enumerate(ids):
            if e is None:
                continue
            out.add(e)
            # 간선 시작 → 붙인 지점 거리
            head = pts[:k] + [p] if direction == 0 else pts[::-1][: len(pts) - k] + [p]
            s = sum(meters(*head[i - 1], *head[i]) for i in range(1, len(head)))
            if s >= budget:
                continue
            # 거꾸로 거리 예산만큼. 상태 = 방향 간선 (그 간선 시작점에 도착, 남은 거리): 회전 제한을 보려면 어느 길에서 들어오는지 알아야 함
            best = {e: budget - s}
            heap = [(-(budget - s), e)]
            while heap:
                r, cur = heapq.heappop(heap)
                r = -r
                if r < best.get(cur, -1):
                    continue
                u, _, _, _, cb, _, cwid = g.edges[cur]
                for pe in g.incoming.get(u, ()):
                    _, _, _, length, pb, rank, pwid = g.edges[pe]
                    # 유턴(같은 토막으로 되돌아옴), 낮은 등급, 회전 제한은 넣지 않는다
                    if pb == cb or rank < min_rank or not g.turn_allowed(pwid, u, cwid):
                        continue
                    if r > 5:
                        out.add(pe)
                    left = r - length
                    if left > 0 and left > best.get(pe, -1):
                        best[pe] = left
                        heapq.heappush(heap, (-left, pe))
    return out


def nearby(g: Graph, used_bases):
    """카메라 길 토막 + 그 둘레 FINE 격자 ±1칸 안의 다른 토막."""
    grid = defaultdict(list)
    for b, (pts, *_) in enumerate(g.base):
        for c in cells_of(pts, FINE):
            grid[c].append(b)
    out = set(used_bases)
    for b in used_bases:
        for (cy, cx) in cells_of(g.base[b][0], FINE):
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    out.update(grid.get((cy + dy, cx + dx), ()))
    return out


def cells_of(pts, size):
    s = set()
    for (a1, o1), (a2, o2) in zip(pts, pts[1:]):
        n = max(1, int(meters(a1, o1, a2, o2) / (size * 50_000)))   # 격자 반 칸 간격으로 훑음
        for j in range(n + 1):
            f = j / n
            s.add((int(math.floor((a1 + (a2 - a1) * f) / size)), int(math.floor((o1 + (o2 - o1) * f) / size))))
    return s


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


def encode(pts, prev=None):
    """Encoded Polyline (1e-5). prev 가 없으면 첫 점은 절대값."""
    out = []
    plat, plon = prev if prev else (0, 0)
    for la, lo in pts:
        ila, ilo = round(la * 1e5), round(lo * 1e5)
        for v in (ila - plat, ilo - plon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ila, ilo
    return "".join(out)


def varints(values):
    """오름차순 정수 → 차이값 varint (7비트씩, 높은 비트 = 계속)."""
    out = bytearray()
    prev = 0
    for v in sorted(values):
        d = v - prev
        prev = v
        while d >= 0x80:
            out.append((d & 0x7F) | 0x80)
            d >>= 7
        out.append(d)
    return bytes(out)


def tile_of(lat, lon):
    return int(math.floor(lat / TILE)) * 100000 + int(math.floor(lon / TILE))


def flags_of(g: Graph, b):
    pts, ids, rank, _, _, _, _, tunnel, bridge = g.base[b]
    return rank | ((ids[0] is not None) << 4) | ((ids[1] is not None) << 5) | (bool(tunnel) << 6) | (bool(bridge) << 7)


def write_db(g: Graph, appr: dict, path: str, meta: dict):
    """appr: 카메라 키 → g.edges 번호 집합."""
    used = {g.edges[e][4] for es in appr.values() for e in es}
    keep = nearby(g, used)
    # 간선은 타일 순서로 번호를 다시 매긴다 (같은 타일 간선이 가까운 번호 → 차이값이 짧음)
    order = sorted(keep, key=lambda b: (tile_of(*g.base[b][0][0]), b))
    newid = {b: i for i, b in enumerate(order)}
    node = {}
    tiles = defaultdict(set)
    rows = []
    points = 0
    for b in order:
        pts, ids, rank, wid, u, v, lanes, tunnel, bridge = g.base[b]
        sp = simplify(pts)
        points += len(sp)
        rows.append((newid[b], node.setdefault(u, len(node)), node.setdefault(v, len(node)), flags_of(g, b), lanes, encode(sp)))
        for cy, cx in cells_of(sp, TILE):
            tiles[cy * 100000 + cx].add(newid[b])
    if os.path.exists(path):
        os.remove(path)
    out = sqlite3.connect(path)
    out.executescript("""
        CREATE TABLE edge (id INTEGER PRIMARY KEY, u INTEGER NOT NULL, v INTEGER NOT NULL, flags INTEGER NOT NULL,
                           lanes INTEGER NOT NULL, geom TEXT NOT NULL);
        CREATE TABLE tile (t INTEGER PRIMARY KEY, edges BLOB NOT NULL);
        CREATE TABLE approach (key TEXT PRIMARY KEY, edges BLOB NOT NULL);
        CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    out.executemany("INSERT INTO edge VALUES (?, ?, ?, ?, ?, ?)", rows)
    out.executemany("INSERT INTO tile VALUES (?, ?)", ((t, varints(s)) for t, s in tiles.items()))

    def ref(e):
        _, _, _, _, b, _, _ = g.edges[e]
        return newid[b] * 2 + (0 if g.base[b][1][0] == e else 1)

    out.executemany("INSERT INTO approach VALUES (?, ?)", ((k, varints(ref(e) for e in es)) for k, es in appr.items()))
    meta = dict(meta, edges=str(len(rows)), tile_deg=str(TILE))
    out.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    out.commit()
    out.execute("VACUUM")
    out.close()
    return len(rows), points


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
    print(f"길 {h.seen:,}개 중 카메라 근처 {len(h.ways):,}개, 회전 제한 {len(h.restrictions):,}개", flush=True)
    g = Graph(h.ways, h.restrictions)
    del h
    print(f"간선 {len(g.base):,}개, 방향 간선 {len(g.edges):,}개", flush=True)

    appr = {}
    for key, la, lo, limit, kind in cams:
        es = approach(g, la, lo, BUDGET[road_class(limit, kind)])
        if es:
            appr[key] = es
    osm_time = osmium.io.Reader(pbf, osmium.osm.osm_entity_bits.NOTHING).header().get("osmosis_replication_timestamp", "")
    version = datetime.now(KST).strftime("%Y%m%d.%H%M")
    meta = {
        "format": str(FORMAT), "version": version, "osm_time": osm_time, "attribution": ATTRIBUTION,
        "budget_highway": str(BUDGET["highway"]), "budget_general": str(BUDGET["general"]), "budget_school": str(BUDGET["school"]),
        "snap_m": str(SNAP_M),
    }
    db_path = os.path.join(out_dir, DB_NAME)
    edges, points = write_db(g, appr, db_path, meta)
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
            "count": len(appr), "cameras": len(cams), "edges": edges, "points": points,
            "db_size": size, "db_sha256": sha, "gz_size": os.path.getsize(gz_path), "attribution": ATTRIBUTION}
    json.dump(info, open(os.path.join(out_dir, JSON_NAME), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(info), flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:4])
