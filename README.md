# elecmuskdata

전국 무인 교통단속 카메라 데이터를 매일 자동으로 받아 정리하고, SQLite 파일로 게시하는 저장소입니다.
ElecMusk 앱(개인용 Tesla 앱)의 과속 카메라 알림에 사용합니다.

## 데이터 출처 (필수 표시)

- **공공데이터포털(data.go.kr) 「전국무인교통단속카메라표준데이터」**
- 이용허락범위: **저작자표시**
- 구간단속: **공공데이터포털 「한국도로공사_구간단속현황」** (CSV, 이용허락범위 제한 없음)
- 이 저장소의 데이터는 원본을 정제·가공한 것이며, 원본과 다를 수 있습니다. 실제 단속 여부를 보장하지 않습니다.

## 받는 곳

Release [`cameras-latest`](../../releases/tag/cameras-latest)

| 파일 | 내용 |
|---|---|
| `cameras.db` | SQLite. `camera`(카메라), `section`(고속도로 구간단속), `meta`(버전, 출처, 기관별 기준일자) |
| `version.json` | 버전, 건수, 데이터 해시, DB의 SHA-256과 크기, 변경 건수. 앱은 이것만 먼저 확인 |
| `changes.json` | 지난 게시본 대비 추가·삭제·변경 건수 |

## 동작

1. 매일 05:23(KST)에 GitHub Actions가 실행됩니다. 수동 실행도 가능합니다.
2. API에서 전체를 받습니다 (1,000건씩, 약 45회 호출).
3. 정제합니다: 코드 표기 통일(`01`→`1`), 범위 밖 좌표 제거, 설치장소의 "(A→B)" 방향 문구 추출, 자체 키 부여(관리번호 + 시도 + 좌표).
4. 한국도로공사 구간단속 CSV를 받아 `section`에 넣습니다. 이 데이터에는 좌표가 없어서, 같은 노선에서 설치장소에 적힌 이정(km)이 1km 이내이고 시점/종점과 방향이 맞는 카메라를 시점·종점 카메라로 연결합니다. 연결 못 한 구간도 이정 정보와 함께 남깁니다. CSV를 못 받은 날은 지난 게시본의 구간을 그대로 씁니다.
5. 지난 게시본과 비교해서 바뀐 게 없으면 게시하지 않습니다.
6. 카메라 수가 지난번보다 20% 넘게 줄면 게시하지 않고 실패로 남깁니다.
7. 실행 결과를 `logs/runs.tsv`에 기록합니다 (365일 지난 기록은 자동 삭제).

## 직접 돌려 보기

```
python -m unittest discover -s tests        # 서비스키 없이 테스트
DATA_GO_KR_SERVICE_KEY=... python scripts/fetch.py out/raw.json
python scripts/build.py out/raw.json prev out
```

Python 3.12 표준 라이브러리만 사용합니다. 서비스키는 GitHub Secrets `DATA_GO_KR_SERVICE_KEY`에만 둡니다.
