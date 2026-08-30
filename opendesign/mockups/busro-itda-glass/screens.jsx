function ExploreScreen({ form, setForm, connection, simulation, onSearch, onOpenSimulation, onOpenLive }) {
  const summary = simulation.summary || { probability: 0, weakestLeg: "집계 전", coverage: 0 };
  return (
    <main className="screen explore-screen">
      <div className="map-atmosphere" aria-hidden="true">
        <div className="terrain terrain-one" /><div className="terrain terrain-two" />
        <div className="route-stroke route-one" /><div className="route-stroke route-two" />
        <span className="map-node node-one" /><span className="map-node node-two" /><span className="map-node node-three" />
        <p className="map-label label-one">세종</p><p className="map-label label-two">영천</p><p className="map-label label-three">부산</p>
      </div>

      <section className="hero-copy">
        <SourceBadge mode={connection.mode} label={connection.label} />
        <p className="eyebrow">오늘의 로컬버스 원정</p>
        <h1>도시와 도시 사이,<br /><em>버스만으로</em> 잇다.</h1>
      </section>

      <GlassCard className="journey-search" aria-label="여정 검색">
        <label className="place-field"><span className="place-dot start" /><span><small>출발</small><input value={form.from} onChange={(event) => setForm({ ...form, from: event.target.value })} aria-label="출발지" /></span></label>
        <button className="swap-button" type="button" aria-label="출발지와 도착지 바꾸기" onClick={() => setForm({ ...form, from: form.to, to: form.from })}><Icon name="arrows-down-up" /></button>
        <label className="place-field"><span className="place-dot end" /><span><small>도착</small><input value={form.to} onChange={(event) => setForm({ ...form, to: event.target.value })} aria-label="도착지" /></span></label>
        <div className="search-meta">
          <button type="button"><Icon name="calendar-blank" /> 9월 1일</button>
          <button type="button"><Icon name="clock" /> 07:00</button>
          <button className="search-submit" type="button" onClick={onSearch} disabled={!form.from.trim() || !form.to.trim()} aria-label="경로 검색"><Icon name="arrow-right" /></button>
        </div>
      </GlassCard>

      <GlassCard className="probability-hero">
        <div className="probability-copy">
          <p className="eyebrow">DAILY ROUTE PULSE</p>
          <h2>오늘, 끝까지<br />이어질 확률</h2>
          <p>{summary.coverage > 0 ? `최근 적재 이력 ${summary.coverage}건 기반` : "아직 공식 이력 없음 · 샘플 분포"}</p>
          <button className="text-link" type="button" onClick={onOpenSimulation}>날짜별 결과 보기 <Icon name="arrow-up-right" /></button>
        </div>
        <ProbabilityRing value={summary.probability || 0} />
      </GlassCard>

      <div className="quick-grid">
        <button className="mini-glass" type="button" onClick={onOpenLive}><span className="mini-icon live"><Icon name="broadcast" /></span><span><small>다음 버스</small><strong>도착정보 확인</strong></span><Icon name="caret-right" /></button>
        <button className="mini-glass" type="button" onClick={onOpenSimulation}><span className="mini-icon sim"><Icon name="waveform" /></span><span><small>취약 구간</small><strong>{summary.weakestLeg}</strong></span><Icon name="caret-right" /></button>
      </div>
    </main>
  );
}

