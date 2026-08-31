const { useRef } = React;

function normalizeCity(item) {
  return { code: String(item.city_code ?? item.citycode ?? item.code ?? ""), name: String(item.city_name ?? item.cityname ?? item.name ?? "") };
}

function normalizeRoute(item) {
  return {
    ...item,
    routeId: String(item.route_id ?? item.routeid ?? ""),
    routeNo: String(item.route_no ?? item.routeno ?? item.route_name ?? ""),
    routeType: String(item.route_type ?? item.routetp ?? "시내버스"),
    startName: String(item.start_node_name ?? item.startnodenm ?? "기점 정보 없음"),
    endName: String(item.end_node_name ?? item.endnodenm ?? "종점 정보 없음"),
  };
}

function normalizeStop(item, index = 0) {
  return {
    ...item,
    node_id: String(item.node_id ?? item.nodeid ?? item.stop_id ?? ""),
    node_name: String(item.node_name ?? item.nodenm ?? item.stop_name ?? "정류장"),
    node_order: Number(item.node_order ?? item.nodeord ?? item.stop_sequence ?? index + 1),
    latitude: Number(item.latitude ?? item.gpslati ?? item.lat),
    longitude: Number(item.longitude ?? item.gpslong ?? item.lon),
    city_code: String(item.city_code ?? item.citycode ?? ""),
    city_name: String(item.city_name ?? item.cityname ?? ""),
  };
}

function OSMRouteMap({ geometry, stops, positions, loading, ariaLabel = "OpenStreetMap 기반 전국 버스 지도", badgeLabel = "OSM" }) {
  const elementRef = useRef(null);
  const mapRef = useRef(null);
  useEffect(() => {
    if (!elementRef.current || !window.BusroMap) return undefined;
    mapRef.current = BusroMap.create(elementRef.current);
    return () => { mapRef.current?.destroy(); mapRef.current = null; };
  }, []);
  useEffect(() => { mapRef.current?.render({ geometry, stops, positions }); }, [geometry, stops, positions]);
  return (
    <div className="osm-map-wrap">
      <div ref={elementRef} className="osm-map" aria-label={ariaLabel} />
      <span className="osm-attribution-pill"><Icon name="globe-hemisphere-east" /> {badgeLabel}</span>
      {loading && <div className="map-loading"><span /><p>공식 정류장과 노선 형상 불러오는 중</p></div>}
    </div>
  );
}

function PrecisionBadge({ geometryPayload }) {
  if (!geometryPayload) return <span className="precision-badge gap"><Icon name="warning-circle" /> 형상 대기</span>;
  const relation = geometryPayload.geometry_source === "osm_bus_relation";
  return <span className={`precision-badge ${relation ? "relation" : "estimate"}`}><Icon name={relation ? "path" : "road-horizon"} />{relation ? "OSM 버스 관계" : "정류장 순서 도로 추정"}</span>;
}

