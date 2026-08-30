# 버스로 잇다

전국 시내버스 여행 경로를 실제 정류장·노선 데이터와 방향성 Dijkstra로 탐색하는 로컬 웹 앱입니다. 흰색 중심의 모바일 UI, TAGO 도착·차량위치 수집, SQLite 이력, OSM 노선 지도를 포함합니다.

앱과 상세 실행 문서는 [`opendesign/mockups/busro-itda-glass`](opendesign/mockups/busro-itda-glass)에 있습니다.

```powershell
cd opendesign\mockups\busro-itda-glass\service
python -B server.py --service-key-stdin
```

서비스키는 표준입력으로만 전달하며 Git·파일·브라우저 저장소에 보관하지 않습니다. 로컬 실행 DB와 캐시는 커밋 대상에서 제외됩니다.

현재 공식 정적 카탈로그에는 전국 정류장 227,054건과 TS BIS 노선 7,239건이 포함됩니다. TAGO 노선·정류장 API 권한 승인 전에는 전국 방향 순서 그래프와 성공률을 추정하지 않고 `DATA_GAP`으로 표시합니다.