function LiveScreen({ journey, connection, legs, selectedLeg, setSelectedLeg, arrivals, history, passageCoverage, mappingSummary, loading, error, notice, onRefresh, onCollect, onExplore }) {
  if (!journey || legs.length === 0) {
    return (
      <main className="screen content-screen">
        <ScreenHeading eyebrow="실시간" title="조회할 여행이 없습니다" detail={journey ? "선택한 후보에 연속 버스 이동 구간이 없습니다." : "전국 탐색에서 실제 생성된 여행 후보를 먼저 선택하세요."} />
        <GlassCard className="stop-board">
          <InlineNotice tone="warning" icon="map-trifold" title="DATA_GAP · 전국 여행 후보 필요">기본 고정 경로나 샘플 도착정보로 대신 표시하지 않습니다.</InlineNotice>
        </GlassCard>
        <button className="liquid-button sticky-action" type="button" onClick={onExplore}>전국 탐색으로 가기 <Icon name="arrow-right" /></button>
      </main>
    );
  }
  const leg = legs.find((item) => item.id === selectedLeg) || legs[0];
  const values = history;
  const maxDelay = Math.max(12, ...values.map((item) => Number(item.delay || item.delay_minutes || 0)));
  return (
    <main className="screen content-screen">
      <ScreenHeading eyebrow="실시간" title="도착정보" detail="현재 도착정보와 저장된 관측 기록을 확인합니다." action={<button className={`refresh-button ${loading ? "spinning" : ""}`} type="button" onClick={onRefresh} disabled={loading} aria-label="도착정보 새로고침"><Icon name="arrows-clockwise" /></button>} />
      <InlineNotice tone={connection.mode === "live" ? "success" : "warning"} icon={connection.mode === "live" ? "cloud-check" : "key"} title={connection.mode === "live" ? "TAGO 공식 응답 연결됨" : connection.mode === "ready" ? "키 연결됨 · LIVE 아님" : connection.mode === "fixture" ? "현재는 FIXTURE 모드" : "공식 데이터 연결 대기"}>{connection.message}</InlineNotice>
      <CoverageStrip mappingSummary={mappingSummary} coverage={passageCoverage} />

      <div className="stop-chips" role="list" aria-label="조회할 여정 구간">
        {legs.map((item) => <button role="listitem" type="button" key={item.id} className={selectedLeg === item.id ? "active" : ""} onClick={() => setSelectedLeg(item.id)}><small>{item.city}</small><strong>{item.routeNo}</strong><i className={`mapping-dot ${item.mappingState}`} aria-label={item.mappingState === "verified" ? "검증됨" : item.mappingState === "checking" ? "검증중" : "미매핑"} /></button>)}
      </div>

      <div className="mapping-context"><MappingBadge state={leg.mappingState} /><p>{leg.apiMapped ? "이 구간의 공식 cityCode · nodeId · routeId가 검증됐습니다." : "공식 식별자가 검증되기 전에는 이 구간을 LIVE로 표시하지 않습니다."}</p></div>

      {notice && <InlineNotice tone="success" icon="database" title="이력 저장">{notice}</InlineNotice>}
      <GlassCard className="stop-board">
        <div className="stop-board-head"><div><p className="eyebrow">{leg.city} · {leg.routeNo}</p><h2>{leg.board}</h2><p>{leg.alight} 방면 · 승차 순번 {Number.isInteger(leg.nodeOrder) ? leg.nodeOrder : "DATA_GAP"}</p></div><span className="route-orb">{leg.routeNo}</span></div>
        {loading ? <LoadingRows count={2} /> : (
          <div className="arrival-list">
            {error && <InlineNotice tone="warning" icon="warning-circle" title="실시간 데이터 없음">{error}</InlineNotice>}
            {arrivals.length ? arrivals.map((arrival, index) => (
              <article className="arrival-row" key={`${arrival.routeNo || arrival.route_no}-${index}`}>
                <div><small>{index === 0 ? "곧 도착" : "다음 버스"}</small><strong>{arrival.minutes ?? arrival.arrival_minutes}<span>분</span></strong></div>
                <div><p>{arrival.stops ?? arrival.remaining_stops}개 정류장 전</p><small>{arrival.vehicleNo || arrival.vehicle_no || "차량번호 미제공"}</small></div>
                <SourceBadge mode={connection.mode} label={connection.mode === "ready" ? "공식 매핑 구간" : undefined} />
              </article>
            )) : <div className="empty-mini"><Icon name="bus" /><p>현재 제공된 도착정보가 없습니다.</p></div>}
          </div>
        )}
        <div className="board-actions"><button type="button" onClick={onCollect} disabled={connection.mode !== "live" || !leg.apiMapped}><Icon name="database" /> 도착·차량 위치 이력 저장</button><small>명시적으로 누른 TAGO LIVE 응답만 관측시각·원문 해시와 함께 저장합니다.</small></div>
      </GlassCard>

      <GlassCard className="history-card">
        <div className="card-title"><div><p className="eyebrow">최근 기록</p><h3>도착 예정시간 관측</h3></div><span>{history.length ? `${history.length}개 적재` : "DATA_GAP"}</span></div>
        {connection.mode === "live" && history.length === 0 && <InlineNotice tone="warning" icon="database" title="DATA_GAP">아직 이 정류장의 실제 관측 이력이 없습니다. 수집 시작 이전 날짜는 실패로 계산하지 않습니다.</InlineNotice>}
        {values.length ? <div className="history-chart" aria-label="최근 지연 관측 막대 그래프">
          {values.slice(-10).map((item, index) => { const delay = Number(item.delay || item.delay_minutes || 0); return <div key={`${item.timestamp || item.label || "history"}-${index}`}><span style={{ height: `${Math.max(12, (delay / maxDelay) * 100)}%` }} className={Number.isFinite(leg.buffer) && delay > leg.buffer ? "risk" : ""} /><small>{String(item.label || item.observed_at || index + 1).slice(5, 10)}</small></div>; })}
        </div> : <div className="history-empty"><Icon name="path" /><p>통과 이력이 없어 성공·실패를 판정하지 않습니다.</p></div>}
        <p className="chart-note">도착예정시간 관측값{Number.isFinite(leg.buffer) ? ` · 실제 시간표 기준 환승 여유 ${leg.buffer}분` : " · 시간표 환승 시각 DATA_GAP"}</p>
      </GlassCard>
    </main>
  );
}

