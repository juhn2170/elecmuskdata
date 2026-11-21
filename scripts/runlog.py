"""실행 결과(out/run.json)를 logs/runs.tsv 에 한 줄 추가하고, 365일 지난 줄은 지운다.

run.json 이 없으면 수집 단계에서 실패한 것으로 기록한다.
사용: python scripts/runlog.py OUT_DIR LOG_FILE
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
KEEP_DAYS = 365
HEADER = "run_at\tstatus\tcount\tadded\tremoved\tchanged\tversion\tnote"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 64
    out_dir, log_file = sys.argv[1], sys.argv[2]
    now = datetime.now(KST)
    run_path = os.path.join(out_dir, "run.json")
    if os.path.exists(run_path):
        with open(run_path, encoding="utf-8") as f:
            run = json.load(f)
    else:
        run = {"run_at": now.isoformat(timespec="seconds"), "status": "failed",
               "note": "수집 실패 (Actions 로그 참고)"}

    line = "\t".join(str(run.get(k, "")).replace("\t", " ") for k in
                     ("run_at", "status", "count", "added", "removed", "changed", "version", "note"))

    lines: list[str] = []
    if os.path.exists(log_file):
        with open(log_file, encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f if l.strip() and not l.startswith("run_at\t")]
    cutoff = now - timedelta(days=KEEP_DAYS)
    kept = []
    for l in lines:
        try:
            if datetime.fromisoformat(l.split("\t", 1)[0]) >= cutoff:
                kept.append(l)
        except ValueError:
            kept.append(l)  # 날짜를 못 읽는 줄은 지우지 않는다
    kept.append(line)

    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    with open(log_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(HEADER + "\n" + "\n".join(kept) + "\n")
    print(f"기록: {line}  (삭제 {len(lines) + 1 - len(kept)}줄)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