function RouteBrowser({ connection, onUseStop }) {
  const [cities, setCities] = useState([]);
  const [cityCode, setCityCode] = useState("");
  const [routeQuery, setRouteQuery] = useState("");
  const [routes, setRoutes] = useState([]);
  const [selected, setSelected] = useState(null);
  const [routeInfo, setRouteInfo] = useState(null);
  const [stops, setStops] = useState([]);
  const [positions, setPositions] = useState([]);
  const [geometryPayload, setGeometryPayload] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [hydrationGap, setHydrationGap] = useState("");

  useEffect(() => {
    let active = true;
    BusroApi.cities().then((payload) => {
      if (!active) return;
      const normalized = (payload.cities || payload.items || []).map(normalizeCity).filter((item) => item.code && item.name);
      setCities(normalized); setCityCode((value) => value || normalized[0]?.code || "");
    }).catch((reason) => active && setError(reason.status === 503 ? "TAGO 공식 데이터 연결이 준비되지 않았습니다." : "전국 도시 목록을 불러오지 못했습니다."));
    return () => { active = false; };
  }, []);

  async function search(event) {
    event?.preventDefault();
    if (!cityCode) return;
    setLoading(true); setError(""); setSelected(null); setStops([]); setGeometryPayload(null);
    try {
      const payload = await BusroApi.routes(cityCode, routeQuery);
      setRoutes((payload.routes || payload.items || []).map(normalizeRoute).filter((item) => item.routeId));
    } catch (reason) { setRoutes([]); setError(reason.message || "노선을 검색하지 못했습니다."); }
    finally { setLoading(false); }
  }

  async function openRoute(route) {
    setSelected(route); setLoading(true); setError(""); setHydrationGap(""); setGeometryPayload(null); setPositions([]);
    try {
      const [stopPayload, infoPayload, positionPayload] = await Promise.all([
        BusroApi.routeStops(cityCode, route.routeId),
        BusroApi.routeInfo(cityCode, route.routeId).catch(() => ({ route })),
        BusroApi.positions({ cityCode, routeId: route.routeId }).catch(() => ({ positions: [] })),
      ]);
      const normalizedStops = (stopPayload.stops || stopPayload.items || []).map(normalizeStop).filter((item) => Number.isFinite(item.latitude) && Number.isFinite(item.longitude));
      setStops(normalizedStops);
      setRouteInfo(normalizeRoute(infoPayload.route || infoPayload.item || route));
      setPositions(positionPayload.positions || []);
      if (connection.hydrationReady) {
        try { await BusroApi.hydrateRoute(cityCode, route.routeId); }
        catch (reason) { setHydrationGap(reason.message || "공식 경유 순서를 여행 그래프에 적재하지 못했습니다."); }
      } else setHydrationGap("검증된 공유 카탈로그 쓰기가 비활성화되어 새 노선은 적재하지 않았습니다.");
      if (normalizedStops.length >= 2) {
        try { setGeometryPayload(await BusroApi.routeGeometry(route.routeNo, normalizedStops)); }
        catch (reason) { setError(`노선은 찾았지만 OSM 형상을 만들지 못했습니다. ${reason.message || "DATA_GAP"}`); }
      } else setError("공식 경유 정류장의 좌표가 2개 미만이라 지도 형상을 만들 수 없습니다.");
    } catch (reason) { setStops([]); setError(reason.message || "경유 정류장을 불러오지 못했습니다."); }
    finally { setLoading(false); }
  }

  return (
    <>
      <OSMRouteMap geometry={geometryPayload?.geometry} stops={stops} positions={positions} loading={loading} />
      <GlassCard className="nation-search-card">
        <div className="nation-mode-title"><div><p className="eyebrow">DATA ADMIN · ROUTE INSPECTOR</p><h1>개별 노선<br /><em>데이터 검증</em></h1></div><SourceBadge mode={connection.mode} label={connection.label} /></div>
        <form className="route-search-form" onSubmit={search}>
          <label><span>지역</span><select value={cityCode} onChange={(event) => setCityCode(event.target.value)} aria-label="버스 지역 선택"><option value="">지역 선택</option>{cities.map((city) => <option key={city.code} value={city.code}>{city.name}</option>)}</select></label>
          <label><span>노선번호</span><input value={routeQuery} onChange={(event) => setRouteQuery(event.target.value)} placeholder="예: 601" maxLength="24" /></label>
          <button type="submit" disabled={!cityCode || loading}><Icon name="magnifying-glass" />{loading ? "조회 중" : "노선 찾기"}</button>
        </form>
        <p className="source-note"><Icon name="database" /> 지역·노선·정류장은 TAGO 공식 식별자로 조회합니다. 서비스 키는 브라우저에 저장하지 않습니다.</p>
      </GlassCard>

      {error && <InlineNotice tone="warning" icon="warning-circle" title="DATA_GAP">{error}</InlineNotice>}
      {hydrationGap && <InlineNotice tone="warning" icon="database" title="그래프 DATA_GAP">{hydrationGap} 노선 지도와 정류장 보기는 계속 사용할 수 있습니다.</InlineNotice>}
      {routes.length > 0 && <section className="route-catalog"><div className="catalog-heading"><div><p className="eyebrow">ROUTES</p><h2>{cities.find((item) => item.code === cityCode)?.name || "선택 지역"} 노선</h2></div><span>{routes.length}개</span></div><div className="route-result-list">{routes.map((route) => <button type="button" key={route.routeId} className={selected?.routeId === route.routeId ? "active" : ""} onClick={() => openRoute(route)}><span className="route-number">{route.routeNo || "—"}</span><span><strong>{route.startName} → {route.endName}</strong><small>{route.routeType} · ID {route.routeId}</small></span><Icon name="caret-right" /></button>)}</div></section>}

      {selected && <GlassCard className="selected-route-card">
        <div className="selected-route-head"><div><p className="eyebrow">SELECTED LINE</p><h2><span>{selected.routeNo}</span>{routeInfo?.routeType || selected.routeType}</h2></div><PrecisionBadge geometryPayload={geometryPayload} /></div>
        <div className="route-terminal-row"><div><small>기점</small><strong>{routeInfo?.startName || selected.startName}</strong><span>{routeInfo?.first_vehicle_time || "시간표 출처 확인 필요"}</span></div><Icon name="arrow-right" /><div><small>종점</small><strong>{routeInfo?.endName || selected.endName}</strong><span>{routeInfo?.last_vehicle_time || "시간표 출처 확인 필요"}</span></div></div>
        <div className="route-evidence-row"><span><Icon name="map-pin" /> 경유 {stops.length}개</span><span><Icon name="bus" /> 현재 차량 {positions.length}대</span><span><Icon name="clock" /> 평일 배차 {routeInfo?.weekday_interval_minutes || "—"}분</span></div>
        {geometryPayload?.data_gap && <p className="geometry-caveat">{geometryPayload.data_gap}</p>}
        <div className="stop-preview-list">{stops.slice(0, 8).map((stop, index) => <button type="button" key={`${stop.node_id}-${index}`} onClick={() => onUseStop(stop)}><span>{stop.node_order || index + 1}</span><strong>{stop.node_name}</strong><small>{stop.node_id}</small></button>)}</div>
        {stops.length > 8 && <p className="more-stops">외 {stops.length - 8}개 정류장 · 지도에서 전체 확인</p>}
      </GlassCard>}
    </>
  );
}