function SimulationScreen({ journey, replayReady, connection, simulation, days, setDays, passageCoverage, mappingSummary, loading, onRun, onExplore }) {
  if (!journey) {
    return (
      <main className="screen content-screen simulation-screen">
        <ScreenHeading eyebrow="이력 재생" title="재생할 여행이 없습니다" detail="전국 탐색에서 실제 생성된 여행 후보를 먼저 선택하세요." />
        <GlassCard className="sim-control"><InlineNotice tone="warning" icon="map-trifold" title="DATA_GAP · 전국 여행 후보 필요">기본 고정 구간이나 샘플 성공률로 대신 계산하지 않습니다.</InlineNotice></GlassCard>
        <button className="liquid-button sticky-action" type="button" onClick={onExplore}>전국 탐색으로 가기 <Icon name="arrow-right" /></button>
      </main>
    );
  }
  const summary = simulation.summary || { probability: 0, successfulDays: 0, totalDays: days, weakestLeg: "집계 전", coverage: 0 };
  const allMapped = mappingSummary.total > 0 && mappingSummary.verified === mappingSummary.total;
  const canReplay = replayReady && connection.mode === "live" && allMapped;
  return (
    <main className="screen content-screen simulation-screen">
      <ScreenHeading eyebrow="이력 재생" title="날짜별 연결 결과" detail="검증된 시간표와 저장된 차량 통과 이력으로만 판정합니다." />
      <CoverageStrip mappingSummary={mappingSummary} coverage={passageCoverage} />
      {mappingSummary.verified < mappingSummary.total && <InlineNotice tone="warning" icon="map-pin-line" title="DATA_GAP · 공식 매핑 미완료">선택 여행 {mappingSummary.total}개 구간이 모두 검증되기 전에는 날짜별 결과를 판정하지 않습니다.</InlineNotice>}
      {!replayReady && <InlineNotice tone="warning" icon="clock" title="DATA_GAP · 실제 환승 시각 필요">후보에 검증된 시간표 출처, 도착 예정시각, 다음 출발시각, 최소 환승시간이 없습니다. 임의 시각이나 fixture 성공률을 사용하지 않습니다.</InlineNotice>}
      {replayReady && connection.mode !== "live" && <InlineNotice tone="warning" icon="database" title="DATA_GAP · TAGO LIVE 필요">실제 차량 통과 이력이 적재된 TAGO LIVE 연결 뒤에만 재생합니다.</InlineNotice>}
      <GlassCard className="sim-control">
        <label>분석 기간<Segmented value={days} onChange={setDays} label="분석 기간" options={[{ value: 7, label: "7일" }, { value: 14, label: "14일" }, { value: 30, label: "30일" }]} /></label>
        <button className="liquid-button" type="button" onClick={onRun} disabled={loading || !canReplay}>{loading ? "통과 이력 재생 중…" : "날짜별 실제 이력 재생"}<Icon name="sparkle" /></button>
      </GlassCard>

      <GlassCard className="sim-summary">
        <ProbabilityRing value={summary.probability || 0} />
        <div><p className="eyebrow">관측 결과</p><h2>{summary.dataGap ? "자료 부족" : summary.successfulDays}<span>{summary.dataGap ? " · DATA_GAP" : ` / ${summary.totalDays}일 성공`}</span></h2><p>{summary.dataGap ? "검증된 시각과 해당 날짜 통과 이력이 모두 필요합니다." : <>결과 요약은 <strong>{summary.weakestLeg}</strong>입니다.</>}</p><div className="coverage-row"><span><Icon name="database" /> 적재 통과 이벤트</span><strong>{summary.coverage || 0}건</strong></div></div>
      </GlassCard>

      <section className="daily-results">
        <div className="card-title"><div><p className="eyebrow">날짜별</p><h3>연결 성공 여부</h3></div><SourceBadge mode={simulation.mode || "offline"} label={simulation.mode === "live" ? "실제 통과 이력" : "DATA_GAP"} /></div>
        {simulation.perDay?.map((day) => (
          <article className="day-row" key={day.date}>
            <div className={`day-state ${day.status === "gap" ? "gap" : day.success ? "success" : "fail"}`}><Icon name={day.status === "gap" ? "question" : day.success ? "check" : "x"} /></div>
            <div className="day-copy"><strong>{day.date}</strong><small>{day.status === "gap" ? "DATA_GAP · 관측 부족" : day.success ? "모든 환승 성공" : (day.reasons?.[0] || "환승 실패")}</small></div>
            <div className="day-score"><strong>{Number.isFinite(day.probability) ? `${day.probability}%` : "—"}</strong><span><i style={{ width: `${Number.isFinite(day.probability) ? day.probability : 0}%` }} /></span></div>
          </article>
        ))}
      </section>
      <InlineNotice tone="neutral" icon="flask" title="결과 해석">TAGO는 과거 운행 이력을 소급 제공하지 않습니다. 연결 이후 적재한 실제 차량 통과와 검증된 시간표 시각이 함께 있는 날짜만 성공·실패로 판정하며, 나머지는 DATA_GAP입니다.</InlineNotice>
    </main>
  );
}

