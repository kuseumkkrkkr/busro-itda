# 버스로 잇다 로컬 데이터 서비스

프론트에 TAGO 키를 노출하지 않고, 전국 도시·노선·정류장 카탈로그와 도착정보·차량 위치를 서버에서 조회하는 Python 표준 라이브러리 서비스입니다. 조회 출처를 SQLite에 스냅샷으로 남기고, 연속 위치 폴링으로 통과 시간창을 재구성해 실제 증거 기반 날짜별 경로 재생을 제공합니다. 웹과 API를 한 프로세스에서 제공하며 기본 화면은 `http://127.0.0.1:8791/`입니다.

## 현재 정확한 범위

- TAGO `버스도착정보`의 정류소별 실시간 도착예정정보를 조회합니다.
- TAGO `버스노선정보`와 `버스정류소정보`에서 도시, 노선, 노선 상세, 경유 정류장, 정류장 검색, 반경 500m 정류장, 정류장 경유 노선을 조회합니다.
- `POST /api/mappings/validate`는 노선 경유 정류장 목록에서 `route_id + node_id`의 실제 포함 여부를 검증하고 결과·출처 해시를 저장합니다.
- `POST /api/collect`를 실행한 시점부터 로컬 SQLite에 스냅샷을 축적합니다.
- TAGO `버스위치정보`의 노선별 현재 차량 위치를 조회하고 `POST /api/positions/collect` 시점부터 위치 스냅샷을 축적합니다.
- TAGO 도착정보 API 자체는 과거 이력 API가 아닙니다. 과거 데이터는 이 서비스가 미리 수집한 스냅샷만 조회합니다.
- 반복 저장한 ETA는 실제 차량이 정류소를 통과한 시각이 아닙니다. 따라서 이 스냅샷을 실운행 지연 표본으로 사용하지 않습니다.
- LIVE `/api/simulate`는 합성 모델 혼용을 막기 위해 계속 `422 PASSAGE_HISTORY_REQUIRED`를 반환합니다. 실제 이력 평가는 `/api/replay`를 사용합니다.
- 동일 `route_id + vehicle_no`의 정류소 순번이 한 칸 전진하면 `PASSAGE`, 여러 칸 건너뛰면 `DATA_GAP`, 감소하면 `REGRESSION`으로 원형 보존합니다.
- 위치 API에는 관측시각이 없으므로 서버의 이전·현재 수집시각을 `observed_from`/`observed_to`로 저장합니다. 이는 정확한 통과시각이 아니라 `polling_window`입니다.
- `/api/replay`는 실제 `route_id + node_id + node_order` 매핑을 요구합니다. 연결 마감이 폴링 시간창 안에 걸리면 성공/실패로 단정하지 않고 `data_gap`으로 처리합니다.
- 날짜별 `data_gap`은 성공률과 실패율의 분모에서 제외합니다.
- fixture 시뮬레이션만 8개 이상의 합성 지연 표본으로 동작하며, UI·연동 시험용임을 응답 `basis.mode=fixture`로 표시합니다.
- 예정시각 진단은 한국 표준시 `Asia/Seoul` 기준으로만 비교합니다.
- 전국 경로 생성에 필요한 TAGO 카탈로그 원천은 제공하지만, TAGO가 제공하지 않는 지자체별 고정 시간표와 과거 원자료는 별도 수집원이 필요합니다.
- TAGO는 과거 위치 조회나 날짜 인자를 제공하지 않습니다. 서비스 가동 이전 날짜는 복원할 수 없고 `DATA_GAP`입니다.
- 공공데이터포털 전국 버스정류장 CSV와 TS BIS 노선 CSV를 전용 SQLite에 원본 URL·기준일·SHA-256·제외 행 수와 함께 가져옵니다. 이름·거리로 ID를 억지 조인하지 않습니다.
- 여행 그래프는 TAGO 또는 출처·해시·순번이 검증된 공식 지자체 경유 정류장 순서만 사용합니다. 운영용 주 경로는 `topology_ingest.py`가 TAGO 도시→노선→전체 경유 정류장을 전국 배치 적재하는 방식이며, `POST /api/network/hydrate`는 단일 노선 진단용입니다. API는 같은 출발·도착에서 최대 5개까지 결정론적으로 생성할 수 있고, 모바일 기본 흐름은 빠른 1차 경로를 보여준 뒤 추천·최소 환승·근거 우선·국토종주 기준을 바꿔 탐색합니다.
- 시간표와 충분한 자체 통과 이력이 모두 없으면 성공확률을 만들지 않고 `DATA_GAP`을 반환합니다.
- 지도는 OSM `route=bus` 관계를 우선 사용합니다. 없으면 공식 정류장 순서를 OSRM 도로에 연결한 추정 형상을 반환하며 정확도를 별도 표시합니다.
- `/api/sources`는 TAGO와 조사한 지자체 공식 시간표/BIS 출처, robots·허가·갱신 정책을 우선순위와 함께 제공합니다.