function StopLookup({ label, value, onChange, selected, onSelect, cityCode }) {
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const query = String(value || "").trim();
    if (selected || query.length < 2) { setResults([]); setLoading(false); return undefined; }
    let active = true;
    const timer = window.setTimeout(async () => {
      setLoading(true);
      try {
        const payload = await BusroApi.searchStops(query, cityCode);
        if (active) setResults((payload.stops || payload.items || []).map(normalizeStop).slice(0, 8));
      } catch { if (active) setResults([]); }
      finally { if (active) setLoading(false); }
    }, 260);
    return () => { active = false; window.clearTimeout(timer); };
  }, [value, selected, cityCode]);
  return <div className="stop-lookup"><label><span>{label}</span><div className="stop-input-shell"><Icon name="map-pin" /><input value={selected ? selected.node_name : value} onChange={(event) => { onSelect(null); onChange(event.target.value); }} placeholder="전국 정류장명 2자 이상" autoComplete="off" /><span className={loading ? "lookup-state loading" : "lookup-state"}><Icon name={loading ? "spinner-gap" : selected ? "check-circle" : "magnifying-glass"} /></span></div></label>{results.length > 0 && !selected && <div className="stop-suggestions">{results.map((stop, index) => <button type="button" key={`${stop.city_code}-${stop.node_id}-${index}`} onClick={() => { onSelect(stop); onChange(stop.node_name); setResults([]); }}><strong>{stop.node_name}</strong><small><span>{stop.city_name || stop.city_code || "지역 미상"} · {stop.node_id}</span><em className={stop.graph_ready ? "graph-ready" : "graph-gap"}>{stop.graph_ready ? "여행 경로 연결" : "정류장 정보만"}</em></small></button>)}</div>}</div>;
}

function formatCount(value) {
  return Number.isFinite(Number(value)) ? Number(value).toLocaleString("ko-KR") : "—";
}