function validJourneyCoordinate(stop) {
  const latitude = Number(stop?.latitude ?? stop?.lat);
  const longitude = Number(stop?.longitude ?? stop?.lon);
  return Number.isFinite(latitude) && Number.isFinite(longitude)
    && latitude >= -90 && latitude <= 90 && longitude >= -180 && longitude <= 180;
}

function normalizeJourneyMapStop(stop) {
  return {
    ...stop,
    node_id: String(stop?.node_id || ""),
    node_name: String(stop?.node_name || stop?.node_id || "정류장"),
    node_order: Number(stop?.node_order || 0),
    latitude: Number(stop?.latitude ?? stop?.lat),
    longitude: Number(stop?.longitude ?? stop?.lon),
  };
}

function journeyStopsMatch(left, right) {
  return String(left?.city_code || "") === String(right?.city_code || "")
    && String(left?.node_id || "") === String(right?.node_id || "")
    && Number(left?.node_order) === Number(right?.node_order);
}

function summarizeJourneySections(journey) {
  const sections = [];
  let currentRide = null;
  for (const step of Array.isArray(journey?.steps) ? journey.steps : []) {
    const from = step?.from || {};
    const to = step?.to || {};
    const distance = Number(step?.distance_m);
    if (step?.kind === "ride" && step.route_id) {
      const routeId = String(step.route_id);
      const continues = currentRide
        && currentRide.routeId === routeId
        && journeyStopsMatch(currentRide.to, from);
      if (continues) {
        currentRide.to = to;
        currentRide.edgeCount += 1;
        currentRide.distanceM += Number.isFinite(distance) ? distance : 0;
        currentRide.stops.push(to);
      } else {
        currentRide = {
          kind: "ride",
          routeId,
          from,
          to,
          edgeCount: 1,
          distanceM: Number.isFinite(distance) ? distance : 0,
          stops: [from, to],
        };
        sections.push(currentRide);
      }
      continue;
    }
    currentRide = null;
    sections.push({
      kind: "transfer",
      routeId: "",
      from,
      to,
      edgeCount: 1,
      distanceM: Number.isFinite(distance) ? distance : 0,
      stops: [from, to],
    });
  }
  return sections;
}

function buildJourneyMapPayload(sections) {
  const lines = [];
  const stops = [];
  for (const section of sections) {
    if (section.kind !== "ride") continue;
    const routeStops = section.stops.filter(validJourneyCoordinate).map(normalizeJourneyMapStop);
    if (routeStops.length < 2) continue;
    lines.push(routeStops.map((stop) => [stop.longitude, stop.latitude]));
    routeStops.forEach((stop) => {
      const previous = stops[stops.length - 1];
      if (!previous || previous.node_id !== stop.node_id || previous.node_order !== stop.node_order) stops.push(stop);
    });
  }
  const geometry = lines.length === 1
    ? { type: "LineString", coordinates: lines[0] }
    : lines.length > 1
      ? { type: "MultiLineString", coordinates: lines }
      : null;
  return { geometry, stops };
}

const JOURNEY_GEOMETRY_REQUEST_CACHE = new Map();
const MAX_JOURNEY_GEOMETRY_CACHE = 12;
const MAX_JOURNEY_GEOMETRY_POINTS = 20000;

function normalizeJourneyGeometry(value) {
  const type = value?.type;
  const sourceLines = type === "LineString"
    ? [value.coordinates]
    : type === "MultiLineString"
      ? value.coordinates
      : null;
  if (!Array.isArray(sourceLines) || sourceLines.length === 0) return null;
  const lines = [];
  let pointCount = 0;
  for (const sourceLine of sourceLines) {
    if (!Array.isArray(sourceLine) || sourceLine.length < 2) return null;
    const line = [];
    for (const sourcePoint of sourceLine) {
      if (!Array.isArray(sourcePoint) || sourcePoint.length < 2) return null;
      const longitude = Number(sourcePoint[0]);
      const latitude = Number(sourcePoint[1]);
      if (!Number.isFinite(latitude) || !Number.isFinite(longitude)
        || latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) return null;
      pointCount += 1;
      if (pointCount > MAX_JOURNEY_GEOMETRY_POINTS) return null;
      line.push([longitude, latitude]);
    }
    lines.push(line);
  }
  return lines.length === 1
    ? { type: "LineString", coordinates: lines[0] }
    : { type: "MultiLineString", coordinates: lines };
}

function journeyGeometryLines(geometry) {
  if (geometry?.type === "LineString") return [geometry.coordinates];
  return geometry?.type === "MultiLineString" ? geometry.coordinates : [];
}

function buildJourneyGeometryRequests(sections) {
  return sections.filter((section) => section.kind === "ride" && section.routeId).map((section) => ({
    routeId: section.routeId,
    stops: section.stops.filter(validJourneyCoordinate).map(normalizeJourneyMapStop),
  })).filter((request) => request.stops.length >= 2);
}