### 공공데이터포털 활용신청 4종 + 보조 1종

실제 전국 탐색·정류장 검색·도착·차량 위치까지 현실화하려면 로그인 후 아래 4종을 각각 활용신청해야 합니다. 서비스는 네 종류 모두 서버측에서만 호출합니다.

1. [국토교통부 (TAGO) 버스노선정보](https://www.data.go.kr/data/15098529/openapi.do)
2. [국토교통부 (TAGO) 버스도착정보](https://www.data.go.kr/data/15098530/openapi.do)
3. [국토교통부 (TAGO) 버스정류소정보](https://www.data.go.kr/data/15098534/openapi.do)
4. [국토교통부 (TAGO) 버스위치정보](https://www.data.go.kr/data/15098533/openapi.do)

[버스노선별 경유정류장](https://www.data.go.kr/data/15142031/openapi.do)은 시도·시군구·운행일자별 `sttn_seq`를 제공하는 별도 보조 출처입니다. 좌표와 운행회차 방향 식별자가 없으므로 전국 기본 그래프의 단독 대체재로 간주하지 않습니다.

2026-08-31 현재 개발계정에서 버스노선정보·버스정류소정보·버스노선별 경유정류장 3종의 자동승인을 확인했습니다. 실제 키 값은 이 문서·명령줄·Git에 기록하지 않습니다.

## 실행

fixture 모드:

```powershell
python server.py --fixture
```

공식 정류장·노선 CSV를 전용 카탈로그 DB로 가져오기:

```powershell
python server.py --fixture `
  --catalog-db C:\data\busro-network.sqlite3 `
  --import-stops C:\data\national_bus_stops.csv `
  --quarantine-invalid-stops `
  --import-routes C:\data\ts_bis_routes.csv `
  --import-only
```

`--quarantine-invalid-stops`는 잘못된 좌표를 고치지 않고 제외하며, 원본 행·가져온 행·제외 행·사유를 provenance에 기록합니다. 옵션을 빼면 한 행이라도 잘못된 CSV는 전체 거부합니다.

실제 TAGO 모드:

```powershell
python server.py --service-key-stdin
```

실행 후 **디코딩 서비스키**를 프롬프트에 붙여 넣습니다. 입력은 화면에 표시되지 않으며 argv·파일·브라우저 저장소에 남기지 않습니다. 환경변수 방식도 지원하지만 공유 PC에서는 `--service-key-stdin`을 권장합니다.

키가 없으면 `/api/status`에는 `missing_key`가 표시되고, `/api/arrivals`와 `/api/collect`는 `503 TAGO_KEY_REQUIRED`를 반환합니다. 키를 브라우저 코드, URL, LocalStorage에 넣지 마세요.

`.env.example`은 항목 설명용입니다. 이 표준 라이브러리 서버는 `.env` 파일을 자동으로 읽지 않으며, 실제 키는 서버 프로세스 환경변수로만 전달합니다.

실제 키로 도착·차량위치 응답이 각각 한 번만 영속화되고 동일 멱등키 재요청이 중복 저장되지 않는지 확인:

```powershell
python -B live_integration_check.py
```

검사는 `127.0.0.1`·`localhost`의 로컬 API만 허용하며 서비스키를 읽거나 출력하지 않습니다. 운행 종료 시간대에는 응답 목록이 비어도 공식 TAGO 응답과 빈 스냅샷 자체의 영속화·멱등성을 검증합니다.

### 전국 방향성 노선 그래프 적재

버스노선정보·버스정류소정보 활용신청 승인 후 아래 명령을 명시적으로 실행합니다. TAGO 자체 도시코드와 노선 ID를 먼저 열거하므로, 식별자 체계가 다른 TS 정적 노선 CSV를 TAGO 조회에 억지로 대입하지 않습니다.

```powershell
python -B topology_ingest.py `
  --catalog-db .\data\network_catalog.sqlite3 `
  --service-key-stdin `
  --request-budget 9000 `
  --requests-per-second 2 `
  --target-source tago
```

디코딩 서비스키는 비표시 프롬프트에 입력합니다. 키·요청 URL·쿼리 문자열은 출력하거나 DB에 저장하지 않습니다. 기본 9,000회 상한은 일일 10,000회 쿼터에 여유를 둔 값이며, 실제 포털 쿼터가 다르면 더 낮춰야 합니다.

이미 키를 메모리에만 보관한 live 서버가 실행 중이면 키를 다시 복사하지 않고 loopback 프록시로 적재할 수 있습니다. 아래 모드는 포트가 명시된 리터럴 loopback HTTP origin과 고정된 카탈로그 GET 경로만 허용하며 프록시·리디렉션·2MB 초과 응답을 거부합니다. `--service-key-stdin`과 동시에 사용할 수 없습니다.

```powershell
python -B topology_ingest.py `
  --catalog-db ..\..\..\..\work\network_catalog.runtime.sqlite3 `
  --local-live-api http://127.0.0.1:8791 `
  --request-budget 900 `
  --requests-per-second 2 `
  --target-source tago
```

수집기는 노선별 최대 100개×10페이지로 제한하며 페이지 내용·다음 페이지·실패 코드·실행별 사용 요청 수·전체 커버리지를 SQLite에 기록합니다. 중단 또는 쿼터 소진 후 같은 명령을 다시 실행하면 저장된 다음 페이지부터 재개합니다. 이미 완료한 노선도 다시 확인하려면 `--refresh-complete`를 붙이며, 정류장열 SHA-256이 같으면 새 버전을 만들지 않습니다.

`--target-source catalog`는 기본 거부됩니다. 특정 정적 카탈로그의 도시/노선 ID가 TAGO와 동일하다는 별도 검증을 마친 경우에만 `--trust-catalog-identifiers`를 함께 쓸 수 있습니다. 현재 번들 TS 노선 CSV에는 이 보장을 적용하지 않습니다.

현재 키가 노선/정류소 API에 승인되지 않았다면 첫 인증 오류에서 실행을 중단하고 `DATA_GAP`을 기록합니다. 7천여 노선에 같은 실패 요청을 반복하지 않습니다.

### 공식 지자체 방향 그래프 적재

TAGO 전국 적재와 별개로, 정확한 노선 ID·정류장 ID·순번·좌표를 함께 공개한 공식 지자체 CSV는 전용 importer로 넣을 수 있습니다. 아래 예시는 춘천시 공식 2026-03-26 파일입니다. Git에 포함된 57MB 정적 카탈로그는 직접 변경하지 않고 `work`의 런타임 복사본을 사용합니다.

```powershell
Copy-Item .\data\network_catalog.sqlite3 ..\..\..\..\work\network_catalog.runtime.sqlite3

python -B municipal_topology_ingest.py `
  --catalog-db ..\..\..\..\work\network_catalog.runtime.sqlite3 `
  --csv ..\..\..\..\work\official-sources\chuncheon_bus_route_stops_20260326.csv `
  --profile chuncheon `
  --source-date 2026-03-26 `
  --expected-sha256 68676665E73872B7E0CDE412E4EDC93F1F7685EEB56044CF9AD7DFB267DB470F
```

파일 크기·행·노선·노선별 정류장 수, 정확한 헤더, CP949/UTF-8, 한국 좌표 범위, 노선별 1부터 연속된 정류장 순서를 모두 검사한 뒤 전체 파일을 한 트랜잭션으로 활성화합니다. 지자체 원시 정류장 ID는 그대로 보존하며 `CCB` 같은 타 제공자 접두사를 추측해 결합하지 않습니다. 같은 파일 재실행은 새 버전을 만들지 않습니다.

### KTDB 공식 GTFS ZIP 적재 준비

[KTDB 대중교통 GTFS 공식 배포 페이지](https://www.ktdb.go.kr/www/selectBbsNttView.do?bbsNo=2&key=45&nttNo=3785)에서 승인받아 내려받은 ZIP은 `gtfs_ingest.py`로 오프라인 적재할 수 있습니다. 원본 ZIP과 압축 해제 파일은 `work` 등 Git 제외 경로에 두고, 다운로드 시점에 별도로 기록한 SHA-256을 반드시 전달합니다. 실제 원본이나 예제 GTFS 데이터는 저장소에 넣지 않습니다.

```powershell
python -B gtfs_ingest.py `
  --catalog-db ..\..\..\..\work\network_catalog.runtime.sqlite3 `
  --zip ..\..\..\..\work\official-sources\ktdb-gtfs.zip `
  --expected-sha256 PUT_RECORDED_64_HEX_SHA256_HERE `
  --source-url "https://www.ktdb.go.kr/www/selectBbsNttView.do?bbsNo=2&key=45&nttNo=3785" `
  --source-date YYYY-MM-DD `
  --provider KTDB
```

- `stops.txt`, `routes.txt`, `trips.txt`, `stop_times.txt`, `calendar.txt`가 모두 있어야 하며 `calendar_dates.txt`가 있으면 예외일도 함께 저장합니다.
- 입력은 UTF-8만 허용합니다. ZIP 경로 탈출·심볼릭 링크·암호화·중복 파일·과도한 압축률을 거부하고, 압축/해제 크기·파일 수·행·열·셀 상한을 적용합니다. ZIP은 하나의 열린 파일 descriptor에서 SHA-256 검증과 해제를 수행하고 활성화 직전 다시 검증하며, 각 표는 한 번의 해제 스트림에서 SHA-256 계산과 CSV 적재를 함께 합니다.
- 전체 파일과 참조 무결성을 먼저 검증한 뒤 GTFS 원문, 파일별 SHA-256, 기준일, 원본 ID alias, trip 시간과 달력, 방향성 정류장 패턴, 활성 포인터를 한 SQLite 트랜잭션으로 기록합니다. 같은 ZIP 재실행은 revision과 버전을 늘리지 않습니다.
- 그래프 ID는 `GTFS:KTDB:...` namespace에서 원본 ID를 해시해 만듭니다. 원본 ID는 alias로 그대로 보존하되 이름·거리·노선번호로 TAGO ID와 결합하지 않습니다.
- 같은 노선이라도 정류장 순서가 반대면 별도 방향 패턴으로 생성합니다. `route_type=3`과 GTFS 확장 버스 유형 `700..799`만 버스 여행 그래프에 활성화합니다.
- `pickup_type` 또는 `drop_off_type`이 일반 승하차(`0`/빈 값)가 아닌 trip은 원문 시간표 근거에는 보존하지만, 시간표 인지 승하차 필터가 없는 일반 여행 그래프에는 활성화하지 않습니다. 해당 trip만 있는 feed는 활성 근거로 적재되되 일반 그래프 경로를 생성하지 않습니다.
- `24:xx:xx`부터 `47:59:59`까지는 원문과 초 단위 값을 함께 보존합니다. `NetworkCatalog.gtfs_schedule_evidence(...)`는 활성 원본·달력·예외일·trip·stop time 근거만 반환하며 `eligible_for_success_rate=false`, `success_probability=null`을 고정합니다. 배포자 시간표 의미 검증과 다일 실제 통과 이력이 없으면 제품 성공률 근거로 사용할 수 없습니다.
- importer는 원본 행을 메모리에 모으지 않고 카탈로그와 같은 드라이브의 임시 SQLite에 스트리밍합니다. 기본 상한은 `stop_times.txt` 2,500만 행·전체 3,000만 행으로, 확인된 KTDB 2024 전체 `stop_times` 21,889,865행을 거부하지 않습니다. 전체 행 상한은 각 표 스트림 안에서 즉시 적용합니다. stage에는 `max_page_count`를 적재 전에 설정하고, 카탈로그 DB 성장·WAL·512 MiB 여유를 포함한 같은 드라이브 가용공간을 사전 검사합니다. 방향 패턴 쿼리는 임시 정렬이 필요한 실행계획을 거부하고 SQLite temp 저장소는 메모리로 고정해 C: 임시공간을 사용하지 않습니다. 검증 완료 뒤에만 카탈로그 본 DB로 한 트랜잭션 복사·활성화하며 stage는 삭제합니다. 실제 승인 ZIP의 end-to-end 소요시간·최종 DB 크기는 파일 수령 후 별도 검증해야 합니다. `frequencies.txt`, `shapes.txt`, `transfers.txt`는 이번 적재 근거에 포함하지 않습니다.

주기 수집은 명시적으로 별도 실행합니다. 숨은 백그라운드 호출은 하지 않습니다.

### 기존 로컬 TAGO 런타임 재사용

서비스 키를 숨김 입력으로 보유한 직접 연결 서버가 이미 `127.0.0.1:8791`에서 실행 중이고, 최신 UI 서버가 같은 이력·카탈로그 DB를 사용한다면 다음처럼 키를 다시 입력하지 않고 연결할 수 있습니다.

```powershell
python -u server.py `
  --host 127.0.0.1 --port 8792 `
  --db D:\path\to\busro-itda-live.sqlite3 `
  --catalog-db D:\path\to\network_catalog.runtime.sqlite3 `
  --local-live-api http://127.0.0.1:8791 `
  --shared-live-storage
```

- upstream은 DNS 이름이 아닌 literal `127.0.0.1` 또는 `::1`, 명시 포트, 현재 리스너와 다른 포트만 허용합니다.
- 시작 시 upstream이 fixture나 연쇄 proxy가 아닌 직접 TAGO LIVE인지 확인합니다. 상태는 `state=ready`와 `credential_scope=loopback_upstream`으로 분리해 현재 프로세스가 키를 보유한다고 표시하지 않습니다.
- 구버전 직접 서버가 한글 노선 ID capability를 광고하지 않으면 ASCII 노선은 계속 사용하되 한글 ID 요청은 명시적 503으로 차단합니다. 전체 노선 ID 지원에는 키 보유 upstream을 현재 코드로 숨김 입력 재시작해야 합니다.
- GET은 고정된 TAGO 조회 9개 경로만 전달합니다. POST는 수집·매핑 검증·노선 적재 4개만 고정 허용하며 브라우저의 Cookie·Authorization·Origin은 전달하지 않습니다.
- `--shared-live-storage`가 없으면 수집·노선 적재 쓰기는 거부합니다. 설정해도 시작 시 두 DB의 저장 카운트·그래프 요약이 다르면 실행하지 않으며, 수집 스냅샷 또는 노선 sequence가 로컬 DB에서 즉시 보이지 않으면 `LOOPBACK_SHARED_STORAGE_MISMATCH`로 중단합니다.
- HTTP 환경 proxy·redirect·재시도는 사용하지 않습니다. 응답 2 MiB, 요청 64 KiB, 총 8초, 동시 8개가 기본 상한입니다.

```powershell
python collector.py --city-code 25 --node-id DJB8001793 --interval 300
```

차량 위치·통과 시간창 수집:

```powershell
python collector.py --city-code 25 --route-id DJB30300052 --interval 60
```

여러 정류장·노선을 서버측 키로 수집하려면 `multi_collector.py`를 명시적으로 실행합니다. 이 수집기는 키를 읽지 않고 HTTP loopback(`127.0.0.1`/`localhost`) API만 호출합니다. 대상 형식은 `collector_targets.example.json`과 같습니다.

```powershell
python -B multi_collector.py `
  --targets-file .\collector_targets.example.json `
  --once `
  --request-budget 100 `
  --requests-per-second 2
```

`--once`를 빼면 `--interval`마다 순회하되 프로세스 전체 `--request-budget` 소진 시 종료합니다. 오류도 예산에 포함하며 대상별로 JSON 한 줄을 남기고 다음 대상으로 격리합니다. 대상·주기·시간 버킷 기반의 결정론적 `Idempotency-Key`를 보내므로 같은 주기 안의 재실행은 중복 저장되지 않습니다. 목록은 중복 제거 후 최대 10,000개, 파일은 1 MiB, 호출률은 초당 0.1~20회로 제한합니다. 숨은 백그라운드 실행은 없습니다.

공식 위치 API는 현재 운행 차량만 반환하며 개발계정 일일 호출량 제한이 있습니다. 수집할 노선 수와 주기를 합산해 쿼터 안에서 운영해야 합니다.

## API

- `GET /api/status`
- `GET /api/cities`
- `GET /api/routes?city_code=25&route_no=101&page=1&limit=100`
- `GET /api/routes/info?city_code=25&route_id=...`
- `GET /api/routes/stops?city_code=25&route_id=...&page=1&limit=100`
- `GET /api/stops?city_code=25&node_name=대전역&page=1&limit=100`
- `GET /api/stops/nearby?latitude=36.35&longitude=127.38` — 반경은 서버에서 500m로 고정합니다.
- `GET /api/stops/routes?city_code=25&node_id=...`
- `GET /api/network/status`
- `GET /api/network/cities?q=서울&limit=20`
- `GET /api/network/stops?q=서울역&limit=20`
- `GET /api/network/routes?q=601&limit=20`
- `GET /api/sources?status=VERIFIED_ROUTE_ONLY&limit=25`
- `GET /api/arrivals?city_code=25&node_id=DJB8001793`
- `GET /api/history?route_id=DJB30300002&from=2026-08-01&to=2026-08-31`
- `GET /api/positions?city_code=25&route_id=DJB30300052`
- `GET /api/passages?route_id=DJB30300052&from=2026-08-01&to=2026-08-31`
- `POST /api/collect` — `Idempotency-Key` 헤더 권장. 없으면 동일 요청을 5분 버킷 단위로 중복 방지합니다. 도착조회 응답은 30초 캐시합니다.
- `POST /api/positions/collect` — 차량 위치를 새로 조회해 스냅샷과 전이 사건을 한 트랜잭션으로 저장합니다. `Idempotency-Key`가 없으면 30초 버킷 단위로 중복 방지합니다.
- `POST /api/replay` — 실제 위치에서 재구성한 통과 시간창만 사용해 날짜별 `success/failure/data_gap`을 판정합니다.
- `POST /api/simulate` — 현재 fixture 모드 전용. LIVE는 `PASSAGE_HISTORY_REQUIRED`.
- `POST /api/mappings/validate` — `{city_code,route_id,node_id}`가 실제 노선 경유 정류장인지 최대 1,000개 범위에서 검증합니다.
- `POST /api/network/hydrate` — `{city_code,route_id}`의 전체 경유 순서를 서버가 TAGO에서 조회해 그래프에 적재합니다.
- `POST /api/journeys/generate` — `{from_stop_id,to_stop_id,service_date,departure_time,preference,max_alternatives}`로 활성 공식 GTFS의 해당 운행일·출발 시각 이후 가장 이른 도착 경로를 검색합니다. 날짜와 시각은 함께 보내야 하며 KST 민간시를 사용합니다. 공식 시간표가 없으면 `SCHEDULE_DATA_GAP`과 별도의 `static_alternatives`만 반환하고 실제 운행 가능 경로로 확정하지 않습니다.
- `POST /api/osm/geometry` — `{route_ref,stops}`로 OSM 버스 관계 또는 명시적으로 라벨된 도로 추정 형상을 반환합니다.

카탈로그 응답은 `provenance.snapshot_id`, `upstream_hash`, `captured_at`을 포함합니다. fixture는 `source=TAGO_SCHEMA_FIXTURE`와 `fixture_notice=SCHEMA_ONLY_NOT_LIVE`로 표시되며 실데이터로 해석하면 안 됩니다.

수집 예시:

```json
{
  "city_code": "25",
  "node_id": "DJB8001793"
}
```

fixture 시뮬레이션은 UI·계약 시험용이며 실제 운행 성공률로 표현하면 안 됩니다. 실제 후보는 아래 `/api/replay`처럼 자체 적재한 통과 시간창만 사용합니다.

실제 통과 이력 재생 계약 예시입니다. 아래 ID는 형식 설명용이며, 실제 요청에서는 활성 GTFS 조회 결과의 ID로 교체해야 합니다.

```json
{
  "route": {"id": "B", "name": "실제 ID 매핑 경로"},
  "legs": [
    {
      "id": "verified-transfer",
      "route_id": "GTFS:KTDB:R000000000000000000000000:P0000000000000000000000000000000000000000",
      "node_id": "GTFS:KTDB:S000000000000000000000000",
      "node_order": 2,
      "time_evidence_source": "ktdb-gtfs-2024",
      "time_evidence_trip_id": "GTFS:KTDB:T000000000000000000000000",
      "next_route_id": "GTFS:KTDB:R111111111111111111111111:P1111111111111111111111111111111111111111",
      "next_node_id": "GTFS:KTDB:S111111111111111111111111",
      "next_node_order": 1,
      "next_time_evidence_trip_id": "GTFS:KTDB:T111111111111111111111111"
    }
  ],
  "dates": {"from": "2026-08-31", "to": "2026-09-06"},
  "match_window_minutes": 180
}
```

LIVE에서는 요청의 `scheduled_arrival`·`next_departure`·`minimum_transfer_minutes` 값을 신뢰하지 않습니다. 서버가 같은 활성 GTFS feed와 서비스일에서 도착 trip/정류장 레코드와 다음 출발 trip/정류장 레코드를 각각 유일하게 결합한 경우에만 저장된 `arrival_seconds`·`departure_seconds`를 사용합니다. 최소 환승 여유는 GTFS 공식 값이 아니라 서버 안전 정책으로 고정한 5분이며, 응답의 `minimum_transfer_source=server_safety_policy`와 `minimum_transfer_minutes=5`로 구분합니다. ID가 없거나 모호하거나 feed가 다르면 `422 OFFICIAL_SCHEDULE_RECORD_REQUIRED`입니다.

`summary.eligible_days = success_days + failure_days`이며 `gap_days`는 분모에 들어가지 않습니다. `success_rate`는 증거가 있는 날짜가 없으면 `null`입니다.

## 보안·운영

- 기본 바인딩은 `127.0.0.1`; JSON 본문은 64 KiB로 제한합니다.
- 기본 CORS 허용 목록은 로컬 OpenDesign 포트 `8289`, `8290`과 서비스 same-origin `8791`뿐입니다.
- 추가 origin은 와일드카드 없이 `BUSRO_ALLOWED_ORIGINS`에 쉼표로 명시합니다.
- 외부 호출 타임아웃은 기본 6초입니다. 실시간 응답 캐시는 30초, 노선·정류장 카탈로그 공유 캐시는 TAGO 쿼터 보호를 위해 24시간입니다.
- 동일 정류장 cache miss와 동일 5분 collect는 per-key singleflight로 한 번만 호출합니다. 실패도 1초간 합쳐 재시도 폭증을 막습니다.
- 동일 노선 위치 조회와 동일 position collect도 per-key singleflight로 합칩니다.
- 실제 TAGO upstream 호출은 프로세스 전체 동시 8개, 입장 대기 0.25초, KST 일일 9,000회가 기본 상한입니다. 캐시 hit와 singleflight follower는 세지 않으며 실제 실패 호출은 쿼터에 포함합니다.
- 비-loopback 클라이언트의 수집·위치 수집·매핑 검증·노선 적재 운영 API는 `BUSRO_OPERATOR_TOKEN` bearer 또는 `X-Busro-Operator-Token`이 필요합니다. 토큰을 설정한 운영 환경에서는 reverse proxy의 loopback 연결에도 항상 요구하며, 전달 프록시 헤더를 로컬 주소로 신뢰하지 않습니다.
- OSM/OSRM 형상 조회는 전체 동시 3개와 절대 20초 deadline을 적용합니다. 느린 응답 reader도 별도 3개 상한을 유지하고, 포화 시 `OSM_BUSY` 429로 빠르게 거절합니다.
- 저장 collect는 조회 캐시를 우회해 새 TAGO 응답만 영속화합니다. SQLite 쓰기는 프로세스 내 직렬화하며 서로 다른 collect 200건의 lock 회귀 테스트를 포함합니다.
- HTTP accept queue는 256, 동시 활성 처리기는 최대 200, 요청 소켓 대기는 10초입니다. 연결이 끊긴 클라이언트의 응답 쓰기는 안전하게 종료합니다.
- JSON 64 KiB, fixture 정규화 500대, replay `dates × legs` 300 및 사건 스캔 100,000건 상한이 있습니다.
- 외부 URL은 TAGO의 도착·위치 및 허용된 7개 노선/정류장 operation으로 고정하며 사용자 URL·operation을 프록시하지 않습니다. 입력은 URL 인코딩 전에 길이·형식·페이지 상한을 검사합니다.
- 로컬 LIVE bridge도 literal loopback과 고정 API 경로만 허용합니다. 직접 서버 상태를 확인해 proxy chaining을 차단하고, 키·브라우저 인증 헤더·upstream CORS 헤더를 전달하거나 노출하지 않습니다.
- 카탈로그 동일 요청은 SQLite 캐시와 per-key singleflight로 합치며, 실제 키는 디코딩 키를 서버 환경변수로 받아 한 번만 인코딩합니다.
- 운영에서는 `collector.py` 또는 동등한 별도 수집 작업을 상시 운영해야 과거 표본이 누적됩니다.
- SQLite WAL·busy timeout·정규화 인덱스를 사용하지만, 이 표준 라이브러리 서버는 로컬 검증용입니다. 실제 200 동시접속 운영은 별도 배포 서버·작업 큐·공유 DB 구성이 필요합니다.

## 테스트

```powershell
python -B -m unittest discover -s tests -v
```
