# 버스로 잇다 · 모바일 리디자인

국토종주 / 나드리, 홈·탐색·여행 중·기록 화면에서 실제 서비스 API를 사용합니다. 정류장 검색은 `/api/network/stops`, 경로 검색은 `/api/journeys/generate`, 승차 정류장 도착정보는 `/api/arrivals`에 연결됩니다.

검색 결과에서 정류장 ID와 도시코드를 선택해야 경로 요청이 활성화됩니다. 수정한 검색어는 이전 선택을 무효화합니다. 검색에는 지연 호출·취소·캐시, 경로에는 중복 요청 방지·45초 타임아웃을 적용합니다. 데이터 부족과 검색 한도 초과는 별도 안내합니다. 경로는 실제 방향별 정류장 연결이며 운행 가능 시각·도착 시각을 보장하지 않습니다. 하차 알림·위치 추적을 가장한 기능은 없습니다. 완료 기록은 사용자 확인으로 브라우저에 저장합니다. 기존 프리뷰 저장소와 키를 분리했습니다.

나드리는 편집한 여행지 소개이며, 해당 여행지로 가는 실제 도착 정류장을 검색·선택해 경로를 조회합니다. 지도의 선은 정류장 연결선이며 도로 형상이 아닙니다.

`npm ci` 후 PowerShell에서 `$env:DEPLOY_BASE='/opendesign/mockups/busro-travel/'; npm run build` 실행. dist 내용을 저장소의 `opendesign/mockups/busro-travel/`에 복사합니다. 백엔드 변경 없음.

사진: Matt Kieffer, Wikimedia Commons, CC BY-SA 2.0. 화면에 맞춰 크롭.
https://commons.wikimedia.org/wiki/File:Bukchon_Hanok_Village,_Seoul_(48733036051).jpg

지도: OpenStreetMap contributors. 폰트: Pretendard (SIL OFL). 아이콘: Lucide (ISC).