function localDateValue(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function localTimeValue(date = new Date()) {
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function gtfsClockMinutes(value) {
  const clean = String(value ?? "").trim();
  const matched = clean.match(/^(\d{1,3}):([0-5]\d)(?::[0-5]\d)?$/);
  return matched ? Number(matched[1]) * 60 + Number(matched[2]) : null;
}

function formatGtfsClock(value, secondsValue) {
  let totalMinutes = gtfsClockMinutes(value);
  if (totalMinutes === null && secondsValue !== null && secondsValue !== undefined && String(secondsValue).trim() !== "" && Number.isFinite(Number(secondsValue))) totalMinutes = Math.floor(Number(secondsValue) / 60);
  if (!Number.isFinite(totalMinutes) || totalMinutes < 0) return null;
  const dayOffset = Math.floor(totalMinutes / 1440);
  const minuteOfDay = totalMinutes % 1440;
  const hour = String(Math.floor(minuteOfDay / 60)).padStart(2, "0");
  const minute = String(minuteOfDay % 60).padStart(2, "0");
  return `${hour}:${minute}${dayOffset ? ` (+${dayOffset}일)` : ""}`;
}

function replayClock(value, minutesValue) {
  const fromRaw = gtfsClockMinutes(value);
  const minutes = fromRaw ?? (minutesValue === null || minutesValue === undefined || String(minutesValue).trim() === "" ? NaN : Number(minutesValue));
  if (!Number.isFinite(minutes) || minutes < 0) return null;
  const minuteOfDay = Math.floor(minutes) % 1440;
  return `${String(Math.floor(minuteOfDay / 60)).padStart(2, "0")}:${String(minuteOfDay % 60).padStart(2, "0")}`;
}

function normalizeSchedule(result) {
  const schedule = result?.schedule && typeof result.schedule === "object" ? result.schedule : {};
  const status = String(schedule.status || result?.schedule_status || "DATA_GAP").toUpperCase();
  const ready = ["READY", "AVAILABLE", "SCHEDULE_READY", "OK"].includes(status);
  return {
    ready,
    status: ready ? "READY" : "DATA_GAP",
    reason: schedule.reason || result?.schedule_reason || result?.reason || "SCHEDULE_DATA_GAP",
    serviceDate: schedule.service_date || result?.service_date || "",
    departureTime: schedule.departure_time || result?.departure_time || "",
    basis: schedule.basis || result?.schedule_basis || "",
    provider: schedule.provider || result?.schedule_provider || "",
    feedId: schedule.feed_id || result?.schedule_feed_id || "",
  };
}

function scheduleEvidence(resultSchedule, candidate) {
  const provenance = candidate?.provenance && typeof candidate.provenance === "object" ? candidate.provenance : {};
  const candidateEvidence = candidate?.evidence && typeof candidate.evidence === "object" ? candidate.evidence : {};
  const replaySource = Array.isArray(candidate?.replay_legs) ? candidate.replay_legs.find((item) => item?.time_evidence_source)?.time_evidence_source : "";
  const source = replaySource && typeof replaySource === "object" ? replaySource : {};
  const providerRaw = provenance.provider || candidateEvidence.provider || resultSchedule.provider || source.provider || "";
  const provider = String(providerRaw).toLowerCase() === "ktdb" ? "KTDB" : String(providerRaw);
  const basis = provenance.basis || candidateEvidence.basis || resultSchedule.basis || source.basis || "";
  const feedId = provenance.feed_id || candidateEvidence.feed_id || candidateEvidence.source_id || resultSchedule.feedId || source.source_id || (typeof replaySource === "string" ? replaySource : "");
  const official = /OFFICIAL/i.test(String(basis));
  const basisLabel = basis === "OFFICIAL_STATIC_GTFS_RAW_EVIDENCE" ? "공식 정적 GTFS 원본 근거" : String(basis || "").replaceAll("_", " ");
  return {
    ready: Boolean(provider || feedId || basis),
    label: [provider ? `${provider}${official ? " 공식 GTFS" : " GTFS"}` : "", feedId, basisLabel].filter(Boolean).join(" · "),
  };
}

function summarizeJourneyLegs(candidate) {
  const legs = [];
  for (const step of Array.isArray(candidate?.steps) ? candidate.steps : []) {
    if (step?.kind !== "ride" || !step.route_id) continue;
    const routeId = String(step.route_id);
    const tripId = String(step.trip_id || "");
    const departureTime = formatGtfsClock(step.departure_time ?? step.from?.departure_time, step.departure_seconds ?? step.from?.departure_seconds);
    const arrivalTime = formatGtfsClock(step.arrival_time ?? step.to?.arrival_time, step.arrival_seconds ?? step.to?.arrival_seconds);
    const previous = legs[legs.length - 1];
    if (previous && previous.routeId === routeId && (!previous.tripId || !tripId || previous.tripId === tripId)) {
      previous.to = step.to || previous.to;
      previous.edgeCount += 1;
      previous.arrivalTime = arrivalTime || previous.arrivalTime;
    } else {
      legs.push({ routeId, tripId, from: step.from || {}, to: step.to || {}, edgeCount: 1, departureTime, arrivalTime });
    }
  }
  const replayRows = Array.isArray(candidate?.replay_legs) ? candidate.replay_legs : [];
  legs.forEach((leg, index) => {
    const replayRow = replayRows[index] || {};
    const scheduledSeconds = replayRow.scheduled_minutes === null || replayRow.scheduled_minutes === undefined ? undefined : Number(replayRow.scheduled_minutes) * 60;
    const nextDepartureSeconds = replayRow.next_departure_minutes === null || replayRow.next_departure_minutes === undefined ? undefined : Number(replayRow.next_departure_minutes) * 60;
    leg.arrivalTime ||= formatGtfsClock(replayRow.scheduled_arrival, scheduledSeconds);
    leg.nextDepartureTime = formatGtfsClock(replayRow.next_departure, nextDepartureSeconds);
  });
  return legs;
}

function prepareJourneyForDetail(candidate, context) {
  const replayLegs = (Array.isArray(candidate?.replay_legs) ? candidate.replay_legs : []).map((row) => {
    const sourceId = typeof row.time_evidence_source === "object" ? String(row.time_evidence_source.source_id || "") : String(row.time_evidence_source || "");
    const scheduledMinutes = gtfsClockMinutes(row.scheduled_arrival) ?? (row.scheduled_minutes !== null && row.scheduled_minutes !== undefined && Number.isFinite(Number(row.scheduled_minutes)) ? Number(row.scheduled_minutes) : null);
    const nextDepartureMinutes = gtfsClockMinutes(row.next_departure) ?? (row.next_departure_minutes !== null && row.next_departure_minutes !== undefined && Number.isFinite(Number(row.next_departure_minutes)) ? Number(row.next_departure_minutes) : null);
    return {
      ...row,
      scheduled_gtfs_time: String(row.scheduled_arrival || ""),
      next_departure_gtfs_time: String(row.next_departure || ""),
      scheduled_minutes: scheduledMinutes,
      next_departure_minutes: nextDepartureMinutes,
      scheduled_day_offset: scheduledMinutes === null ? null : Math.floor(scheduledMinutes / 1440),
      next_departure_day_offset: nextDepartureMinutes === null ? null : Math.floor(nextDepartureMinutes / 1440),
      scheduled_arrival: replayClock(row.scheduled_arrival, scheduledMinutes),
      next_departure: replayClock(row.next_departure, nextDepartureMinutes),
      time_evidence_source: sourceId,
      time_evidence_verified: row.time_evidence_verified === true || Boolean(row.time_evidence_trip_id && sourceId),
      time_evidence_feed_id: String(row.time_evidence_feed_id || ""),
      next_time_evidence_feed_id: String(row.next_time_evidence_feed_id || ""),
    };
  });
  return { ...candidate, ...context, replay_legs: replayLegs };
}

const JOURNEY_CRITERION_LABELS = {
  minimum_transfers: "최소 환승",
  generalized_cost: "균형 경로",
  explorer: "탐험 경로",
  earliest_arrival: "가장 이른 도착",
};

function JourneyCandidateCard({ candidate, index, schedule, structural = false, context, onChooseJourney }) {
  const routeIds = Array.isArray(candidate?.route_ids) ? candidate.route_ids.filter(Boolean) : [];
  const legs = summarizeJourneyLegs(candidate);
  const coverage = candidate?.coverage && typeof candidate.coverage === "object" ? candidate.coverage : {};
  const evidence = candidate?.evidence && typeof candidate.evidence === "object" ? candidate.evidence : {};
  const provenance = scheduleEvidence(schedule, candidate);
  const hasProbability = typeof candidate?.success_probability === "number" && Number.isFinite(candidate.success_probability);
  const departureTime = formatGtfsClock(candidate?.departure_time, candidate?.departure_seconds);
  const arrivalTime = formatGtfsClock(candidate?.arrival_time, candidate?.arrival_seconds);
  const minutes = Number(candidate?.estimated_minutes);
  const timeSummary = [departureTime ? `출발 ${departureTime}` : "", arrivalTime ? `도착 ${arrivalTime}` : "", Number.isFinite(minutes) ? `${Math.max(0, Math.round(minutes))}분` : ""].filter(Boolean);
  return <article className={structural ? "structural-candidate" : "scheduled-candidate"}>
    <div className="candidate-rank">{index + 1}</div>
    <div className="candidate-copy">
      <div className="candidate-title"><div><p>{JOURNEY_CRITERION_LABELS[candidate?.criterion] || candidate?.criterion || (structural ? "방향 경로 후보" : "시간표 경로")}</p><h3>{routeIds.length > 0 ? `${candidate?.transfers || 0}회 환승 · ${routeIds.length}개 노선` : "노선 DATA_GAP"}</h3></div><small className={structural ? "schedule-gap" : "schedule-ready"}>{structural ? "시간 미검증" : "시간표 확인"}</small></div>
      {!structural && timeSummary.length > 0 && <div className="schedule-summary"><Icon name="clock" /><strong>{timeSummary.join(" · ")}</strong></div>}
      {structural && <div className="schedule-gap-copy"><Icon name="warning-circle" /><span>정류장 진행 방향만 확인했습니다. 이 날짜·시각에 실제 운행 가능한 경로로 확정되지 않았습니다.</span></div>}
      <div className="candidate-leg-list">{legs.map((leg, legIndex) => <div className="candidate-leg" key={`${leg.routeId}-${leg.tripId}-${legIndex}`}>
        <span className="timeline-rail"><i /><b /></span>
        <div className="leg-copy"><span className="route-pill"><Icon name="bus" /> {leg.routeId}</span><strong>{leg.from?.node_name || leg.from?.node_id || "승차 정류장"}</strong>{!structural && leg.departureTime && <span className="leg-time"><Icon name="clock" /> {leg.departureTime} 출발</span>}<small>총 {leg.edgeCount + 1}개 정류장</small><strong>{leg.to?.node_name || leg.to?.node_id || "하차 정류장"}</strong>{!structural && leg.arrivalTime && <span className="leg-time arrival"><Icon name="clock" /> {leg.arrivalTime} 도착</span>}{!structural && leg.nextDepartureTime && <span className="transfer-time">다음 버스 {leg.nextDepartureTime} 출발</span>}</div>
      </div>)}</div>
      <footer>
        <span><Icon name="arrows-left-right" /> {typeof candidate?.transfers === "number" ? `${candidate.transfers}회 환승` : "환승 DATA_GAP"}</span>
        <span><Icon name="database" /> 승차 {evidence.ride_edges ?? "—"} · 환승 {evidence.transfer_edges ?? "—"} 간선</span>
        <strong>{hasProbability ? `성공률 ${Math.round(candidate.success_probability * 100)}%` : "성공률 DATA_GAP"}</strong>
      </footer>
      {!structural && <small className={provenance.ready ? "official-schedule-evidence" : "schedule-evidence-gap"}><Icon name={provenance.ready ? "shield-check" : "warning-circle"} /> {provenance.ready ? provenance.label : "시간표 출처 DATA_GAP"}</small>}
      {typeof coverage.schedule_routes === "number" && typeof coverage.total_routes === "number" && <small className="evidence-copy">시간표 근거 {coverage.schedule_routes}/{coverage.total_routes}{typeof coverage.passage_routes === "number" ? ` · 통과 이력 ${coverage.passage_routes}/${coverage.total_routes}` : ""}</small>}
      <button className={structural ? "open-candidate structural" : "open-candidate"} type="button" onClick={() => onChooseJourney?.(prepareJourneyForDetail(candidate, context))}>{structural ? "정류장 순서 보기" : "시간표 경로 자세히 보기"} <Icon name="arrow-right" /></button>
    </div>
  </article>;
}

function GraphCoverage({ networkStatus, result }) {
  const sources = Array.isArray(networkStatus?.sources) ? networkStatus.sources : [];
  const stopRows = Number(sources.find((item) => item.dataset_kind === "stops")?.row_count || 0);
  const routeRows = Number(sources.find((item) => item.dataset_kind === "routes")?.row_count || 0);
  const topology = networkStatus?.topology_coverage && typeof networkStatus.topology_coverage === "object" ? networkStatus.topology_coverage : {};
  const topologyTargets = Number(topology.targets || 0);
  const topologyComplete = Number(topology.complete || 0);
  const activeTopology = networkStatus?.active_topology && typeof networkStatus.active_topology === "object" ? networkStatus.active_topology : {};
  const activeRoutes = Number(activeTopology.active_route_sequences || 0);
  const activeStops = Number(activeTopology.unique_graph_stops || 0);
  const activeCities = Number(activeTopology.city_count || 0);
  const graphReady = networkStatus?.graph_ready === true;
  const nationwideComplete = networkStatus?.nationwide_graph_complete === true;
  const graph = result?.graph && typeof result.graph === "object" ? result.graph : null;
  const schedule = normalizeSchedule(result);
  const topologyReady = graph && Number(graph.nodes) > 0 && Number(graph.edges) > 0;
  const scheduleGraph = Boolean(graph && (result?.schedule || String(graph.algorithm || "").includes("time_dependent") || ["expanded_stops", "departures_scanned", "search_complete", "detail_reason"].some((key) => Object.prototype.hasOwnProperty.call(graph, key))));
  const staticAlternativeCount = Array.isArray(result?.static_alternatives) ? result.static_alternatives.length : 0;
  const scheduleSearchState = graph?.search_complete === true ? "검색 완료" : graph?.search_complete === false ? "검색 미완료" : "완료 상태 DATA_GAP";
  const scheduleDetailReason = graph?.detail_reason || result?.schedule?.detail_reason || "";
  const primaryStatus = nationwideComplete ? "전국 경로망 연결됨" : graphReady ? "공식 검증 구간 연결됨" : "전국 경로망 준비 중";
  const catalogSummary = stopRows && routeRows
    ? `정류장 ${formatCount(stopRows)} · 노선 ${formatCount(routeRows)}`
    : "전국 목록 DATA_GAP";
  const topologySummary = activeRoutes
    ? `방향 노선 ${formatCount(activeRoutes)} · 그래프 정류장 ${formatCount(activeStops)}`
    : topologyTargets
    ? `방향 순서 ${formatCount(topologyComplete)}/${formatCount(topologyTargets)}`
    : "방향 순서 DATA_GAP";
  return <div className={`graph-coverage ${graphReady ? "catalog-ready" : "catalog-gap"}`}>
    <span className="graph-pulse" aria-hidden="true" />
    <p><strong>{primaryStatus}</strong><small>{catalogSummary} · {topologySummary}</small></p>
    <span className="graph-method">{scheduleGraph ? "시간의존 Dijkstra" : "단방향 Dijkstra"}</span>
    {graphReady && !nationwideComplete && <small className="coverage-query">공식 경유 순서가 연결된 {formatCount(activeCities)}개 지역부터 실제 방향으로 검색합니다. 전국 확대 중입니다.</small>}
    {!graphReady && <small className="coverage-gap">TAGO 노선별 경유 순서의 전국 적재가 끝나지 않아, 확인된 구간만 검색합니다.</small>}
    {scheduleGraph && <small className={schedule.ready ? "coverage-query" : "coverage-gap"}>이번 일정 검색: {formatCount(graph.expanded_stops)}개 정류장 확장 · {formatCount(graph.departures_scanned)}개 출발편 확인 · {scheduleSearchState} · {graph.algorithm}</small>}
    {scheduleGraph && scheduleDetailReason && <small className="coverage-gap">시간표 상세: {scheduleDetailReason}</small>}
    {staticAlternativeCount > 0 && <small className="coverage-query">방향 구조 후보 {formatCount(staticAlternativeCount)}건 확인 · 시간표 운행 가능성 미확정</small>}
    {graph && !scheduleGraph && <small className={topologyReady ? "coverage-query" : "coverage-gap"}>이번 검색: {formatCount(graph.nodes)}개 상태 · {formatCount(graph.edges)}개 승차 간선 · {graph.algorithm || "directed_dijkstra"}</small>}
    {graph && !scheduleGraph && !topologyReady && staticAlternativeCount === 0 && <small className="coverage-gap">DATA_GAP · 검색 가능한 검증 노선 순서가 없습니다.</small>}
  </div>;
}

function JourneyGenerator({ seededStop, onChooseJourney, connection }) {
  const [fromText, setFromText] = useState(""); const [toText, setToText] = useState("");
  const [fromStop, setFromStop] = useState(null); const [toStop, setToStop] = useState(null);
  const [preference, setPreference] = useState("diverse"); const [result, setResult] = useState(null);
  const [serviceDate, setServiceDate] = useState(() => localDateValue());
  const [departureTime, setDepartureTime] = useState(() => localTimeValue());
  const [loading, setLoading] = useState(false); const [error, setError] = useState("");
  const [networkStatus, setNetworkStatus] = useState(null);
  useEffect(() => { if (seededStop) { if (!fromStop) { setFromStop(seededStop); setFromText(seededStop.node_name); } else { setToStop(seededStop); setToText(seededStop.node_name); } } }, [seededStop]);
  useEffect(() => {
    let active = true;
    BusroApi.networkStatus().then((payload) => active && setNetworkStatus(payload)).catch(() => active && setNetworkStatus({ ready: false, sources: [] }));
    return () => { active = false; };
  }, []);
  async function generate(event) {
    event.preventDefault(); if (!fromStop || !toStop) return;
    setLoading(true); setError(""); setResult(null);
    try { setResult(await BusroApi.generateJourneys({ from_stop_id: fromStop.node_id, to_stop_id: toStop.node_id, from_city_code: fromStop.city_code || undefined, to_city_code: toStop.city_code || undefined, service_date: serviceDate, departure_time: departureTime, preference, max_alternatives: 3 })); }
    catch (reason) { setError(reason.message || "현재 적재된 노선 그래프로 여행을 만들지 못했습니다."); }
    finally { setLoading(false); }
  }
  const schedule = normalizeSchedule(result);
  const candidateRows = result?.candidates || result?.journeys || result?.alternatives || [];
  const returnedCandidates = Array.isArray(candidateRows) ? candidateRows : [];
  const scheduled = schedule.ready ? returnedCandidates.filter((candidate) => candidate?.scheduled !== false) : [];
  const staticRows = Array.isArray(result?.static_alternatives) ? result.static_alternatives : [];
  const structuralCandidates = [...staticRows, ...returnedCandidates.filter((candidate) => !scheduled.includes(candidate) && !staticRows.includes(candidate))];
  const journeyContext = { from_stop: fromStop, to_stop: toStop, preference, service_date: schedule.serviceDate || serviceDate, departure_time: schedule.departureTime || departureTime, schedule: result?.schedule || { status: schedule.status, reason: schedule.reason } };
  const gapReasons = {
    STOP_NOT_IN_HYDRATED_SEQUENCE: "선택한 정류장은 전국 목록에 있지만 검증된 노선 순서 그래프에는 아직 포함되지 않았습니다.",
    NO_DIRECTED_PATH_IN_HYDRATED_GRAPH: "현재 검증 그래프에서 출발 방향부터 도착 방향까지 이어지는 경로가 없습니다. 역방향 간선을 임의로 만들지 않습니다.",
    EVIDENCE_INCOMPLETE: "경로 구조는 찾았지만 시간표 또는 통과 이력이 부족합니다.",
    SCHEDULE_DATA_GAP: "선택한 날짜·출발 시각에 적용할 공식 GTFS 운행 기록이 없습니다. 아래 구조 후보가 있더라도 실제 운행 가능 경로로 확정하지 않습니다.",
  };
  return (
    <section className="journey-generator">
      <GlassCard className="generator-card">
        <div className="generator-heading">
          <div className="generator-kicker"><p className="eyebrow">전국 버스 여행</p><SourceBadge mode={connection.mode} label={connection.label} /></div>
          <h1>어디까지 가세요?</h1>
          <p>출발지와 도착지, 떠날 때를 고르면 실제 진행 방향과 공식 시간표를 함께 확인해요.</p>
        </div>
        <form onSubmit={generate}>
          <div className="route-point-sheet">
            <div className="route-point origin"><span className="point-mark" aria-hidden="true" />
              <StopLookup label="출발" value={fromText} onChange={setFromText} selected={fromStop} onSelect={setFromStop} />
            </div>
            <div className="route-point destination"><span className="point-mark" aria-hidden="true" />
              <StopLookup label="도착" value={toText} onChange={setToText} selected={toStop} onSelect={setToStop} />
            </div>
            <button className="generator-swap" type="button" onClick={() => { setFromStop(toStop); setToStop(fromStop); setFromText(toText); setToText(fromText); }} aria-label="출발과 도착 바꾸기"><Icon name="arrows-down-up" /></button>
          </div>
          <fieldset className="schedule-fieldset"><legend>언제 떠날까요?</legend><div className="schedule-input-grid">
            <label><span><Icon name="calendar-blank" /> 여행 날짜</span><input type="date" value={serviceDate} onChange={(event) => setServiceDate(event.target.value)} required /></label>
            <label><span><Icon name="clock" /> 출발 시각</span><input type="time" value={departureTime} onChange={(event) => setDepartureTime(event.target.value)} step="60" required /></label>
          </div><small>선택한 날짜의 공식 GTFS 운행 기록만 시간 가능 경로로 표시합니다.</small></fieldset>
          <fieldset><legend>어떤 길로 갈까요?</legend><div className="preference-grid">{[["diverse","추천","sparkle"],["low_transfer","최소 환승","arrows-left-right"],["reliable","근거 우선","shield-check"],["challenge","국토종주","flag-banner"]].map(([value,label,icon]) => <button type="button" key={value} className={preference === value ? "active" : ""} onClick={() => setPreference(value)}><Icon name={icon} />{label}</button>)}</div></fieldset>
          <button className="liquid-button route-search-primary" type="submit" disabled={!fromStop || !toStop || !serviceDate || !departureTime || loading}>{loading ? "공식 시간표에서 찾는 중…" : "시간표 경로 찾기"}<Icon name="arrow-right" /></button>
          {(!fromStop || !toStop) && <small className="search-help">정류장명을 입력하고 전국 목록에서 출발·도착을 각각 선택하세요.</small>}
        </form>
      </GlassCard>
      <GraphCoverage networkStatus={networkStatus} result={result} />
      {error && <InlineNotice tone="warning" icon="warning-circle" title="DATA_GAP">{error} 검증된 노선 경유 정류장이 적재되어야 경로에 포함됩니다.</InlineNotice>}
      {result && !schedule.ready && <InlineNotice tone="warning" icon="warning-circle" title="SCHEDULE_DATA_GAP">{gapReasons[schedule.reason] || gapReasons[result.reason] || schedule.reason || "선택한 일정의 공식 시간표 근거가 없습니다."}</InlineNotice>}
      {result && schedule.ready && scheduled.length === 0 && <InlineNotice tone="warning" icon="warning-circle" title={result.status || "SCHEDULE_DATA_GAP"}>{gapReasons[result.reason] || result.reason || "선택한 일정에 출발 가능한 공식 시간표 경로가 없습니다."}</InlineNotice>}
      {scheduled.length > 0 && <div className="generated-journeys scheduled-results">
        <div className="catalog-heading"><div><p className="eyebrow">확인된 시간표 경로</p><h2>{scheduled.length}가지 길을 확인했어요</h2></div><span>{schedule.serviceDate || serviceDate} · {schedule.departureTime || departureTime}</span></div>
        <p className="alternative-hint">출발·도착 시각은 표시된 GTFS 원본 근거를 따릅니다. 성공률은 별도 통과 이력이 있을 때만 계산합니다.</p>
        {scheduled.map((candidate, index) => <JourneyCandidateCard key={`scheduled-${candidate.id || candidate.criterion || "candidate"}-${index}`} candidate={candidate} index={index} schedule={schedule} context={journeyContext} onChooseJourney={onChooseJourney} />)}
      </div>}
      {structuralCandidates.length > 0 && <div className="generated-journeys structural-results">
        <div className="catalog-heading"><div><p className="eyebrow">방향 구조 후보</p><h2>시간표 확인 전 경로</h2></div><span>운행 가능성 미확정</span></div>
        <p className="alternative-hint warning">단방향 정류장 순서만 연결된 결과입니다. 선택한 일정의 실제 버스가 있다고 해석하면 안 됩니다.</p>
        {structuralCandidates.map((candidate, index) => <JourneyCandidateCard key={`structural-${candidate.id || candidate.criterion || "candidate"}-${index}`} candidate={candidate} index={index} schedule={schedule} structural context={journeyContext} onChooseJourney={onChooseJourney} />)}
      </div>}
    </section>
  );
}

function NationwideScreen({ connection, onChooseJourney }) {
  const [seededStop, setSeededStop] = useState(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  return <main className="screen nationwide-screen"><JourneyGenerator seededStop={seededStop} onChooseJourney={onChooseJourney} connection={connection} /><details className="route-admin-tools" open={toolsOpen} onToggle={(event) => setToolsOpen(event.currentTarget.open)}><summary><span><Icon name="wrench" /><strong>노선 데이터 도구</strong><small>운영·검증용</small></span><Icon name="caret-down" /></summary>{toolsOpen && <><div className="route-admin-intro"><p>개별 TAGO 노선 조회·OSM 형상·경유순서 적재는 데이터 점검용입니다. 여행자는 위 전국 경로 검색만 사용하면 됩니다.</p></div><RouteBrowser connection={connection} onUseStop={setSeededStop} /></>}</details></main>;
}