function journeyGeometryRequestKey(requests) {
  if (requests.length === 0) return "journey-geometry:none";
  return JSON.stringify(requests.map((request) => [
    request.routeId,
    request.stops.map((stop) => [stop.node_id, stop.node_order, stop.latitude, stop.longitude]),
  ]));
}

function requestJourneyGeometry(requestKey, requests) {
  if (JOURNEY_GEOMETRY_REQUEST_CACHE.has(requestKey)) return JOURNEY_GEOMETRY_REQUEST_CACHE.get(requestKey);
  if (JOURNEY_GEOMETRY_REQUEST_CACHE.size >= MAX_JOURNEY_GEOMETRY_CACHE) {
    JOURNEY_GEOMETRY_REQUEST_CACHE.delete(JOURNEY_GEOMETRY_REQUEST_CACHE.keys().next().value);
  }
  const request = Promise.allSettled(requests.map((item) => BusroApi.routeGeometry(item.routeId, item.stops))).then((outcomes) => {
    if (outcomes.some((outcome) => outcome.status !== "fulfilled")) return { status: "gap" };
    const payloads = outcomes.map((outcome) => outcome.value);
    const sources = payloads.map((payload) => String(payload?.geometry_source || ""));
    if (sources.some((source) => !["osm_bus_relation", "osm_road_route_estimate"].includes(source))) return { status: "gap" };
    const geometries = payloads.map((payload) => normalizeJourneyGeometry(payload?.geometry));
    if (geometries.some((geometry) => !geometry)) return { status: "gap" };
    const lines = geometries.flatMap(journeyGeometryLines);
    if (lines.length === 0) return { status: "gap" };
    const geometry = lines.length === 1
      ? { type: "LineString", coordinates: lines[0] }
      : { type: "MultiLineString", coordinates: lines };
    const source = sources.every((item) => item === "osm_bus_relation")
      ? "osm_bus_relation"
      : sources.every((item) => item === "osm_road_route_estimate")
        ? "osm_road_route_estimate"
        : "mixed_osm_geometry";
    return {
      status: "ready",
      geometry,
      source,
      precision: [...new Set(payloads.map((payload) => String(payload?.precision || "")).filter(Boolean))].join(","),
    };
  }).catch(() => ({ status: "gap" }));
  JOURNEY_GEOMETRY_REQUEST_CACHE.set(requestKey, request);
  return request;
}

function formatJourneyDistance(value) {
  const distance = Number(value);
  if (!Number.isFinite(distance)) return "거리 DATA_GAP";
  return distance >= 1000 ? `${(distance / 1000).toFixed(1)}km` : `${Math.round(distance)}m`;
}

function safeJourneySourceUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return url.protocol === "https:" ? url.href : "";
  } catch { return ""; }
}

function parseJourneySource(value) {
  const raw = String(value || "").trim();
  let source = null;
  if (raw.startsWith("{")) {
    try { source = JSON.parse(raw); } catch { source = null; }
  }
  if (!source || Array.isArray(source) || typeof source !== "object") {
    return { key: raw || "unknown", label: raw || "출처 DATA_GAP", type: "경유 순서 근거", date: "", hash: "", url: "" };
  }
  const kind = String(source.kind || "");
  const dataset = String(source.dataset || source.name || "공식 교통 데이터").replace(/_\d{8}$/, "");
  return {
    key: raw,
    label: dataset,
    type: kind === "OFFICIAL_MUNICIPAL_ROUTE_STOP_CSV" ? "지자체 공식 경유 순서" : (kind || "공식 경로 근거"),
    date: String(source.route_date || source.source_date || ""),
    capturedAt: String(source.captured_at || ""),
    hash: String(source.file_sha256 || source.sha256 || ""),
    url: safeJourneySourceUrl(source.page || source.download),
  };
}

function collectJourneySources(journey) {
  const evidence = journey?.evidence && typeof journey.evidence === "object" ? journey.evidence : {};
  const rawSources = Array.isArray(evidence.sources) ? [...evidence.sources] : [];
  for (const step of Array.isArray(journey?.steps) ? journey.steps : []) {
    if (step?.evidence?.source) rawSources.push(step.evidence.source);
  }
  return [...new Set(rawSources.map((item) => String(item || "")).filter(Boolean))].map(parseJourneySource);
}

function journeyReasonLabel(reason) {
  return ({
    VERIFIED_TIMETABLE_REQUIRED: "검증된 시간표 없음",
    PASSAGE_HISTORY_REQUIRED: "실제 통과 이력 부족",
  })[reason] || reason;
}

