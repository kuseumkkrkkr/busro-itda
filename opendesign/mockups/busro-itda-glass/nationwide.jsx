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

  const selectedFirstTime = validRouteClock(routeInfo?.first_vehicle_time);
  const selectedLastTime = validRouteClock(routeInfo?.last_vehicle_time);
  const selectedWeekdayHeadway = validHeadway(routeInfo?.weekday_interval_minutes);

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
        <div className="route-terminal-row"><div><small>기점</small><strong>{routeInfo?.startName || selected.startName}</strong><span>{selectedFirstTime ? `첫차 ${selectedFirstTime}` : "첫차 정보 미확보"}</span></div><Icon name="arrow-right" /><div><small>종점</small><strong>{routeInfo?.endName || selected.endName}</strong><span>{selectedLastTime ? `막차 ${selectedLastTime}` : "막차 정보 미확보"}</span></div></div>
        <div className="route-evidence-row"><span><Icon name="map-pin" /> 경유 {stops.length}개</span><span><Icon name="bus" /> 현재 차량 {positions.length}대</span><span><Icon name="clock" /> 평일 배차 {selectedWeekdayHeadway ?? "—"}{selectedWeekdayHeadway ? "분" : ""}</span></div>
        <p className="route-window-note"><Icon name="info" /> 첫차·막차·배차는 TAGO 노선 운행창입니다. 정류장별 출발시각으로 바꾸어 계산하지 않습니다.</p>
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

function evidenceObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function evidenceText(value) {
  return String(value ?? "").replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, 160);
}

function evidenceBoolean(...values) {
  return values.find((value) => typeof value === "boolean");
}

function isGtfsEvidence({ provider = "", basis = "", feedId = "" } = {}) {
  return /GTFS|KTDB/i.test(`${provider} ${basis} ${feedId}`);
}

function isHistoricalEvidence({ topologyRole = "", basis = "", reason = "", projectionAllowed, provider = "", feedId = "" } = {}) {
  if (projectionAllowed === false) return true;
  if (/HISTORICAL_MODEL/i.test(topologyRole)) return true;
  if (/HISTORICAL|PRIOR_ONLY|VERIFIED_PRIOR_ONLY/i.test(`${basis} ${reason}`)) return true;
  return isGtfsEvidence({ provider, basis, feedId }) && !(projectionAllowed === true && /ACTIVE_TOPOLOGY/i.test(topologyRole));
}

