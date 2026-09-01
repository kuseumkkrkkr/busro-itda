# 버스로 잇다

> 전국 시내버스만 이어 대한민국을 여행하는 경로 탐색 웹 앱

[![Live](https://img.shields.io/badge/Live-Vercel-000000?logo=vercel)](https://busro-itda.vercel.app)
[![Python](https://img.shields.io/badge/API-Python-3776AB?logo=python&logoColor=white)](opendesign/mockups/busro-itda-glass/service)
[![OpenStreetMap](https://img.shields.io/badge/Map-OpenStreetMap-7EBC6F?logo=openstreetmap&logoColor=white)](https://www.openstreetmap.org/)

**[웹 앱 열기](https://busro-itda.vercel.app)** · **[서비스/API 문서](opendesign/mockups/busro-itda-glass/service/README.md)**

<p align="center">
  <img src="docs/assets/github-social-preview.png" width="960" alt="버스로 잇다 소셜 미리보기">
</p>

## 핵심 기능

- 전국 정류장·노선 검색과 방향성 Dijkstra 경로 탐색
- 최소환승·균형·여행우선 조건별 대안 경로 생성
- TAGO API 기반 실시간 도착·차량 위치 조회
- 정류장 순서와 OSM 도로망을 이용한 실제 구간 지도
- 공식 시간표·운행 관측 근거가 있을 때만 성공 가능성 계산
- 모바일 우선 흰색 글래스모피즘 UI

<p align="center">
  <img src="docs/assets/journey-detail-mobile.png" width="360" alt="버스로 잇다 모바일 경로 상세 화면">
  <img src="docs/assets/journey-evidence-mobile.png" width="360" alt="버스로 잇다 경로 근거 화면">
</p>

## 데이터 현황

| 항목 | 현재 포함량 |
|---|---:|
| 정류장 카탈로그 | 227,054건 |
| 노선 카탈로그 | 7,239건 |
| 방향성 노선 시퀀스 | 5,247건 |
| 방향성 정류장 행 | 301,379건 |
| 지자체 코드 | 138개 |
| 배포 카탈로그 | SQLite 160.9 MiB / 압축 39.0 MiB |

정적 카탈로그는 국토교통부·TAGO 계열 식별자와 수집된 방향 순서를 보존합니다. 시간표나 관측 근거가 부족하면 앱은 값을 추정하지 않고 `DATA_GAP`으로 표시합니다. 과거 GTFS는 현재 시간표를 대신하지 않으며 조발·연착 신뢰도 모델의 근거로만 사용합니다.

## 배포 구조

```text
Browser
  ├─ Vercel static UI
  ├─ /api/* → Python serverless function
  ├─ GitHub Release tar.gz → SHA-256 검증 → /tmp SQLite
  ├─ TAGO API → 실시간 도착·차량 위치
  └─ OSM/OSRM → 노선 구간 지도
```

Vercel에서는 고정 버전 압축 자산의 크기·압축 SHA-256·해제 SHA-256·SQLite 헤더를 확인한 뒤 임시 디렉터리에서 읽습니다. 원본 SQLite와 서비스키는 Git에 넣지 않습니다. 서버리스 파일시스템은 영속 저장소가 아니므로 관측 이력 수집·노선 갱신 API는 현재 차단되어 있습니다.

경로 결과는 카탈로그 revision을 키에 포함한 bounded LRU와 singleflight로 합칩니다. 동일 warm instance의 반복 요청과 같은 브라우저의 최근 후보 경로는 즉시 재사용하지만, Vercel 인스턴스 사이의 공유 캐시는 아직 아닙니다.

## 지자체 공식 데이터 탐색

TAGO가 일시적으로 제공되지 않거나 특정 지자체의 원천이 더 최신인 경우를
대비해 공공데이터포털의 오픈 API·파일데이터 목록을 bounded discovery로 검색할 수 있습니다.
검색 결과는 후보 링크일 뿐이며 자동으로 그래프에 반영하지 않습니다. 선택한
후보는 `municipal_source_fetch.py`로 원본을 격리 다운로드한 뒤 지자체별
스키마 importer와 ID·순번 검증을 거쳐야 활성화됩니다.

웹에서도 `GET /api/sources/discover?q=춘천%20버스`로 같은 공식 검색을 호출할 수
있습니다. 응답은 검수용 후보만 반환하며 자동 수집·활성화하지 않습니다.

공개 여행기 기반 검증 큐는 `docs/obsidian/bus-travel-research/`와
`service/data/journey_research.json`에 보관합니다. `GET /api/journeys/research`는
각 기록의 노선번호를 현재 TAGO 카탈로그와 대조해 `HYDRATED`·부분 확인·재검증
필요로 나눕니다. 노선번호만 일치한 기록은 실제 경로로 가장하지 않으며, 정류장·
방향·시간표가 확인된 뒤에만 여행 검색 결과로 사용합니다.

```powershell
python -B opendesign/mockups/busro-itda-glass/service/municipal_source_discovery.py `
  --query "버스 노선 정류장" `
  --query "시내버스 시간표" `
  --query "버스 정보시스템" `
  --pages 2 `
  --per-page 50 `
  --output .\work\municipal-discovery.json
```

2026-09-01에 공식 검색에서 71개 후보를 확인했지만, 후보의 최신성·사용권·호출
승인·실제 스키마는 각 상세 페이지에서 별도로 확인해야 합니다. 검색 도구는
`www.data.go.kr` HTTPS 페이지만 읽고 리디렉션·외부 호스트·2MiB 초과 응답을
거부합니다.

상용 지도 화면을 크롤링해 버스 원천 데이터를 대체하지 않습니다. 버스
노선·정류장 원장은 공식 API/파일을 사용하고, 지도 형상은 OSM `route=bus`
관계와 OSRM 보완 경로를 사용합니다. 네이버·카카오·Google 지도 SDK를 붙일
때는 별도 키·쿼터·표시/캐시 약관을 확인한 뒤 좌표 시각화 용도로만 추가합니다.

## 로컬 실행

```powershell
cd opendesign\mockups\busro-itda-glass\service
python -B server.py --service-key-stdin
```

TAGO 서비스키는 표준입력 또는 서버 환경변수 `TAGO_SERVICE_KEY`로만 전달합니다. 저장소·브라우저 저장소·응답에는 키를 기록하지 않습니다.

## 검증

```powershell
cd opendesign\mockups\busro-itda-glass\service
python -B -m unittest discover -s tests -v
```

서비스 테스트는 API 입력 제한, 동일 출처 정책, TAGO 호출 예산·동시성, SQLite 무결성, 방향성 경로 탐색과 서버리스 저장 경계를 검증합니다.

## 현재 경계

- 현재 배포본은 공식 자료로 확인된 부분 그래프이며, 전국 모든 노선이 최신 방향 순서·시간표를 가진 것은 아닙니다.
- TAGO 제공 범위와 지자체 데이터 품질에 따라 실시간 조회 가능 여부가 다릅니다.
- Vercel 배포에서는 이력 쓰기가 영속적이지 않으므로 수집 API를 의도적으로 비활성화했습니다.
- 이 저장소에는 API 키와 운영 데이터베이스를 포함하지 않습니다.