function journeyMapPresentation(state, stopCount) {
  if (state.source === "osm_bus_relation") return {
    title: "OSM 버스 관계 형상",
    badge: "OSM route=bus",
    icon: "path",
    tone: "relation",
    detail: `OSM 버스 관계와 공식 정류장 ${stopCount}개를 함께 표시합니다. 실제 차량 GPS 궤적은 아닙니다.`,
  };
  if (state.source === "osm_road_route_estimate") return {
    title: "OSM/OSRM 도로 추정선",
    badge: "정류장 순서 기반",
    icon: "road-horizon",
    tone: "estimate",
    detail: `공식 정류장 ${stopCount}개의 운행 순서를 따라 도로망으로 추정했습니다. 실제 차량 GPS 궤적은 아닙니다.`,
  };
  if (state.source === "mixed_osm_geometry") return {
    title: "OSM 관계·도로 추정 혼합",
    badge: "구간별 형상",
    icon: "map-trifold",
    tone: "mixed",
    detail: `노선별 OSM 관계 또는 도로 추정 형상을 이어 표시합니다. 실제 차량 GPS 궤적은 아닙니다.`,
  };
  return {
    title: "공식 정류장 연결선",
    badge: state.status === "loading" ? "도로 형상 확인 중" : "도로 형상 DATA_GAP",
    icon: state.status === "loading" ? "spinner-gap" : "path",
    tone: state.status === "loading" ? "loading" : "gap",
    detail: `${state.status === "loading" ? "현재는" : "공개 도로 형상을 가져오지 못해"} 공식 경유 정류장 좌표 ${stopCount}개를 운행 순서대로 연결했습니다. 도로 주행궤적은 아닙니다.`,
  };
}

function JourneyRouteMap({ sections, fromName, toName }) {
  const mapPayload = buildJourneyMapPayload(sections);
  const geometryRequests = buildJourneyGeometryRequests(sections);
  const requestKey = journeyGeometryRequestKey(geometryRequests);
  const [resolvedGeometry, setResolvedGeometry] = useState({ key: "", status: "idle", geometry: null, source: "" });
  useEffect(() => {
    let active = true;
    if (!mapPayload.geometry || geometryRequests.length === 0) {
      setResolvedGeometry({ key: requestKey, status: "gap", geometry: null, source: "" });
      return () => { active = false; };
    }
    setResolvedGeometry({ key: requestKey, status: "loading", geometry: null, source: "" });
    requestJourneyGeometry(requestKey, geometryRequests).then((result) => {
      if (!active) return;
      setResolvedGeometry({ key: requestKey, ...result, geometry: result.geometry || null, source: result.source || "" });
    });
    return () => { active = false; };
  }, [requestKey]);

  const geometryState = resolvedGeometry.key === requestKey
    ? resolvedGeometry
    : { key: requestKey, status: geometryRequests.length ? "loading" : "gap", geometry: null, source: "" };
  const displayedGeometry = geometryState.status === "ready" && geometryState.geometry
    ? geometryState.geometry
    : mapPayload.geometry;
  const presentation = journeyMapPresentation(geometryState, mapPayload.stops.length);
  if (!mapPayload.geometry) {
    return <InlineNotice tone="warning" icon="map-trifold" title="지도 DATA_GAP">선택 경로의 공식 정류장 좌표가 없어 이동선을 표시할 수 없습니다.</InlineNotice>;
  }
  return <section className={`journey-route-map ${presentation.tone}`} aria-labelledby="journey-map-title">
    <OSMRouteMap
      geometry={displayedGeometry}
      stops={mapPayload.stops}
      positions={[]}
      loading={false}
      ariaLabel={`${fromName}에서 ${toName}까지 ${presentation.title} 지도`}
      badgeLabel="OpenStreetMap"
    />
    <div className="journey-map-caption"><span><Icon name={presentation.icon} /></span><div><div className="journey-map-title-row"><strong id="journey-map-title">{presentation.title}</strong><em>{presentation.badge}</em></div><small>{presentation.detail}</small></div></div>
  </section>;
}