function normalizeSchedule(result) {
  const schedule = result?.schedule && typeof result.schedule === "object" ? result.schedule : {};
  const status = String(schedule.status || result?.schedule_status || "DATA_GAP").toUpperCase();
  const basis = evidenceText(schedule.basis || result?.schedule_basis);
  const provider = evidenceText(schedule.provider || result?.schedule_provider);
  const feedId = evidenceText(schedule.feed_id || result?.schedule_feed_id);
  const topologyRole = evidenceText(schedule.topology_role || result?.topology_role);
  const projectionAllowed = evidenceBoolean(schedule.projection_allowed, result?.projection_allowed);
  const reason = evidenceText(schedule.reason || result?.schedule_reason || result?.reason || "SCHEDULE_DATA_GAP");
  const historical = isHistoricalEvidence({ topologyRole, basis, reason, projectionAllowed, provider, feedId });
  const ready = ["READY", "AVAILABLE", "SCHEDULE_READY", "OK"].includes(status) && !historical;
  return {
    ready,
    status: ready ? "READY" : "DATA_GAP",
    reason,
    serviceDate: schedule.service_date || result?.service_date || "",
    departureTime: schedule.departure_time || result?.departure_time || "",
    basis,
    provider,
    feedId,
    topologyRole,
    projectionAllowed,
    historical,
    limitations: Array.isArray(schedule.limitations) ? schedule.limitations.map(evidenceText).filter(Boolean) : [],
    historicalPrior: evidenceObject(schedule.historical_prior || result?.historical_gtfs_prior || result?.reliability?.historical_prior),
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
  const topologyRole = provenance.topology_role || candidateEvidence.topology_role || resultSchedule.topologyRole || source.topology_role || "";
  const projectionAllowed = evidenceBoolean(provenance.projection_allowed, candidateEvidence.projection_allowed, resultSchedule.projectionAllowed, source.projection_allowed);
  const historical = isHistoricalEvidence({ topologyRole, basis, reason: resultSchedule.reason, projectionAllowed, provider, feedId });
  const official = /OFFICIAL/i.test(String(basis));
  const basisLabel = String(basis || "").replaceAll("_", " ");
  return {
    ready: !historical && Boolean(provider || feedId || basis),
    historical,
    provider,
    feedId,
    projectionAllowed,
    label: [provider ? `${provider}${official && !isGtfsEvidence({ provider, basis, feedId }) ? " 공식" : ""}` : "", feedId, basisLabel].filter(Boolean).join(" · "),
  };
}

const candidateRouteWindowCache = new Map();
const candidateRouteWindowPending = new Map();
const CANDIDATE_ROUTE_WINDOW_CACHE_LIMIT = 96;
const CANDIDATE_ROUTE_WINDOW_CACHE_TTL_MS = 5 * 60 * 1000;
let activeCandidateRouteWindowBase = "";
let candidateRouteWindowBaseEpoch = 0;

function normalizeCandidateRouteWindowBase(value = BusroApi.getBase()) {
  const raw = String(value || "").trim();
  try {
    const parsed = new URL(raw, window.location.href);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return "";
    parsed.search = ""; parsed.hash = "";
    return parsed.href.replace(/\/+$/, "");
  } catch { return ""; }
}

function activateCandidateRouteWindowBase(apiBase) {
  if (activeCandidateRouteWindowBase !== apiBase) {
    activeCandidateRouteWindowBase = apiBase;
    candidateRouteWindowBaseEpoch += 1;
  }
  return candidateRouteWindowBaseEpoch;
}

function candidateRouteWindowKey(apiBase, request) {
  return `${apiBase}|${request.cityCode}|${request.routeId}`;
}

function rememberCandidateRouteWindow(key, value) {
  if (candidateRouteWindowCache.has(key)) candidateRouteWindowCache.delete(key);
  candidateRouteWindowCache.set(key, value);
  while (candidateRouteWindowCache.size > CANDIDATE_ROUTE_WINDOW_CACHE_LIMIT) candidateRouteWindowCache.delete(candidateRouteWindowCache.keys().next().value);
}

function cachedCandidateRouteWindow(key, now = Date.now()) {
  const entry = candidateRouteWindowCache.get(key);
  if (!entry || !Number.isFinite(entry.expiresAt) || entry.expiresAt <= now) {
    candidateRouteWindowCache.delete(key);
    return null;
  }
  return entry;
}

function candidateRouteRequests(candidates) {
  const rows = [];
  for (const candidate of candidates) {
    const routes = Array.isArray(candidate?.routes) ? candidate.routes : [];
    for (const route of routes) {
      const cityCode = evidenceText(route?.city_code || route?.cityCode);
      const routeId = evidenceText(route?.route_id || route?.routeId);
      if (cityCode && routeId) rows.push({ cityCode, routeId });
    }
  }
  return [...new Map(rows.map((row) => [`${row.cityCode}|${row.routeId}`, row])).values()].slice(0, 12);
}

function routeWindowResponseEvidence(payload) {
  const provenance = evidenceObject(payload?.provenance);
  const mode = evidenceText(payload?.mode).toLowerCase();
  const fixtureNotice = evidenceText(payload?.fixture_notice || provenance.fixture_notice);
  const source = evidenceText(payload?.source || provenance.provider);
  const fixture = payload?.fixture === true || provenance.fixture === true || mode === "fixture" || /FIXTURE|SCHEMA_ONLY/i.test(`${source} ${fixtureNotice}`);
  return {
    mode,
    provenance: { ...provenance },
    source,
    ready: mode === "live" && !fixture && !/SCHEMA_ONLY/i.test(fixtureNotice),
  };
}

function fetchCandidateRouteWindow(request, apiBase, epoch) {
  const key = candidateRouteWindowKey(apiBase, request);
  const cached = cachedCandidateRouteWindow(key);
  if (cached) return Promise.resolve(cached);
  const existingPending = candidateRouteWindowPending.get(key);
  if (existingPending?.epoch === epoch) return existingPending.promise;
  const pending = BusroApi.routeInfo(request.cityCode, request.routeId).then((payload) => {
    const route = normalizeRoute(payload.route || payload.item || {});
    const evidence = routeWindowResponseEvidence(payload);
    const fetchedAt = Date.now();
    const entry = {
      apiBase,
      route: { ...route, city_code: request.cityCode, route_id: request.routeId },
      ...evidence,
      fetchedAt,
      expiresAt: fetchedAt + CANDIDATE_ROUTE_WINDOW_CACHE_TTL_MS,
    };
    if (epoch !== candidateRouteWindowBaseEpoch || apiBase !== activeCandidateRouteWindowBase) return null;
    rememberCandidateRouteWindow(key, entry);
    return entry;
  }).catch(() => null).finally(() => {
    if (candidateRouteWindowPending.get(key)?.promise === pending) candidateRouteWindowPending.delete(key);
  });
  candidateRouteWindowPending.set(key, { epoch, promise: pending });
  return pending;
}

function useCandidateRouteWindows(candidates) {
  const requests = candidateRouteRequests(candidates);
  const apiBase = normalizeCandidateRouteWindowBase();
  const epoch = activateCandidateRouteWindowBase(apiBase);
  const signature = `${apiBase}::${requests.map((row) => `${row.cityCode}|${row.routeId}`).join(",")}`;
  const [windows, setWindows] = useState(() => new Map());
  const [expiryTick, setExpiryTick] = useState(0);
  useEffect(() => {
    let active = true;
    let refreshTimer = null;
    const queue = [...requests];
    async function worker() {
      while (queue.length > 0) await fetchCandidateRouteWindow(queue.shift(), apiBase, epoch);
    }
    Promise.all(Array.from({ length: Math.min(3, queue.length) }, worker)).then(() => {
      if (!active || epoch !== candidateRouteWindowBaseEpoch || apiBase !== activeCandidateRouteWindowBase) return;
      const next = new Map();
      let nextExpiry = Number.POSITIVE_INFINITY;
      for (const request of requests) {
        const entry = cachedCandidateRouteWindow(candidateRouteWindowKey(apiBase, request));
        if (!entry) continue;
        next.set(`${request.cityCode}|${request.routeId}`, entry);
        nextExpiry = Math.min(nextExpiry, entry.expiresAt);
      }
      setWindows(next);
      if (Number.isFinite(nextExpiry)) refreshTimer = window.setTimeout(() => active && setExpiryTick((value) => value + 1), Math.max(1000, nextExpiry - Date.now() + 25));
    });
    return () => { active = false; if (refreshTimer !== null) window.clearTimeout(refreshTimer); };
  }, [signature, expiryTick]);
  return windows;
}

function validRouteClock(value) {
  const text = evidenceText(value);
  if (/^(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$/.test(text)) return text;
  if (/^\d{3,4}$/.test(text)) {
    const padded = text.padStart(4, "0");
    const hour = Number(padded.slice(0, 2));
    const minute = Number(padded.slice(2));
    if (hour <= 23 && minute <= 59) return `${padded.slice(0, 2)}:${padded.slice(2)}`;
  }
  return "";
}

function validHeadway(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 && number <= 1440 ? Math.round(number) : null;
}

function routeWindowRowAllowed(row) {
  const value = evidenceObject(row);
  const provenance = evidenceObject(value.provenance);
  const mode = evidenceText(value.mode).toLowerCase();
  const marker = `${evidenceText(value.fixture_notice)} ${evidenceText(value.source)} ${evidenceText(provenance.fixture_notice)} ${evidenceText(provenance.provider)}`;
  if (value.fixture === true || provenance.fixture === true || mode === "fixture" || /FIXTURE|SCHEMA_ONLY/i.test(marker)) return false;
  return !mode || mode === "live";
}

function routeWindowRows(candidate, context = {}, fetchedWindows = new Map()) {
  const current = evidenceObject(candidate?.current_timetable || candidate?.current_static_timetable || context?.current_timetable || context?.current_static_timetable);
  const explicit = (Array.isArray(candidate?.route_windows) ? candidate.route_windows : Array.isArray(current.route_windows) ? current.route_windows : []).filter(routeWindowRowAllowed);
  const routes = Array.isArray(candidate?.routes) ? candidate.routes : [];
  const fetched = routes.map((route) => fetchedWindows.get(`${evidenceText(route?.city_code || route?.cityCode)}|${evidenceText(route?.route_id || route?.routeId)}`)).filter((entry) => entry?.ready === true).map((entry) => entry.route);
  return [...explicit, ...fetched, ...routes].map((route, index) => {
    const row = evidenceObject(route);
    const first = validRouteClock(row.first_vehicle_time || row.first_time || row.first_departure);
    const last = validRouteClock(row.last_vehicle_time || row.last_time || row.last_departure);
    const weekday = validHeadway(row.weekday_interval_minutes || row.weekday_headway_minutes || row.headway_minutes || row.interval_minutes);
    const saturday = validHeadway(row.saturday_interval_minutes || row.saturday_headway_minutes);
    const sunday = validHeadway(row.sunday_interval_minutes || row.sunday_headway_minutes);
    return {
      key: `${evidenceText(row.city_code || row.cityCode)}|${evidenceText(row.route_id || row.routeId || row.route_no || row.routeNo || index)}`,
      route: evidenceText(row.route_no || row.routeNo || row.route_id || row.routeId || `노선 ${index + 1}`),
      first,
      last,
      weekday,
      saturday,
      sunday,
    };
  }).filter((row, index, rows) => (row.first || row.last || row.weekday || row.saturday || row.sunday) && rows.findIndex((item) => item.key === row.key) === index).slice(0, 3);
}

function currentTimetableEvidence(candidate, context, schedule, timeSummary, fetchedWindows) {
  const raw = evidenceObject(candidate?.current_timetable || candidate?.current_static_timetable || context?.current_timetable || context?.current_static_timetable);
  const provider = evidenceText(raw.provider || raw.source_name || raw.municipality || (schedule.ready ? schedule.provider : ""));
  const granularity = evidenceText(raw.schedule_granularity || raw.granularity || raw.tier);
  const date = evidenceText(raw.effective_date || raw.valid_from || raw.published_at || raw.source_date);
  const windows = routeWindowRows(candidate, context, fetchedWindows);
  const granularityLabel = ({ EXACT_STOP_TIMES: "정류장별 시각", TRIP_ORIGIN_ONLY: "기점 출발표", ROUTE_WINDOW: "첫차·막차·배차" })[granularity] || granularity.replaceAll("_", " ");
  if (schedule.ready && timeSummary.length > 0) return {
    ready: true,
    label: [provider || "현재 공식 시간표", granularityLabel, date].filter(Boolean).join(" · "),
    detail: timeSummary.join(" · "),
    windows,
  };
  if (windows.length > 0 || provider || granularity) return {
    ready: true,
    label: [provider || "TAGO", granularityLabel || "ROUTE_WINDOW", date].filter(Boolean).join(" · "),
    detail: windows.length > 0 ? "실제 수신된 첫차·막차·배차 범위" : "현재 출처의 제공 범위만 표시",
    windows,
  };
  return { ready: false, label: "정류장별 출발시각 미확보", detail: "TAGO 경로는 계속 표시 · 임의 시각 생성 안 함", windows: [] };
}

function historicalPriorEvidence(candidate, context, schedule, provenance) {
  const reliability = evidenceObject(candidate?.reliability || context?.reliability);
  const raw = evidenceObject(candidate?.historical_prior || reliability.historical_gtfs_prior || reliability.historical_prior || context?.historical_prior || schedule.historicalPrior);
  const evidence = evidenceObject(raw.evidence);
  const feedId = evidenceText(raw.feed_id || raw.source_id || raw.dataset_id || raw.version_id || raw.evidence_id || evidence.feed_id || evidence.source_id || evidence.dataset_id || evidence.version_id || evidence.evidence_id);
  const hash = evidenceText(raw.file_sha256 || raw.sha256 || raw.source_hash || evidence.file_sha256 || evidence.sha256 || evidence.source_hash);
  const explicitEvidence = Boolean(feedId || hash);
  const matched = raw.matched_to_current_route === true;
  const present = matched && raw.projection_allowed === false && explicitEvidence;
  const provider = evidenceText(raw.provider || raw.source || evidence.provider || "GTFS");
  const hasRawPrior = Object.keys(raw).length > 0;
  return {
    present,
    label: present ? [provider, feedId || (hash ? `SHA-256 ${hash.slice(0, 12)}…` : "")].filter(Boolean).join(" · ") : matched ? "GTFS 모델 근거 식별자 없음" : hasRawPrior ? "현재 노선과 GTFS 모델 근거 미매칭" : "GTFS 과거 모델 근거 미연결",
    detail: present ? "현재 노선에 매칭된 모델 가중치 전용 · 현재 시간표 투영 안 함 · 단독 성공률 미산출" : "현재 노선과 명시적으로 매칭된 근거가 생기기 전에는 가중치로 사용하지 않음",
  };
}

function verifiedSuccessProbability(candidate) {
  const value = candidate?.success_probability;
  const basis = evidenceText(candidate?.probability_basis || candidate?.reliability?.probability_basis);
  const scope = evidenceText(candidate?.probability_scope || candidate?.reliability?.probability_scope);
  if (typeof value !== "number" || !Number.isFinite(value) || !basis || !scope) return null;
  if (/GTFS|HISTORICAL|PRIOR|RECONSTRUCTION|PASSAGE_OUTCOME_RATIO/i.test(`${basis} ${scope}`)) return null;
  return Math.max(0, Math.min(1, value));
}

function JourneyEvidenceStack({ candidate, context = {}, schedule, provenance, timeSummary, connection, fetchedWindows = new Map() }) {
  const timetable = currentTimetableEvidence(candidate, context, schedule, timeSummary, fetchedWindows);
  const prior = historicalPriorEvidence(candidate, context, schedule, provenance);
  const reliability = evidenceObject(candidate?.reliability || context?.reliability);
  const trust = evidenceObject(reliability.trust_assumption);
  const live = evidenceObject(candidate?.realtime_adjustment || candidate?.live_adjustment || context?.realtime_adjustment);
  const liveReady = /READY|LIVE|APPLIED/i.test(evidenceText(live.status || live.state));
  const liveDetail = liveReady
    ? evidenceText(live.summary || live.detail || "TAGO 도착·차량 위치 보정 반영")
    : connection?.mode === "live" || connection?.mode === "ready"
      ? "TAGO 연결됨 · 선택 후 정류장별 도착정보로 보정"
      : "선택 후 TAGO 도착정보로 보정 · 현재값 없음";
  return <div className="journey-evidence-stack" aria-label="경로 데이터 근거">
    <div className="evidence-tier current"><span><Icon name="path" /></span><div><small>1 · 현재 경로</small><strong>TAGO 경유 순서</strong><p>현재 공식 식별자로 연결한 단방향 경로</p></div></div>
    <div className={`evidence-tier ${timetable.ready ? "current" : "gap"}`}><span><Icon name="clock" /></span><div><small>2 · 현재 정적 시간표</small><strong>{timetable.label}</strong><p>{timetable.detail}</p>{timetable.windows.map((row) => <em key={row.key}>{row.route} · {[row.first ? `첫차 ${row.first}` : "", row.last ? `막차 ${row.last}` : "", row.weekday ? `평일 ${row.weekday}분` : "", row.saturday ? `토 ${row.saturday}분` : "", row.sunday ? `일 ${row.sunday}분` : ""].filter(Boolean).join(" · ")}</em>)}</div></div>
    <div className={`evidence-tier ${liveReady ? "live" : "pending"}`}><span><Icon name="broadcast" /></span><div><small>3 · 실시간 보정</small><strong>TAGO 도착·차량 위치</strong><p>{liveDetail}</p></div></div>
    <div className={`evidence-tier ${prior.present ? "prior" : "pending"}`}><span><Icon name="flask" /></span><div><small>4 · 과거 모델 근거</small><strong>{prior.label}</strong><p>{prior.detail}</p></div></div>
    {trust.code === "USUALLY_ON_TIME" && <small className="trust-assumption"><Icon name="shield-check" /> 운영 가정: 대체로 정시 · 실측 확률 아님</small>}
  </div>;
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
  const currentSchedule = normalizeSchedule({ schedule: context?.schedule || {} });
  const replayLegs = (Array.isArray(candidate?.replay_legs) ? candidate.replay_legs : []).map((row) => {
    const sourceId = typeof row.time_evidence_source === "object" ? String(row.time_evidence_source.source_id || "") : String(row.time_evidence_source || "");
    const scheduledMinutes = gtfsClockMinutes(row.scheduled_arrival) ?? (row.scheduled_minutes !== null && row.scheduled_minutes !== undefined && Number.isFinite(Number(row.scheduled_minutes)) ? Number(row.scheduled_minutes) : null);
    const nextDepartureMinutes = gtfsClockMinutes(row.next_departure) ?? (row.next_departure_minutes !== null && row.next_departure_minutes !== undefined && Number.isFinite(Number(row.next_departure_minutes)) ? Number(row.next_departure_minutes) : null);
    return {
      ...row,
      scheduled_source_time: String(row.scheduled_arrival || ""),
      next_departure_source_time: String(row.next_departure || ""),
      scheduled_gtfs_time: String(row.scheduled_arrival || ""),
      next_departure_gtfs_time: String(row.next_departure || ""),
      scheduled_minutes: scheduledMinutes,
      next_departure_minutes: nextDepartureMinutes,
      scheduled_day_offset: scheduledMinutes === null ? null : Math.floor(scheduledMinutes / 1440),
      next_departure_day_offset: nextDepartureMinutes === null ? null : Math.floor(nextDepartureMinutes / 1440),
      scheduled_arrival: replayClock(row.scheduled_arrival, scheduledMinutes),
      next_departure: replayClock(row.next_departure, nextDepartureMinutes),
      time_evidence_source: sourceId,
      time_evidence_verified: currentSchedule.ready && (row.time_evidence_verified === true || Boolean(row.time_evidence_trip_id && sourceId)),
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

function JourneyCandidateCard({ candidate, index, schedule, structural = false, context, connection, fetchedWindows, onChooseJourney }) {
  const routeIds = Array.isArray(candidate?.route_ids) ? candidate.route_ids.filter(Boolean) : (Array.isArray(candidate?.routes) ? candidate.routes.map((item) => item?.route_id || item?.routeId).filter(Boolean) : []);
  const legs = summarizeJourneyLegs(candidate);
  const coverage = candidate?.coverage && typeof candidate.coverage === "object" ? candidate.coverage : {};
  const evidence = candidate?.evidence && typeof candidate.evidence === "object" ? candidate.evidence : {};
  const provenance = scheduleEvidence(schedule, candidate);
  const successProbability = verifiedSuccessProbability(candidate);
  const departureTime = formatGtfsClock(candidate?.departure_time, candidate?.departure_seconds);
  const arrivalTime = formatGtfsClock(candidate?.arrival_time, candidate?.arrival_seconds);
  const minutes = Number(candidate?.estimated_minutes);
  const timeSummary = [departureTime ? `출발 ${departureTime}` : "", arrivalTime ? `도착 ${arrivalTime}` : "", Number.isFinite(minutes) ? `${Math.max(0, Math.round(minutes))}분` : ""].filter(Boolean);
  return <article className={structural ? "structural-candidate" : "scheduled-candidate"}>
    <div className="candidate-rank">{index + 1}</div>
    <div className="candidate-copy">
      <div className="candidate-title"><div><p>{JOURNEY_CRITERION_LABELS[candidate?.criterion] || candidate?.criterion || (structural ? "현재 TAGO 경로" : "현재 시간표 경로")}</p><h3>{routeIds.length > 0 ? `${candidate?.transfers || 0}회 환승 · ${routeIds.length}개 노선` : "노선 DATA_GAP"}</h3></div><small className={structural ? "topology-ready" : "schedule-ready"}>{structural ? "TAGO 경로" : "현재 시각 확인"}</small></div>
      {!structural && timeSummary.length > 0 && <div className="schedule-summary"><Icon name="clock" /><strong>{timeSummary.join(" · ")}</strong></div>}
      {structural && <div className="topology-assumption-copy"><Icon name="path" /><span>현재 TAGO 정류장 진행 방향으로 연결했습니다. 정류장별 출발시각이 없어도 경로는 유지합니다.</span></div>}
      <div className="candidate-leg-list">{legs.map((leg, legIndex) => <div className="candidate-leg" key={`${leg.routeId}-${leg.tripId}-${legIndex}`}>
        <span className="timeline-rail"><i /><b /></span>
        <div className="leg-copy"><span className="route-pill"><Icon name="bus" /> {leg.routeId}</span><strong>{leg.from?.node_name || leg.from?.node_id || "승차 정류장"}</strong>{!structural && leg.departureTime && <span className="leg-time"><Icon name="clock" /> {leg.departureTime} 출발</span>}<small>총 {leg.edgeCount + 1}개 정류장</small><strong>{leg.to?.node_name || leg.to?.node_id || "하차 정류장"}</strong>{!structural && leg.arrivalTime && <span className="leg-time arrival"><Icon name="clock" /> {leg.arrivalTime} 도착</span>}{!structural && leg.nextDepartureTime && <span className="transfer-time">다음 버스 {leg.nextDepartureTime} 출발</span>}</div>
      </div>)}</div>
      <footer>
        <span><Icon name="arrows-left-right" /> {typeof candidate?.transfers === "number" ? `${candidate.transfers}회 환승` : "환승 DATA_GAP"}</span>
        <span><Icon name="database" /> 승차 {evidence.ride_edges ?? "—"} · 환승 {evidence.transfer_edges ?? "—"} 간선</span>
        <strong>{successProbability === null ? "성공률 미산출" : `관측 성공률 ${Math.round(successProbability * 100)}%`}</strong>
      </footer>
      {!structural && <small className={provenance.ready ? "official-schedule-evidence" : "schedule-evidence-gap"}><Icon name={provenance.ready ? "shield-check" : "warning-circle"} /> {provenance.ready ? provenance.label : "현재 시간표 출처 DATA_GAP"}</small>}
      {typeof coverage.schedule_routes === "number" && typeof coverage.total_routes === "number" && <small className="evidence-copy">현재 시간표 근거 {coverage.schedule_routes}/{coverage.total_routes}{typeof coverage.passage_routes === "number" ? ` · 실제 통과 이력 ${coverage.passage_routes}/${coverage.total_routes}` : ""}</small>}
      <JourneyEvidenceStack candidate={candidate} context={context} schedule={schedule} provenance={provenance} timeSummary={timeSummary} connection={connection} fetchedWindows={fetchedWindows} />
      <button className={structural ? "open-candidate structural" : "open-candidate"} type="button" onClick={() => onChooseJourney?.(prepareJourneyForDetail(candidate, context))}>경로·근거 자세히 보기 <Icon name="arrow-right" /></button>
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
  const scheduleGraph = Boolean(schedule.ready && graph && (String(graph.algorithm || "").includes("time_dependent") || ["expanded_stops", "departures_scanned", "search_complete"].some((key) => Object.prototype.hasOwnProperty.call(graph, key))));
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
    <span className="graph-method">{scheduleGraph ? "현재 시간표 Dijkstra" : "TAGO 방향 Dijkstra"}</span>
    {graphReady && !nationwideComplete && <small className="coverage-query">공식 경유 순서가 연결된 {formatCount(activeCities)}개 지역부터 실제 방향으로 검색합니다. 전국 확대 중입니다.</small>}
    {!graphReady && <small className="coverage-gap">TAGO 노선별 경유 순서의 전국 적재가 끝나지 않아, 확인된 구간만 검색합니다.</small>}
    {scheduleGraph && <small className={schedule.ready ? "coverage-query" : "coverage-gap"}>이번 일정 검색: {formatCount(graph.expanded_stops)}개 정류장 확장 · {formatCount(graph.departures_scanned)}개 출발편 확인 · {scheduleSearchState} · {graph.algorithm}</small>}
    {scheduleGraph && scheduleDetailReason && <small className="coverage-gap">시간표 상세: {scheduleDetailReason}</small>}
    {staticAlternativeCount > 0 && <small className="coverage-query">현재 TAGO 방향 경로 {formatCount(staticAlternativeCount)}건 확인 · 정류장별 시각이 없어도 우선 표시</small>}
    {schedule.historical && <small className="coverage-prior">과거 GTFS는 모델 가중치 전용 · 현재 날짜 시간표로 투영하지 않음</small>}
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
  const structuralPool = [...staticRows, ...returnedCandidates.filter((candidate) => candidate?.scheduled !== true && !scheduled.includes(candidate))];
  const structuralCandidates = structuralPool.filter((candidate, index, rows) => rows.findIndex((item) => (item?.id && item.id === candidate?.id) || (!item?.id && JSON.stringify(item?.route_ids || []) === JSON.stringify(candidate?.route_ids || []) && item?.criterion === candidate?.criterion)) === index);
  const fetchedWindows = useCandidateRouteWindows([...structuralCandidates, ...scheduled]);
  const journeyContext = {
    from_stop: fromStop,
    to_stop: toStop,
    preference,
    service_date: schedule.serviceDate || serviceDate,
    departure_time: schedule.departureTime || departureTime,
    schedule: result?.schedule || { status: schedule.status, reason: schedule.reason },
    current_timetable: result?.current_timetable || result?.current_static_timetable || null,
    reliability: result?.reliability || null,
    realtime_adjustment: result?.realtime_adjustment || result?.live_adjustment || null,
    historical_prior: result?.historical_gtfs_prior || result?.reliability?.historical_prior || null,
  };
  const gapReasons = {
    STOP_NOT_IN_HYDRATED_SEQUENCE: "선택한 정류장은 전국 목록에 있지만 검증된 노선 순서 그래프에는 아직 포함되지 않았습니다.",
    NO_DIRECTED_PATH_IN_HYDRATED_GRAPH: "현재 검증 그래프에서 출발 방향부터 도착 방향까지 이어지는 경로가 없습니다. 역방향 간선을 임의로 만들지 않습니다.",
    EVIDENCE_INCOMPLETE: "현재 TAGO 경로는 찾았지만 정류장별 시간표 또는 실제 통과 이력이 부족합니다.",
    SCHEDULE_DATA_GAP: "정류장별 현재 출발시각은 확보되지 않았습니다. TAGO 방향 경로와 확보된 노선 운행창은 계속 표시합니다.",
    HISTORICAL_GTFS_PRIOR_ONLY: "과거 GTFS는 신뢰도 모델 근거로만 사용하며 오늘 시간표로 투영하지 않습니다.",
  };
  return (
    <section className="journey-generator">
      <GlassCard className="generator-card">
        <div className="generator-heading">
          <div className="generator-kicker"><p className="eyebrow">전국 버스 여행</p><SourceBadge mode={connection.mode} label={connection.label} /></div>
          <h1>어디까지 가세요?</h1>
          <p>현재 TAGO 진행 방향을 먼저 찾고, 확보된 최신 정적 시간표와 실시간 도착정보를 단계별로 확인해요.</p>
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
          </div><small>날짜·시각은 현재 공식 정적 시간표와 TAGO 실시간 보정에 사용합니다. 과거 GTFS 시각은 오늘 시간표로 쓰지 않습니다.</small></fieldset>
          <fieldset><legend>어떤 길로 갈까요?</legend><div className="preference-grid">{[["diverse","추천","sparkle"],["low_transfer","최소 환승","arrows-left-right"],["reliable","근거 우선","shield-check"],["challenge","국토종주","flag-banner"]].map(([value,label,icon]) => <button type="button" key={value} className={preference === value ? "active" : ""} onClick={() => setPreference(value)}><Icon name={icon} />{label}</button>)}</div></fieldset>
          <button className="liquid-button route-search-primary" type="submit" disabled={!fromStop || !toStop || !serviceDate || !departureTime || loading}>{loading ? "현재 TAGO 경로 찾는 중…" : "현재 버스 경로 찾기"}<Icon name="arrow-right" /></button>
          {(!fromStop || !toStop) && <small className="search-help">정류장명을 입력하고 전국 목록에서 출발·도착을 각각 선택하세요.</small>}
        </form>
      </GlassCard>
      <GraphCoverage networkStatus={networkStatus} result={result} />
      {error && <InlineNotice tone="warning" icon="warning-circle" title="DATA_GAP">{error} 검증된 노선 경유 정류장이 적재되어야 경로에 포함됩니다.</InlineNotice>}
      {structuralCandidates.length > 0 && <div className="generated-journeys structural-results">
        <div className="catalog-heading"><div><p className="eyebrow">현재 TAGO 경로</p><h2>{structuralCandidates.length}가지 길을 찾았어요</h2></div><span>경로 우선</span></div>
        <p className="alternative-hint">현재 공식 정류장 순서로 연결했습니다. 정류장별 출발시각이 없으면 시간은 비워 두고 경로는 숨기지 않습니다.</p>
        {structuralCandidates.map((candidate, index) => <JourneyCandidateCard key={`structural-${candidate.id || candidate.criterion || "candidate"}-${index}`} candidate={candidate} index={index} schedule={schedule} structural context={journeyContext} connection={connection} fetchedWindows={fetchedWindows} onChooseJourney={onChooseJourney} />)}
      </div>}
      {scheduled.length > 0 && <div className="generated-journeys scheduled-results">
        <div className="catalog-heading"><div><p className="eyebrow">현재 시간표 확인 경로</p><h2>출발시각까지 확인했어요</h2></div><span>{schedule.serviceDate || serviceDate} · {schedule.departureTime || departureTime}</span></div>
        <p className="alternative-hint">표시된 현재 공식 시간표 범위만 사용합니다. 과거 GTFS 시각이나 임의 성공률은 섞지 않습니다.</p>
        {scheduled.map((candidate, index) => <JourneyCandidateCard key={`scheduled-${candidate.id || candidate.criterion || "candidate"}-${index}`} candidate={candidate} index={index} schedule={schedule} context={journeyContext} connection={connection} fetchedWindows={fetchedWindows} onChooseJourney={onChooseJourney} />)}
      </div>}
      {result && !schedule.ready && <InlineNotice tone="neutral" icon={schedule.historical ? "flask" : "clock"} title={schedule.historical ? "GTFS · 모델 근거 전용" : "현재 시간표 범위"}>{gapReasons[schedule.reason] || gapReasons[result.reason] || "정류장별 현재 출발시각은 아직 없습니다. 현재 TAGO 경로는 위에 계속 표시합니다."}</InlineNotice>}
      {result && schedule.ready && scheduled.length === 0 && <InlineNotice tone="warning" icon="clock" title={result.status || "CURRENT_TIMETABLE_DATA_GAP"}>{gapReasons[result.reason] || "현재 시간표에서 해당 시각 이후 출발편을 확인하지 못했습니다. TAGO 방향 경로는 별도로 유지합니다."}</InlineNotice>}
    </section>
  );
}

function NationwideScreen({ connection, onChooseJourney }) {
  const [seededStop, setSeededStop] = useState(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  return <main className="screen nationwide-screen"><JourneyGenerator seededStop={seededStop} onChooseJourney={onChooseJourney} connection={connection} /><details className="route-admin-tools" open={toolsOpen} onToggle={(event) => setToolsOpen(event.currentTarget.open)}><summary><span><Icon name="wrench" /><strong>노선 데이터 도구</strong><small>운영·검증용</small></span><Icon name="caret-down" /></summary>{toolsOpen && <><div className="route-admin-intro"><p>개별 TAGO 노선 조회·OSM 형상·경유순서 적재는 데이터 점검용입니다. 여행자는 위 전국 경로 검색만 사용하면 됩니다.</p></div><RouteBrowser connection={connection} onUseStop={setSeededStop} /></>}</details></main>;
}
