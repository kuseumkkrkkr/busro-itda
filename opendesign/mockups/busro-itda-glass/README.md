# 버스로 잇다 · 실제 동작 웹 초안

흰색 중심의 밝은 iOS 여행 UI에서 전국 정류장 검색, 검증된 노선 경유 순서 기반 다중 여행 후보, OSM 노선 지도, TAGO 실시간 도착·차량 위치, 로컬 이력 축적과 날짜별 재생을 제공합니다. 글래스 재질은 상단·하단의 기능 레이어에만 제한했습니다.

## 바로 실행

```powershell
cd service
python server.py --service-key-stdin
```

프롬프트에 공공데이터포털의 **디코딩 서비스키**를 입력한 뒤 `http://127.0.0.1:8791/`을 엽니다. 키는 화면·파일·URL·브라우저 저장소에 남기지 않습니다. 테스트용 fixture 화면은 `python server.py --fixture`로 실행할 수 있습니다.

프런트 JSX를 수정했다면 고전 스크립트 전역을 유지하도록 아래와 같이 다시 빌드합니다. `--global-name`이나 IIFE 래핑은 컴포넌트를 숨기므로 사용하지 않습니다.

```powershell
foreach ($name in @("components", "nationwide", "screens", "app")) {
  npx.cmd --yes esbuild@0.25.10 "$name.jsx" --loader:.jsx=jsx --tree-shaking=false --outfile="$name.compiled.js"
}
```

## 실제 데이터 경계

- 동봉한 공식 정적 카탈로그: 전국 정류장 227,054건, TS BIS 노선 7,239건. 출처·기준일·SHA-256·제외 행 수를 DB에 함께 보존합니다.
- 경로 그래프: TAGO에서 확인한 방향성 경유 정류장 순서만 적재합니다. 이름·거리만으로 노선과 정류장을 억지 조인하지 않습니다.
- 실시간: 도착·차량 위치 API를 서버에서 호출하고, 사용자가 저장할 때부터 SQLite 이력이 쌓입니다.
- 과거: TAGO가 제공하지 않는 과거 운행은 역으로 만들 수 없습니다. 충분한 통과 이력과 검증된 시간표가 없으면 `DATA_GAP`입니다.
- 차량 관측은 시간표 검증을 대신하지 않습니다. 공식 시간표 출처·수집 시각이 명시된 근거만 시간표 검증으로 인정합니다.
- 지도: OSM `route=bus` 관계를 우선 사용하며, 없을 때만 공식 정류장 순서를 따른 OSRM 도로 추정선임을 표시합니다.

노선·정류장 카탈로그, 도착, 차량 위치 API는 공공데이터포털에서 각각 활용신청해야 합니다. 상세 실행·API·운영 한계는 [service/README.md](service/README.md)를 확인하세요.