function JourneyScreen({ journey, onExplore }) {
  if (!journey) {
    return (
      <main className="screen content-screen journey-screen">
        <ScreenHeading eyebrow="선택한 여정" title="선택된 버스 여행이 없습니다" detail="전국 탐색에서 출발·도착 정류장을 고르고 생성된 후보를 선택하세요." />
        <GlassCard className="ticket-card">
          <InlineNotice tone="neutral" icon="map-trifold" title="전국 경로 탐색">공식 정류장 순서가 적재된 노선만 여행 후보로 사용합니다.</InlineNotice>
        </GlassCard>
        <button className="liquid-button sticky-action" type="button" onClick={onExplore}>전국 탐색으로 가기 <Icon name="arrow-right" /></button>
      </main>
    );
  }

  const fromStop = journey.from_stop || journey.from || {};
  const toStop = journey.to_stop || journey.to || {};
  const fromName = fromStop.node_name || fromStop.stop_name || fromStop.node_id || "DATA_GAP";
  const toName = toStop.node_name || toStop.stop_name || toStop.node_id || "DATA_GAP";
  const routeIds = Array.isArray(journey.route_ids) ? journey.route_ids.filter(Boolean) : [];
  const steps = Array.isArray(journey.steps) ? journey.steps : [];
  const sections = summarizeJourneySections(journey);
  const reasons = Array.isArray(journey.reasons) ? journey.reasons.filter(Boolean) : [];
  const status = journey.status || "DATA_GAP";
  const hasProbability = typeof journey.success_probability === "number" && Number.isFinite(journey.success_probability);
  const coverage = journey.coverage && typeof journey.coverage === "object" ? journey.coverage : {};
  const evidence = journey.evidence && typeof journey.evidence === "object" ? journey.evidence : {};
  const sources = collectJourneySources(journey);

  return (
    <main className="screen content-screen journey-screen">
      <ScreenHeading eyebrow="선택한 여정" title={`${fromName} → ${toName}`} detail="공식 경유 정류장으로 생성된 경로입니다. 관측 근거가 있는 정보만 표시합니다." />
      <JourneyRouteMap sections={sections} fromName={fromName} toName={toName} />
      <GlassCard className="ticket-card">
        <div className="ticket-route"><div><small>출발 정류장</small><strong>{fromName}</strong><span>{fromStop.node_id || "ID DATA_GAP"}</span></div><div className="ticket-line"><span /><Icon name="bus" /><span /></div><div><small>도착 정류장</small><strong>{toName}</strong><span>{toStop.node_id || "ID DATA_GAP"}</span></div></div>
        <div className="ticket-meta">
          <span><Icon name="bus" /> 노선 {routeIds.length || "DATA_GAP"}</span>
          {typeof journey.transfers === "number" && <span><Icon name="arrows-left-right" /> {journey.transfers}회 환승</span>}
          {typeof journey.walking_m === "number" && <span><Icon name="person-simple-walk" /> {Math.round(journey.walking_m)}m</span>}
        </div>
        {routeIds.length > 0 && <div className="ticket-meta">{routeIds.map((routeId) => <span key={routeId}><Icon name="path" /> {routeId}</span>)}</div>}
      </GlassCard>
      <section className="leg-timeline">
        {sections.map((section, index) => {
          const stepFrom = section.from || {};
          const stepTo = section.to || {};
          const isTransfer = section.kind === "transfer";
          const stopCount = section.edgeCount + 1;
          const intermediateCount = Math.max(0, stopCount - 2);
          const stepLabel = isTransfer ? "환승" : (section.routeId || "노선 DATA_GAP");
          return (
          <article key={`${section.kind || "step"}-${section.routeId || "none"}-${index}`} className={index === 0 ? "current" : ""}>
            <div className="leg-rail"><span>{index + 1}</span><i /></div>
            <div className="leg-card"><div><p><span className="line-chip blue">{stepLabel}</span>{isTransfer ? "정류장 간 이동" : "버스 승차 구간"}</p><h3>{stepFrom.node_name || stepFrom.node_id || "DATA_GAP"} → {stepTo.node_name || stepTo.node_id || "DATA_GAP"}</h3><small>{isTransfer ? `도보 연결 · ${formatJourneyDistance(section.distanceM)}` : `총 ${stopCount}개 정류장 · 중간 경유 ${intermediateCount}개 · 좌표 간 ${formatJourneyDistance(section.distanceM)}`}</small></div></div>
          </article>
          );
        })}
      </section>
      {steps.length === 0 && <InlineNotice tone="warning" icon="warning-circle" title="DATA_GAP">이 후보에 표시할 경로 단계가 없습니다.</InlineNotice>}
      {sources.length > 0 && <details className="journey-evidence">
        <summary><span><Icon name="seal-check" /><span><strong>공식 경로 근거</strong><small>{sources.length}개 출처 · 원문 정보는 여기서 한 번만 표시합니다.</small></span></span><Icon name="caret-down" /></summary>
        <div className="journey-evidence-list">{sources.map((source) => <article key={source.key}>
          <span>{source.type}</span><strong>{source.label}</strong>
          <small>{source.date ? `기준일 ${source.date}` : "기준일 DATA_GAP"}{source.hash ? ` · SHA-256 ${source.hash.slice(0, 12)}…` : ""}</small>
          {source.url && <a href={source.url} target="_blank" rel="noreferrer">공식 원문 보기 <Icon name="arrow-square-out" /></a>}
        </article>)}</div>
      </details>}
      <InlineNotice tone={status === "READY" ? "success" : "warning"} icon={status === "READY" ? "shield-check" : "warning-circle"} title={status}>
        {reasons.length > 0 ? reasons.map(journeyReasonLabel).join(" · ") : "추가 결측 사유 없음"}
        {` · 성공률 ${hasProbability ? `${Math.round(journey.success_probability * 100)}%` : "DATA_GAP"}`}
        {typeof coverage.schedule_routes === "number" && typeof coverage.total_routes === "number" ? ` · 시간표 근거 ${coverage.schedule_routes}/${coverage.total_routes}` : ""}
        {typeof coverage.passage_routes === "number" && typeof coverage.total_routes === "number" ? ` · 통과 이력 ${coverage.passage_routes}/${coverage.total_routes}` : ""}
        {evidence.topology ? " · 검증된 단방향 경유 순서" : ""}
      </InlineNotice>
      <button className="liquid-button sticky-action" type="button" onClick={onExplore}>다른 전국 여행 찾기 <Icon name="arrow-right" /></button>
    </main>
  );
}

