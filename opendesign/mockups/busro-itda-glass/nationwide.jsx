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
    }).catch((reason) => active && setError(reason.status === 503 ? "TAGO 전국 도시 목록을 쓰려면 서버에 인증키를 연결해야 합니다." : "전국 도시 목록을 불러오지 못했습니다."));
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
      try { await BusroApi.hydrateRoute(cityCode, route.routeId); }
      catch (reason) { setHydrationGap(reason.message || "공식 경유 순서를 여행 그래프에 적재하지 못했습니다."); }
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
        <p className="source-note"><Icon name="database" /> 지역·노선·정류장은 TAGO 공식 식별자로 조회합니다. 서비스 키는 서버에만 있습니다.</p>
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

function summarizeJourneyLegs(candidate) {
  const legs = [];
  for (const step of Array.isArray(candidate?.steps) ? candidate.steps : []) {
    if (step?.kind !== "ride" || !step.route_id) continue;
    const routeId = String(step.route_id);
    const previous = legs[legs.length - 1];
    if (previous && previous.routeId === routeId) {
      previous.to = step.to || previous.to;
      previous.edgeCount += 1;
    } else {
      legs.push({ routeId, from: step.from || {}, to: step.to || {}, edgeCount: 1 });
    }
  }
  return legs;
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
  const topologyReady = graph && Number(graph.nodes) > 0 && Number(graph.edges) > 0;
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
    <span className="graph-method">단방향 Dijkstra</span>
    {graphReady && !nationwideComplete && <small className="coverage-query">공식 경유 순서가 연결된 {formatCount(activeCities)}개 지역부터 실제 방향으로 검색합니다. 전국 확대 중입니다.</small>}
    {!graphReady && <small className="coverage-gap">TAGO 노선별 경유 순서의 전국 적재가 끝나지 않아, 확인된 구간만 검색합니다.</small>}
    {graph && <small className={topologyReady ? "coverage-query" : "coverage-gap"}>이번 검색: {formatCount(graph.nodes)}개 상태 · {formatCount(graph.edges)}개 승차 간선 · {graph.algorithm || "directed_dijkstra"}</small>}
    {graph && !topologyReady && <small className="coverage-gap">DATA_GAP · 검색 가능한 검증 노선 순서가 없습니다.</small>}
  </div>;
}

