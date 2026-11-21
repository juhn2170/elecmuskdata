"""공공데이터포털 전국무인교통단속카메라표준데이터를 전부 내려받아 JSON 파일로 저장한다.

서비스키는 환경 변수 DATA_GO_KR_SERVICE_KEY 에서만 읽는다 (GitHub Secrets).
인코딩된 키(%2B 등 포함)와 디코딩된 키 둘 다 받는다.
키가 로그에 찍히지 않도록 요청 URL은 어디에도 출력하지 않는다.

사용: python scripts/fetch.py out/raw.json
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ENDPOINT = "https://api.data.go.kr/openapi/tn_pubr_public_unmanned_traffic_camera_api"
PAGE_SIZE = 1000          # API 한 페이지 최대
MAX_PAGES = 200           # 안전 장치 (현재 약 45페이지)
RETRIES = 5
TIMEOUT = 60
KST = timezone(timedelta(hours=9))


class FetchError(Exception):
    pass


def service_key() -> str:
    key = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
    if not key:
        raise FetchError("환경 변수 DATA_GO_KR_SERVICE_KEY 가 비어 있음 (GitHub Secrets 등록 필요)")
    if "%" in key:  # 인코딩된 키 → 원래 값으로
        key = urllib.parse.unquote(key)
    return key


def mask(text: str, key: str) -> str:
    """오류 메시지에 키가 섞여 나오지 않게 가린다."""
    for k in {key, urllib.parse.quote(key, safe=""), urllib.parse.quote_plus(key)}:
        if k:
            text = text.replace(k, "***")
    return text


def decode(raw: bytes) -> str:
    """응답이 UTF-8이 아니라 EUC-KR(CP949)로 오는 경우가 있다 (2026-11-21 확인)."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp949", errors="replace")


def get_page(key: str, page: int) -> dict:
    query = urllib.parse.urlencode(
        {"serviceKey": key, "pageNo": page, "numOfRows": PAGE_SIZE, "type": "json"}
    )
    req = urllib.request.Request(
        f"{ENDPOINT}?{query}", headers={"User-Agent": "elecmuskdata-collector"}
    )
    last = ""
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = decode(resp.read())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = f"연결 오류 {type(e).__name__}"
        else:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                # 인증 오류 등은 JSON 요청에도 XML로 오는 경우가 있다.
                last = "JSON 아님: " + mask(body[:300], key).replace("\n", " ")
            else:
                # 실제 응답은 {"header":..,"body":..} (2026-11-21 확인). "response"로 감싼 형태도 대비.
                root = data.get("response", data)
                header = root.get("header", {})
                code = str(header.get("resultCode", ""))
                if code in ("00", "0"):
                    return root["body"]
                if code == "03":  # NODATA
                    return {"items": [], "totalCount": 0}
                last = f"resultCode={code} {header.get('resultMsg', '')}"
        print(f"  {page}페이지 {attempt}회 실패: {last}", file=sys.stderr)
        if attempt < RETRIES:
            time.sleep(2 ** attempt)
    raise FetchError(f"{page}페이지를 {RETRIES}회 시도했지만 실패: {last}")


def items_of(body: dict) -> list[dict]:
    items = body.get("items") or []
    if isinstance(items, dict):  # {"item": [...]} 형태 대비
        items = items.get("item") or []
    if isinstance(items, dict):
        items = [items]
    return list(items)


def fetch_all() -> dict:
    key = service_key()
    first = get_page(key, 1)
    total = int(first.get("totalCount") or 0)
    rows = items_of(first)
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if pages > MAX_PAGES:
        raise FetchError(f"페이지 수 {pages}가 안전 한도 {MAX_PAGES}를 넘음")
    print(f"전체 {total}건, {pages}페이지")
    for page in range(2, pages + 1):
        rows.extend(items_of(get_page(key, page)))
        time.sleep(0.3)  # 서버에 부담 주지 않게
    if len(rows) != total:
        raise FetchError(f"받은 건수 {len(rows)} ≠ totalCount {total}")
    return {
        "fetched_at": datetime.now(KST).isoformat(timespec="seconds"),
        "source": "data.go.kr 전국무인교통단속카메라표준데이터",
        "total_count": total,
        "calls": pages,
        "items": rows,
    }


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 64
    out = sys.argv[1]
    try:
        data = fetch_all()
    except FetchError as e:
        print(f"수집 실패: {e}", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"저장: {out} ({data['total_count']}건, {data['calls']}회 호출)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