function SettingsSheet({ open, onClose, apiBase, setApiBase, connection, journey, mappings, legs, mappingSummary, settingsError, onMappingChange, onVerifyMapping, onReconnect }) {
  if (!open) return null;
  return (
    <div className="sheet-layer" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="settings-sheet" role="dialog" aria-modal="true" aria-labelledby="settings-title">
        <div className="sheet-grabber" /><div className="sheet-title"><div><p className="eyebrow">데이터 연결</p><h2 id="settings-title">공식 교통 데이터</h2></div><button type="button" onClick={onClose} aria-label="닫기"><Icon name="x" /></button></div>
        <InlineNotice tone={connection.mode === "live" ? "success" : "warning"} icon={connection.mode === "live" ? "cloud-check" : "key"} title={connection.label}>{connection.message}</InlineNotice>
        <label className="api-field"><span>로컬 데이터 서비스 주소</span><input value={apiBase} onChange={(event) => setApiBase(event.target.value)} placeholder="http://127.0.0.1:8791/api" /></label>
        <p className="privacy-note"><Icon name="shield-check" /> TAGO 서비스 키는 브라우저에 입력하거나 저장하지 않습니다. 서버 환경변수에서만 읽습니다.</p>

        {!journey || legs.length === 0 ? (
          <section className="mapping-settings" aria-labelledby="mapping-title">
            <div className="mapping-settings-head"><div><p className="eyebrow">공식 식별자</p><h3 id="mapping-title">노선 매핑</h3></div><strong>0/0</strong></div>
            <InlineNotice tone="warning" icon="map-trifold" title="전국 여행 후보 먼저 선택">선택한 후보의 연속 버스 이동 구간만 cityCode · nodeId · routeId 검증 대상으로 표시합니다. 기존 고정 구간은 사용하지 않습니다.</InlineNotice>
          </section>
        ) : <section className="mapping-settings" aria-labelledby="mapping-title">
          <div className="mapping-settings-head"><div><p className="eyebrow">공식 식별자</p><h3 id="mapping-title">노선 매핑</h3></div><strong>{mappingSummary.verified}/{mappingSummary.total}</strong></div>
          <p className="mapping-help">선택한 전국 여행의 승차 정류장 cityCode · nodeId · routeId만 로컬에 저장합니다. 서버가 해당 노선의 경유 정류장으로 검증하지 못하면 DATA_GAP입니다.</p>
          <div className="mapping-leg-list">
            {legs.map((leg, index) => {
              const mapping = mappings[leg.id] || {};
              const complete = Boolean(mapping.cityCode && mapping.nodeId && mapping.routeId);
              return (
                <article className="mapping-leg" key={leg.id}>
                    <div className="mapping-leg-title"><span>{index + 1}</span><div><strong>{leg.city} {leg.routeNo}</strong><small>{leg.board} → {leg.alight} · 순번 {Number.isInteger(leg.nodeOrder) ? leg.nodeOrder : "DATA_GAP"}</small></div><MappingBadge state={mapping.state} /></div>
                  <div className="mapping-fields">
                    <label><span>cityCode</span><input value={mapping.cityCode || ""} onChange={(event) => onMappingChange(leg.id, "cityCode", event.target.value)} inputMode="numeric" autoComplete="off" aria-label={`${leg.city} ${leg.routeNo} cityCode`} /></label>
                    <label><span>nodeId</span><input value={mapping.nodeId || ""} onChange={(event) => onMappingChange(leg.id, "nodeId", event.target.value)} autoCapitalize="characters" autoComplete="off" aria-label={`${leg.city} ${leg.routeNo} nodeId`} /></label>
                    <label><span>routeId</span><input value={mapping.routeId || ""} onChange={(event) => onMappingChange(leg.id, "routeId", event.target.value)} autoCapitalize="characters" autoComplete="off" aria-label={`${leg.city} ${leg.routeNo} routeId`} /></label>
                  </div>
                  <div className="mapping-leg-foot"><p>{mapping.note || "서버 검증 전"}</p><button type="button" disabled={!complete || mapping.state === "checking"} onClick={() => onVerifyMapping(leg.id)}>{mapping.state === "checking" ? "검증중" : "서버 검증"}<Icon name={mapping.state === "checking" ? "spinner-gap" : "arrow-right"} /></button></div>
                </article>
              );
            })}
          </div>
        </section>}

        {settingsError && <InlineNotice tone="danger" icon="warning-circle" title="저장할 수 없음">{settingsError}</InlineNotice>}
        <button className="liquid-button settings-save" type="button" onClick={onReconnect}>주소·식별자 저장 후 연결 확인 <Icon name="plug" /></button>
      </section>
    </div>
  );
}