function JourneyGenerator({ seededStop, onChooseJourney, connection }) {
  const [fromText, setFromText] = useState(""); const [toText, setToText] = useState("");
  const [fromStop, setFromStop] = useState(null); const [toStop, setToStop] = useState(null);
  const [preference, setPreference] = useState("diverse"); const [result, setResult] = useState(null);
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
    try { setResult(await BusroApi.generateJourneys({ from_stop_id: fromStop.node_id, to_stop_id: toStop.node_id, from_city_code: fromStop.city_code || undefined, to_city_code: toStop.city_code || undefined, preference, max_alternatives: 1 })); }
    catch (reason) { setError(reason.message || "현재 적재된 노선 그래프로 여행을 만들지 못했습니다."); }
    finally { setLoading(false); }
  }
  const candidates = result?.alternatives || result?.candidates || result?.journeys || [];
  const criterionLabels = {
    minimum_transfers: "최소 환승",
    generalized_cost: "균형 경로",
    explorer: "탐험 경로",
  };
  const gapReasons = {
    STOP_NOT_IN_HYDRATED_SEQUENCE: "선택한 정류장은 전국 목록에 있지만 검증된 노선 순서 그래프에는 아직 포함되지 않았습니다.",
    NO_DIRECTED_PATH_IN_HYDRATED_GRAPH: "현재 검증 그래프에서 출발 방향부터 도착 방향까지 이어지는 경로가 없습니다. 역방향 간선을 임의로 만들지 않습니다.",
    EVIDENCE_INCOMPLETE: "경로 구조는 찾았지만 시간표 또는 통과 이력이 부족합니다.",
  };
  return (
    <section className="journey-generator">
      <GlassCard className="generator-card">
        <div className="generator-heading">
          <div className="generator-kicker"><p className="eyebrow">전국 버스 여행</p><SourceBadge mode={connection.mode} label={connection.label} /></div>
          <h1>어디까지 가세요?</h1>
          <p>출발지와 도착지만 고르면, 전국 노선의 실제 진행 방향을 따라 길을 찾아드려요.</p>
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
          <fieldset><legend>어떤 길로 갈까요?</legend><div className="preference-grid">{[["diverse","추천","sparkle"],["low_transfer","최소 환승","arrows-left-right"],["reliable","근거 우선","shield-check"],["challenge","국토종주","flag-banner"]].map(([value,label,icon]) => <button type="button" key={value} className={preference === value ? "active" : ""} onClick={() => setPreference(value)}><Icon name={icon} />{label}</button>)}</div></fieldset>
          <button className="liquid-button route-search-primary" type="submit" disabled={!fromStop || !toStop || loading}>{loading ? "전국 노선에서 찾는 중…" : "경로 찾기"}<Icon name="arrow-right" /></button>
          {(!fromStop || !toStop) && <small className="search-help">정류장명을 입력하고 전국 목록에서 출발·도착을 각각 선택하세요.</small>}
        </form>
      </GlassCard>
      <GraphCoverage networkStatus={networkStatus} result={result} />
      {error && <InlineNotice tone="warning" icon="warning-circle" title="DATA_GAP">{error} 검증된 노선 경유 정류장이 적재되어야 경로에 포함됩니다.</InlineNotice>}
      {result && candidates.length === 0 && <InlineNotice tone="warning" icon="warning-circle" title={result.status || "DATA_GAP"}>{gapReasons[result.reason] || result.reason || "생성 가능한 방향성 경로가 적재된 그래프에 없습니다."}</InlineNotice>}
      {candidates.length > 0 && <div className="generated-journeys">
        <div className="catalog-heading"><div><p className="eyebrow">선택 기준 경로</p><h2>{candidates.length}가지 길을 찾았어요</h2></div><span>빠른 1차 검색</span></div>
        <p className="alternative-hint">다른 여행 종류는 위 기준을 바꿔 다시 찾아보세요.</p>
        {candidates.map((candidate, index) => {
          const routeIds = Array.isArray(candidate.route_ids) ? candidate.route_ids.filter(Boolean) : [];
          const legs = summarizeJourneyLegs(candidate);
          const coverage = candidate.coverage && typeof candidate.coverage === "object" ? candidate.coverage : {};
          const evidence = candidate.evidence && typeof candidate.evidence === "object" ? candidate.evidence : {};
          const hasProbability = typeof candidate.success_probability === "number" && Number.isFinite(candidate.success_probability);
          return <article key={`${candidate.criterion || "candidate"}-${routeIds.join("-")}-${index}`}>
            <div className="candidate-rank">{index + 1}</div>
            <div className="candidate-copy">
              <div className="candidate-title"><div><p>{criterionLabels[candidate.criterion] || candidate.criterion || "경로 후보"}</p><h3>{routeIds.length > 0 ? `${candidate.transfers || 0}회 환승 · ${routeIds.length}개 노선` : "노선 DATA_GAP"}</h3></div><small>{candidate.status || "DATA_GAP"}</small></div>
              <div className="candidate-leg-list">{legs.map((leg, legIndex) => <div className="candidate-leg" key={`${leg.routeId}-${legIndex}`}>
                <span className="timeline-rail"><i /><b /></span>
                <div className="leg-copy"><span className="route-pill"><Icon name="bus" /> {leg.routeId}</span><strong>{leg.from?.node_name || leg.from?.node_id || "승차 정류장"}</strong><small>총 {leg.edgeCount + 1}개 정류장</small><strong>{leg.to?.node_name || leg.to?.node_id || "하차 정류장"}</strong></div>
              </div>)}</div>
              <footer>
                <span><Icon name="arrows-left-right" /> {typeof candidate.transfers === "number" ? `${candidate.transfers}회 환승` : "환승 DATA_GAP"}</span>
                <span><Icon name="database" /> 승차 {evidence.ride_edges ?? "—"} · 환승 {evidence.transfer_edges ?? "—"} 간선</span>
                <strong>{hasProbability ? `성공률 ${Math.round(candidate.success_probability * 100)}%` : "성공률 DATA_GAP"}</strong>
              </footer>
              {typeof coverage.schedule_routes === "number" && typeof coverage.total_routes === "number" && <small className="evidence-copy">시간표 근거 {coverage.schedule_routes}/{coverage.total_routes}{typeof coverage.passage_routes === "number" ? ` · 통과 이력 ${coverage.passage_routes}/${coverage.total_routes}` : ""}</small>}
              <button className="open-candidate" type="button" onClick={() => onChooseJourney?.({ ...candidate, from_stop: fromStop, to_stop: toStop, preference })}>경로 자세히 보기 <Icon name="arrow-right" /></button>
            </div>
          </article>;
        })}
      </div>}
    </section>
  );
}

function NationwideScreen({ connection, onChooseJourney }) {
  const [seededStop, setSeededStop] = useState(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  return <main className="screen nationwide-screen"><JourneyGenerator seededStop={seededStop} onChooseJourney={onChooseJourney} connection={connection} /><details className="route-admin-tools" open={toolsOpen} onToggle={(event) => setToolsOpen(event.currentTarget.open)}><summary><span><Icon name="wrench" /><strong>노선 데이터 도구</strong><small>운영·검증용</small></span><Icon name="caret-down" /></summary>{toolsOpen && <><div className="route-admin-intro"><p>개별 TAGO 노선 조회·OSM 형상·경유순서 적재는 데이터 점검용입니다. 여행자는 위 전국 경로 검색만 사용하면 됩니다.</p></div><RouteBrowser connection={connection} onUseStop={setSeededStop} /></>}</details></main>;
}
