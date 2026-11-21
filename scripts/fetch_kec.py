"""한국도로공사_구간단속현황(파일데이터, CSV)을 내려받는다. 서비스키가 필요 없다.

공공데이터포털 상세 화면의 다운로드 버튼과 같은 경로를 쓴다 (2026-11-21 확인):
  1) /tcs/dss/selectFileDataDownload.do 로 첨부파일 ID(atchFileId)를 얻고
  2) /cmm/cmm/fileDownload.do 로 CSV를 받는다.
데이터가 갱신되면 publicDataDetailPk가 바뀔 수 있으므로, 상세 화면에서 매번 새로 찾는다.

사용: python scripts/fetch_kec.py out/kec_sections.csv
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request

PUBLIC_DATA_PK = "15104214"
BASE = "https://www.data.go.kr"
UA = {"User-Agent": "Mozilla/5.0 (elecmuskdata-collector)"}
HEADER = "노선,방향,시점,시점인근시설물,종점,종점인근시설물,연장"


def get(url: str) -> bytes:
    last = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read()
        except OSError as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"다운로드 실패: {type(last).__name__}")


def decode(b: bytes) -> str:
    try:
        return b.decode("utf-8-sig")
    except UnicodeDecodeError:
        return b.decode("cp949")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 64
    page = decode(get(f"{BASE}/data/{PUBLIC_DATA_PK}/fileData.do"))
    m = re.search(r"fn_fileDataDown\('" + PUBLIC_DATA_PK + r"',\s*'([^']+)'", page)
    if not m:
        print("상세 화면에서 다운로드 정보를 찾지 못함", file=sys.stderr)
        return 1
    info = json.loads(get(f"{BASE}/tcs/dss/selectFileDataDownload.do?publicDataPk={PUBLIC_DATA_PK}"
                          f"&publicDataDetailPk={m.group(1)}&fileDetailSn=1"))
    file_id = info.get("atchFileId")
    name = info.get("dataSetFileDetailInfo", {}).get("dataNm", "")
    if not file_id:
        print("첨부파일 ID가 없음", file=sys.stderr)
        return 1
    text = decode(get(f"{BASE}/cmm/cmm/fileDownload.do?atchFileId={file_id}&fileDetailSn=1"))
    first = text.lstrip().splitlines()[0].replace(" ", "") if text.strip() else ""
    if first != HEADER:
        print(f"CSV 머리글이 예상과 다름: {first[:80]}", file=sys.stderr)
        return 1
    with open(sys.argv[1], "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print(f"저장: {sys.argv[1]} ({name}, {len(text.splitlines()) - 1}행)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
