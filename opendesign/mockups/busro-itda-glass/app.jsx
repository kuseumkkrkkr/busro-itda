const APP_STORAGE_KEY = "busro-itda-glass-v1";
const MAPPING_STORAGE_KEY = "busro-itda-official-identifiers-v1";

const EMPTY_PASSAGE_COVERAGE = {
  supported: false,
  count: 0,
  eligibleDays: 0,
  gapCount: 0,
  dataGap: true,
  code: "JOURNEY_REQUIRED",
};

function cleanIdentifier(value) {
  return String(value || "").replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, 128);
}

function isClockTime(value) {
  return /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(String(value || ""));
}

function journeyUsesCurrentTimetable(journey) {
  const schedule = journey?.schedule && typeof journey.schedule === "object" ? journey.schedule : {};
  const topologyRole = String(schedule.topology_role || "");
  const basis = String(schedule.basis || "");
  const reason = String(schedule.reason || "");
  const provider = String(schedule.provider || "");
  const feedId = String(schedule.feed_id || "");
  if (schedule.projection_allowed === false || /HISTORICAL_MODEL/i.test(topologyRole) || /HISTORICAL|PRIOR_ONLY|VERIFIED_PRIOR_ONLY/i.test(`${basis} ${reason}`)) return false;
  const gtfsLike = /GTFS|KTDB/i.test(`${provider} ${basis} ${feedId}`);
  if (gtfsLike && !(schedule.projection_allowed === true && /ACTIVE_TOPOLOGY/i.test(topologyRole))) return false;
  return journey?.scheduled === true;
}

function buildDataGapSimulation(days, code = "JOURNEY_REQUIRED", reason = "전국 여행 후보를 먼저 선택하세요.") {
  const end = new Date();
  const perDay = Array.from({ length: Math.max(1, Number(days) || 7) }, (_, index) => {
    const date = new Date(end);
    date.setDate(end.getDate() - (Math.max(1, Number(days) || 7) - index - 1));
    return { date: date.toISOString().slice(0, 10), probability: null, success: null, status: "gap", reasons: [code] };
  });
  return {
    mode: "offline",
    perDay,
    summary: { probability: null, successfulDays: 0, totalDays: perDay.length, weakestLeg: reason, coverage: 0, dataGap: true, code },
  };
}

function replayValue(step, key) {
  return step?.replay?.[key] ?? step?.timetable?.[key] ?? step?.time_evidence?.[key] ?? step?.[key];
}

function deriveJourneyLegs(journey, { replayOnly = false } = {}) {
  const steps = Array.isArray(journey?.steps) ? journey.steps : [];
  const currentTimetableAllowed = journeyUsesCurrentTimetable(journey);
  const groups = [];
  let current = null;
  steps.forEach((step) => {
    if (step?.kind !== "ride" || !step.route_id || !step.from?.node_id || !step.to?.node_id) {
      current = null;
      return;
    }
    const cityCode = String(step.from.city_code || step.to.city_code || "");
    const continues = current
      && current.routeId === String(step.route_id)
      && current.cityCode === cityCode
      && current.to.node_id === step.from.node_id
      && Number(current.to.node_order) === Number(step.from.node_order);
    if (!continues) {
      current = { routeId: String(step.route_id), cityCode, from: step.from, to: step.to, rides: [step] };
      groups.push(current);
    } else {
      current.to = step.to;
      current.rides.push(step);
    }
  });

  const hasReplayContract = Object.prototype.hasOwnProperty.call(journey || {}, "replay_legs");
  const replayRows = Array.isArray(journey?.replay_legs) ? journey.replay_legs : [];
  const selections = replayOnly && hasReplayContract ? replayRows.map((replayRow) => {
    const groupIndex = groups.findIndex((group) => {
      const lastRide = group.rides[group.rides.length - 1];
      return group.routeId === String(replayRow?.route_id || "")
        && String(group.to?.node_id || "") === String(replayRow?.node_id || "")
        && Number(group.to?.node_order) === Number(replayRow?.node_order)
        && String(lastRide?.trip_id || "") === String(replayRow?.time_evidence_trip_id || "");
    });
    if (groupIndex < 0) return null;
    const nextGroup = groups[groupIndex + 1];
    if (!nextGroup
      || nextGroup.routeId !== String(replayRow?.next_route_id || "")
      || String(nextGroup.from?.node_id || "") !== String(replayRow?.next_node_id || "")
      || Number(nextGroup.from?.node_order) !== Number(replayRow?.next_node_order)
      || String(nextGroup.rides[0]?.trip_id || "") !== String(replayRow?.next_time_evidence_trip_id || "")) return null;
    return { group: groups[groupIndex], replayRow };
  }).filter(Boolean) : replayOnly ? [] : groups.map((group) => ({ group, replayRow: {} }));

  return selections.map(({ group, replayRow }, index) => {
    const lastRide = group.rides[group.rides.length - 1];
    const scheduledArrival = replayRow.scheduled_arrival ?? replayValue(lastRide, "scheduled_arrival");
    const nextDeparture = replayRow.next_departure ?? replayValue(lastRide, "next_departure");
    const minimumTransfer = replayRow.minimum_transfer_minutes ?? replayValue(lastRide, "minimum_transfer_minutes");
    const evidenceBlock = replayRow.time_evidence || lastRide?.time_evidence || lastRide?.timetable || lastRide?.replay || {};
    const timeEvidenceSource = cleanIdentifier(replayRow.time_evidence_source || evidenceBlock.source || "");
    const timeEvidenceVerified = currentTimetableAllowed && (replayRow.time_evidence_verified === true || evidenceBlock.verified === true);
    const timeEvidenceTripId = cleanIdentifier(replayRow.time_evidence_trip_id || evidenceBlock.trip_id || "");
    const timeEvidenceFeedId = cleanIdentifier(replayRow.time_evidence_feed_id || evidenceBlock.feed_id || "");
    const nextRouteId = cleanIdentifier(replayRow.next_route_id || evidenceBlock.next_route_id || "");
    const nextNodeId = cleanIdentifier(replayRow.next_node_id || evidenceBlock.next_node_id || "");
    const nextNodeOrderValue = replayRow.next_node_order ?? evidenceBlock.next_node_order;
    const nextNodeOrder = Number.isInteger(Number(nextNodeOrderValue)) ? Number(nextNodeOrderValue) : null;
    const nextTimeEvidenceTripId = cleanIdentifier(replayRow.next_time_evidence_trip_id || evidenceBlock.next_trip_id || "");
    const nextTimeEvidenceFeedId = cleanIdentifier(replayRow.next_time_evidence_feed_id || evidenceBlock.next_feed_id || "");
    const parsedMinimumTransfer = minimumTransfer !== null && minimumTransfer !== undefined && String(minimumTransfer).trim() !== "" && Number.isInteger(Number(minimumTransfer)) ? Number(minimumTransfer) : null;
    let buffer = null;
    if (timeEvidenceVerified && timeEvidenceSource && isClockTime(scheduledArrival) && isClockTime(nextDeparture) && parsedMinimumTransfer !== null) {
      const [arrivalHour, arrivalMinute] = String(scheduledArrival).split(":").map(Number);
      const [departureHour, departureMinute] = String(nextDeparture).split(":").map(Number);
      const arrivalTotal = arrivalHour * 60 + arrivalMinute;
      let departureTotal = departureHour * 60 + departureMinute;
      if (departureTotal < arrivalTotal) departureTotal += 24 * 60;
      buffer = departureTotal - arrivalTotal - parsedMinimumTransfer;
    }
    const safeRoute = group.routeId.replace(/[^A-Za-z0-9_-]/g, "-").slice(0, 42) || "route";
    const checkpointId = replayOnly ? `replay-${cleanIdentifier(replayRow.id) || `transfer-${index + 1}-${safeRoute}`}` : `ride-${index + 1}-${safeRoute}`;
    const checkpointNodeId = replayOnly ? String(replayRow.node_id || "") : String(group.from.node_id);
    const checkpointNodeOrder = replayOnly ? Number(replayRow.node_order) : Number(group.from.node_order);
    return {
      id: checkpointId.slice(0, 64),
      city: group.from.city_name || group.to.city_name || (group.cityCode ? `도시코드 ${group.cityCode}` : "도시 DATA_GAP"),
      cityCode: group.cityCode,
      routeId: group.routeId,
      routeNo: String(lastRide.route_no || group.rides[0].route_no || group.routeId),
      board: replayOnly ? (group.to.node_name || group.to.node_id) : (group.from.node_name || group.from.node_id),
      nodeId: checkpointNodeId,
      nodeOrder: checkpointNodeOrder,
      alight: group.to.node_name || group.to.node_id,
      alightNodeId: replayOnly ? String(replayRow.node_id || "") : String(group.to.node_id),
      alightNodeOrder: replayOnly ? Number(replayRow.node_order) : Number(group.to.node_order),
      scheduledArrival: isClockTime(scheduledArrival) ? String(scheduledArrival) : null,
      nextDeparture: isClockTime(nextDeparture) ? String(nextDeparture) : null,
      minimumTransfer: parsedMinimumTransfer,
      buffer,
      timeEvidenceSource,
      timeEvidenceVerified,
      timeEvidenceTripId,
      timeEvidenceFeedId,
      nextRouteId,
      nextNodeId,
      nextNodeOrder,
      nextTimeEvidenceTripId,
      nextTimeEvidenceFeedId,
      transferCheckpoint: replayOnly,
    };
  });
}

function loadMappingState(legs = []) {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(MAPPING_STORAGE_KEY) || "{}"); } catch { saved = {}; }
  return Object.fromEntries(legs.map((leg) => {
    const identifiers = saved?.[leg.id] || {};
    return [leg.id, {
      id: leg.id,
      cityCode: cleanIdentifier(identifiers.cityCode || leg.cityCode),
      nodeId: cleanIdentifier(identifiers.nodeId || leg.nodeId),
      routeId: cleanIdentifier(identifiers.routeId || leg.routeId),
      state: "unmapped",
      note: "서버 검증 전",
      code: "MAPPING_UNVERIFIED",
    }];
  }));
}

function persistMappingIdentifiers(mappings, legs) {
  const identifiersOnly = Object.fromEntries((legs || []).map((leg) => {
    const mapping = mappings[leg.id] || {};
    return [leg.id, {
      cityCode: cleanIdentifier(mapping.cityCode),
      nodeId: cleanIdentifier(mapping.nodeId),
      routeId: cleanIdentifier(mapping.routeId),
    }];
  }));
  localStorage.setItem(MAPPING_STORAGE_KEY, JSON.stringify(identifiersOnly));
}

function mergeMappingResult(current, result, onlyIds) {
  const targetIds = onlyIds ? new Set(onlyIds) : new Set(Object.keys(current));
  const byId = new Map((result.entries || []).map((entry) => [entry.id, entry]));
  return Object.fromEntries(Object.entries(current).map(([id, mapping]) => {
    if (!targetIds.has(id)) return [id, mapping];
    const entry = byId.get(id);
    if (!result.supported || !entry) return [id, { ...mapping, state: "unmapped", code: result.code || "DATA_GAP", note: result.message || "DATA_GAP · 검증 기능을 확인할 수 없습니다." }];
    return [id, { ...mapping, state: entry.verified ? "verified" : "unmapped", code: entry.code, note: entry.message }];
  }));
}

function loadUiState() {
  try {
    const saved = JSON.parse(localStorage.getItem(APP_STORAGE_KEY) || "{}");
    return {
      tab: ["explore", "live", "simulation", "journey"].includes(saved.tab) ? saved.tab : "explore",
      selectedLeg: cleanIdentifier(saved.selectedLeg),
    };
  } catch { return { tab: "explore", selectedLeg: "" }; }
}

function App() {
  const adminEnabled = new URLSearchParams(window.location.search).get("admin") === "1";
  const initial = useMemo(loadUiState, []);
  const [tab, setTab] = useState(initial.tab);
  const [activeJourney, setActiveJourney] = useState(null);
  const [selectedLeg, setSelectedLeg] = useState(initial.selectedLeg);
  const [connection, setConnection] = useState({ mode: "offline", label: "연결 확인 중", message: "로컬 데이터 서비스 상태를 확인하고 있습니다." });
  const [arrivals, setArrivals] = useState([]);
  const [history, setHistory] = useState([]);
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState("");
  const [liveNotice, setLiveNotice] = useState("");
  const [days, setDays] = useState(7);
  const [simulation, setSimulation] = useState(() => buildDataGapSimulation(7));
  const [simLoading, setSimLoading] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [apiBase, setApiBase] = useState(BusroApi.getBase());
  const [settingsError, setSettingsError] = useState("");
  const [statusPayload, setStatusPayload] = useState(null);
  const [mappings, setMappings] = useState({});
  const [passageCoverage, setPassageCoverage] = useState(EMPTY_PASSAGE_COVERAGE);
  const [legCoverage, setLegCoverage] = useState(EMPTY_PASSAGE_COVERAGE);
  const liveLegs = useMemo(() => deriveJourneyLegs(activeJourney), [activeJourney]);
  const replayCheckpoints = useMemo(() => deriveJourneyLegs(activeJourney, { replayOnly: true }), [activeJourney]);
  const operationalLegs = useMemo(() => [...liveLegs, ...replayCheckpoints], [liveLegs, replayCheckpoints]);
  const applyMappingState = (legs) => legs.map((item) => {
    const mapping = mappings[item.id] || {};
    return {
      ...item,
      cityCode: mapping.cityCode || item.cityCode,
      nodeId: mapping.nodeId || item.nodeId,
      routeId: mapping.routeId || item.routeId,
      apiMapped: mapping.state === "verified",
      mappingState: mapping.state || "unmapped",
      mappingNote: mapping.note || "서버 검증 전",
    };
  });
  const effectiveLiveLegs = useMemo(() => applyMappingState(liveLegs), [liveLegs, mappings]);
  const effectiveReplayCheckpoints = useMemo(() => applyMappingState(replayCheckpoints), [replayCheckpoints, mappings]);
  const effectiveOperationalLegs = useMemo(() => [...effectiveLiveLegs, ...effectiveReplayCheckpoints], [effectiveLiveLegs, effectiveReplayCheckpoints]);
  const leg = useMemo(() => effectiveLiveLegs.find((item) => item.id === selectedLeg) || effectiveLiveLegs[0], [effectiveLiveLegs, selectedLeg]);
  const summarizeMappings = (legs) => ({
    verified: legs.filter((item) => item.apiMapped).length,
    checking: legs.filter((item) => item.mappingState === "checking").length,
    total: legs.length,
  });
  const liveMappingSummary = useMemo(() => summarizeMappings(effectiveLiveLegs), [effectiveLiveLegs]);
  const replayMappingSummary = useMemo(() => summarizeMappings(effectiveReplayCheckpoints), [effectiveReplayCheckpoints]);
  const operationalMappingSummary = useMemo(() => summarizeMappings(effectiveOperationalLegs), [effectiveOperationalLegs]);
  const replayReady = useMemo(() => effectiveReplayCheckpoints.length > 0 && effectiveReplayCheckpoints.every((item) => (
    item.timeEvidenceVerified
    && item.timeEvidenceSource
    && item.timeEvidenceTripId
    && item.timeEvidenceFeedId
    && item.nextRouteId
    && item.nextNodeId
    && Number.isInteger(item.nextNodeOrder)
    && item.nextTimeEvidenceTripId
    && item.nextTimeEvidenceFeedId
    && isClockTime(item.scheduledArrival)
    && isClockTime(item.nextDeparture)
    && Number.isInteger(item.minimumTransfer)
    && item.alightNodeId
    && Number.isInteger(item.alightNodeOrder)
  )), [effectiveReplayCheckpoints]);
  const replayApplicability = useMemo(() => {
    if (!activeJourney) return "journey_required";
    if (journeyUsesCurrentTimetable(activeJourney) && Number(activeJourney.transfers) === 0) return "not_applicable";
    return replayReady ? "ready" : "data_gap";
  }, [activeJourney, replayReady]);

  useEffect(() => { localStorage.setItem(APP_STORAGE_KEY, JSON.stringify({ tab, selectedLeg })); }, [tab, selectedLeg]);

  async function checkConnection(mappingSnapshot = mappings, legsSnapshot = effectiveOperationalLegs) {
    setConnection({ mode: "offline", label: "연결 확인 중", message: "로컬 데이터 서비스 상태를 확인하고 있습니다." });
    try {
      const status = await BusroApi.status();
      setStatusPayload(status);
      let mappingResult = { supported: false, entries: [], code: "JOURNEY_REQUIRED", message: "전국 여행 후보를 선택하면 해당 구간만 검증합니다." };
      try {
        if (legsSnapshot.length > 0) mappingResult = await BusroApi.mappingStatus(status, Object.values(mappingSnapshot));
      }
      catch (error) { mappingResult = { supported: false, code: error.payload?.code || "MAPPING_STATUS_FAILED", entries: [], message: "DATA_GAP · 공식 식별자 검증 상태를 불러오지 못했습니다." }; }
      const nextMappings = mergeMappingResult(mappingSnapshot, mappingResult);
      setMappings(nextMappings);
      const tagoState = status.tago?.state;
      const mappedCount = Object.values(nextMappings).filter((item) => item.state === "verified").length;
      const total = legsSnapshot.length;
      const allMapped = total > 0 && mappedCount === total;
      const collectionReady = status.capabilities?.snapshot_collection === true
        && status.capabilities?.position_snapshot_collection === true;
      const hydrationReady = Boolean(status.capabilities?.verified_route_hydration);
      const mode = tagoState === "fixture" ? "fixture" : tagoState === "ready" && allMapped ? "live" : tagoState === "ready" ? "ready" : "offline";
      const label = mode === "live" ? "TAGO LIVE" : mode === "ready" ? (total ? "TAGO 연결 · 매핑 필요" : "TAGO 연결 · 여행 선택 필요") : mode === "fixture" ? "FIXTURE 연결" : "TAGO 연결 필요";
      const message = mode === "live"
        ? `공식 TAGO 연결과 선택 여행 ${total}개 구간의 공식 식별자가 모두 검증됐습니다.`
        : mode === "ready"
          ? (total ? `공식 TAGO 연결은 준비됐지만 선택 여행 매핑은 ${mappedCount}/${total}개입니다. LIVE로 표시하지 않습니다.` : "공식 TAGO 연결이 준비됐습니다. 전국 탐색에서 여행 후보를 먼저 선택하세요.")
          : mode === "fixture"
            ? "스키마 검증용 FIXTURE입니다. 선택 여행의 실시간 도착정보나 성공률로 표시하지 않습니다."
            : "서버는 켜져 있지만 공식 TAGO 데이터 연결이 준비되지 않았습니다.";
      setConnection({ mode, label, message, tagoReady: tagoState === "ready", apiMapped: allMapped, mappingSupported: mappingResult.supported, collectionReady, hydrationReady });
      return { status, mappings: nextMappings };
    } catch {
      setStatusPayload(null);
      setConnection({ mode: "offline", label: "로컬 서비스 꺼짐", message: "로컬 데이터 서비스를 시작하거나 설정 주소를 확인하면 실시간 조회와 이력 저장이 활성화됩니다." });
      return null;
    }
  }

  async function loadLive(target = leg) {
    setLiveLoading(true); setLiveError("");
    if (!target) {
      setArrivals([]); setHistory([]); setLegCoverage(EMPTY_PASSAGE_COVERAGE); setLiveError(""); setLiveLoading(false); return;
    }
    if (connection.mode !== "live" || !target.apiMapped) {
      const code = connection.mode === "fixture" ? "FIXTURE_NOT_LIVE" : connection.tagoReady ? "MAPPING_REQUIRED" : "TAGO_KEY_REQUIRED";
      const message = connection.mode === "fixture"
        ? "DATA_GAP · FIXTURE 응답은 선택 여행의 실시간 데이터로 표시하지 않습니다."
        : connection.tagoReady
          ? "DATA_GAP · 이 구간의 공식 cityCode·nodeId·routeId 매핑이 아직 검증되지 않았습니다."
          : "DATA_GAP · TAGO 공식 데이터 연결 후 이 구간의 도착정보를 조회할 수 있습니다.";
      setArrivals([]); setHistory([]); setLegCoverage({ ...EMPTY_PASSAGE_COVERAGE, supported: Boolean(statusPayload), code }); setLiveError(message); setLiveLoading(false); return;
    }
    try {
      const [arrivalPayload, historyPayload, coveragePayload] = await Promise.all([
        BusroApi.arrivals(target),
        BusroApi.history(target, 14),
        BusroApi.passageCoverage(statusPayload, target, 14).catch(() => ({ supported: false, count: 0, eligibleDays: 0, gapCount: 0, dataGap: true, code: "PASSAGE_HISTORY_UNAVAILABLE" })),
      ]);
      const normalizedArrivals = (arrivalPayload.arrivals || arrivalPayload.items || []).filter((item) => {
        const itemRouteId = item.route_id ?? item.routeid ?? item.routeId;
        return itemRouteId && String(itemRouteId) === String(target.routeId);
      }).map((item) => ({
        ...item,
        minutes: item.minutes ?? item.arrival_minutes ?? Math.max(0, Math.ceil(Number(item.arrival_seconds || 0) / 60)),
        stops: item.stops ?? item.remaining_stops,
        vehicleNo: item.vehicleNo || item.vehicle_no || item.vehicle_type,
      }));
      const normalizedHistory = (historyPayload.snapshots || []).flatMap((snapshot) => (snapshot.arrivals || []).filter((item) => String(item.route_id || item.routeid || "") === String(target.routeId)).map((item) => ({
        timestamp: snapshot.captured_at,
        label: String(snapshot.captured_at || "").slice(5, 10),
        delay: Math.max(0, Math.ceil(Number(item.arrival_seconds || 0) / 60)),
      })));
      setArrivals(normalizedArrivals);
      setHistory(normalizedHistory);
      setLegCoverage(coveragePayload);
    } catch (error) {
      setArrivals([]);
      setHistory([]);
      setLegCoverage({ supported: false, count: 0, eligibleDays: 0, gapCount: 0, dataGap: true, code: "PASSAGE_HISTORY_UNAVAILABLE" });
      const errorCode = error.payload?.error?.code;
      setLiveError(error.status === 503 && ["TAGO_KEY_REQUIRED", "TAGO_KEY_INVALID"].includes(errorCode)
        ? "DATA_GAP · TAGO 서비스 키가 아직 없습니다."
        : "DATA_GAP · 선택 구간의 공식 데이터를 불러오지 못했습니다.");
    } finally { setLiveLoading(false); }
  }

  async function collectSnapshot() {
    setLiveLoading(true); setLiveError(""); setLiveNotice("");
    if (!leg || connection.mode !== "live" || !leg.apiMapped || !connection.collectionReady) {
      setLiveError("DATA_GAP · TAGO LIVE, 공식 구간 검증, 공유 이력 저장소가 모두 준비된 뒤에만 저장합니다.");
      setLiveLoading(false);
      return;
    }
    try {
      const [arrivalResult, positionResult] = await Promise.all([
        BusroApi.collect(leg),
        BusroApi.collectPositions(leg),
      ]);
      const passages = Array.isArray(positionResult.passages) ? positionResult.passages.length : 0;
      const duplicated = arrivalResult.created === false && positionResult.created === false;
      setLiveNotice(duplicated
        ? "같은 관측 구간은 이미 저장되어 중복 적재하지 않았습니다."
        : `도착·차량 위치를 저장했습니다. 이번 폴링에서 통과 사건 ${passages}건을 재구성했습니다.`);
      await loadLive(leg);
    } catch (error) { setLiveError(error.message || "현재 응답을 저장하지 못했습니다."); setLiveLoading(false); }
  }

  async function loadPassageCoverage(targetLegs = effectiveReplayCheckpoints) {
    if (!targetLegs.length) {
      setPassageCoverage(EMPTY_PASSAGE_COVERAGE);
      return EMPTY_PASSAGE_COVERAGE;
    }
    try {
      const coverage = await BusroApi.passageCoverage(statusPayload, targetLegs, days);
      setPassageCoverage(coverage);
      return coverage;
    } catch {
      const gap = { supported: false, count: 0, eligibleDays: 0, gapCount: 0, dataGap: true, code: "PASSAGE_HISTORY_UNAVAILABLE" };
      setPassageCoverage(gap);
      return gap;
    }
  }

  async function runSimulation() {
    setSimLoading(true);
    if (!activeJourney) {
      setSimulation(buildDataGapSimulation(days));
      setSimLoading(false); return;
    }
    if (replayApplicability === "not_applicable") {
      setSimulation(buildDataGapSimulation(days, "NO_TRANSFER_CONNECTIONS", "직통 경로는 환승 연결 성공·실패 시뮬레이션 대상이 아닙니다."));
      setSimLoading(false); return;
    }
    if (effectiveReplayCheckpoints.length === 0) {
      setSimulation(buildDataGapSimulation(days, "VERIFIED_TIMETABLE_TIMES_REQUIRED", "검증된 환승 체크포인트가 필요합니다."));
      setSimLoading(false); return;
    }
    if (!replayReady) {
      setSimulation(buildDataGapSimulation(days, "VERIFIED_TIMETABLE_TIMES_REQUIRED", "검증된 현재 시간표의 환승 시각 근거가 필요합니다."));
      setSimLoading(false); return;
    }
    if (connection.mode !== "live") {
      setSimulation(buildDataGapSimulation(days, "TAGO_LIVE_REQUIRED", "TAGO LIVE 연결과 실제 통과 이력이 필요합니다."));
      setSimLoading(false); return;
    }
    if (effectiveReplayCheckpoints.some((item) => !item.apiMapped)) {
      setSimulation(buildDataGapSimulation(days, "UNMAPPED_OFFICIAL_STOP", "선택 여행의 공식 구간 매핑이 필요합니다."));
      setSimLoading(false); return;
    }
    try {
      const payload = await BusroApi.replay(days, effectiveReplayCheckpoints, activeJourney);
      const rawDays = payload.daily || payload.perDay || payload.per_day || [];
      const perDay = rawDays.map((day) => {
        const status = String(day.status || "data_gap").toLowerCase();
        return {
          date: day.date,
          success: status === "success" ? true : status === "failure" ? false : null,
          status: status === "data_gap" ? "gap" : status,
          probability: null,
          reasons: [day.reason].filter(Boolean),
        };
      });
      const successRate = payload.summary?.success_rate;
      const eventCount = Number(payload.basis?.events_scanned || 0);
      const eligibleDays = Number(payload.summary?.eligible_days || 0);
      setSimulation({
        ...payload,
        mode: payload.basis?.mode === "live" ? "live" : "offline",
        perDay,
        summary: {
          probability: typeof successRate === "number" ? Math.round(successRate * 100) : null,
          successfulDays: payload.summary?.success_days ?? perDay.filter((day) => day.success).length,
          totalDays: payload.summary?.days ?? perDay.length,
          weakestLeg: Number(payload.summary?.failure_days || 0) > 0 ? "실패 일자 확인" : eligibleDays > 0 ? "확인된 실패 없음" : "판정 불가",
          coverage: eventCount,
          passageEvidence: eventCount > 0,
          dataGap: eligibleDays === 0,
        },
      });
    } catch {
      setSimulation(buildDataGapSimulation(days, "PASSAGE_HISTORY_REQUIRED", "선택 여행의 실제 통과 이력이 부족합니다."));
    }
    finally { setSimLoading(false); }
  }

  useEffect(() => { checkConnection({}, []); }, []);
  useEffect(() => { if (tab === "live" && leg) loadLive(leg); }, [tab, selectedLeg, leg?.apiMapped, statusPayload]);
  useEffect(() => { if (tab === "simulation") loadPassageCoverage(); }, [tab, statusPayload, replayMappingSummary.verified, days]);
  useEffect(() => {
    if (!activeJourney) setSimulation(buildDataGapSimulation(days));
    else if (replayApplicability === "not_applicable") setSimulation(buildDataGapSimulation(days, "NO_TRANSFER_CONNECTIONS", "직통 경로는 환승 연결 성공·실패 시뮬레이션 대상이 아닙니다."));
    else if (!replayReady) setSimulation(buildDataGapSimulation(days, "VERIFIED_TIMETABLE_TIMES_REQUIRED", "검증된 현재 시간표의 환승 시각 근거가 필요합니다."));
    else setSimulation(buildDataGapSimulation(days, "REPLAY_NOT_RUN", "선택 기간의 실제 통과 이력을 재생하세요."));
  }, [days, activeJourney, replayApplicability, replayReady]);

  function changeTab(next) { setTab(next); document.querySelector(".screen-scroll")?.scrollTo({ top: 0, behavior: "smooth" }); }
  function openJourney(candidate) {
    const nextLiveLegs = deriveJourneyLegs(candidate);
    const nextReplayCheckpoints = deriveJourneyLegs(candidate, { replayOnly: true });
    const nextOperationalLegs = [...nextLiveLegs, ...nextReplayCheckpoints];
    const nextMappings = loadMappingState(nextOperationalLegs);
    setActiveJourney(candidate);
    setMappings(nextMappings);
    setSelectedLeg(nextLiveLegs[0]?.id || "");
    setArrivals([]); setHistory([]); setLiveError(""); setLiveNotice("");
    setLegCoverage({ ...EMPTY_PASSAGE_COVERAGE, code: nextLiveLegs.length ? "PASSAGE_HISTORY_UNAVAILABLE" : "JOURNEY_RIDE_LEGS_REQUIRED" });
    setPassageCoverage({ ...EMPTY_PASSAGE_COVERAGE, code: nextReplayCheckpoints.length ? "PASSAGE_HISTORY_UNAVAILABLE" : "VERIFIED_TRANSFER_CHECKPOINTS_REQUIRED" });
    setSimulation(journeyUsesCurrentTimetable(candidate) && Number(candidate.transfers) === 0
      ? buildDataGapSimulation(days, "NO_TRANSFER_CONNECTIONS", "직통 경로는 환승 연결 성공·실패 시뮬레이션 대상이 아닙니다.")
      : buildDataGapSimulation(days, "VERIFIED_TIMETABLE_TIMES_REQUIRED", "검증된 현재 시간표의 환승 시각 근거가 필요합니다."));
    checkConnection(nextMappings, nextOperationalLegs);
    changeTab("journey");
  }
  function updateMapping(id, field, value) {
    setMappings((current) => ({ ...current, [id]: { ...current[id], [field]: cleanIdentifier(value), state: "unmapped", code: "MAPPING_CHANGED", note: "변경된 식별자는 다시 검증해야 합니다." } }));
    setConnection((current) => current.mode === "live" ? { ...current, mode: "ready", apiMapped: false, label: "TAGO 연결 · 매핑 필요", message: "식별자가 변경되어 LIVE 표시를 중지했습니다. 선택 여행 구간을 다시 검증하세요." } : current);
  }

  async function verifyMapping(id) {
    const candidate = mappings[id];
    if (!candidate?.cityCode || !candidate?.nodeId || !candidate?.routeId) return;
    persistMappingIdentifiers(mappings, operationalLegs);
    setMappings((current) => ({ ...current, [id]: { ...current[id], state: "checking", note: "공식 식별자 검증 중…" } }));
    try {
      const latestStatus = statusPayload || await BusroApi.status();
      if (!statusPayload) setStatusPayload(latestStatus);
      const result = await BusroApi.validateMapping(latestStatus, candidate);
      const next = mergeMappingResult({ ...mappings, [id]: { ...candidate, state: "checking" } }, result, [id]);
      setMappings(next);
      await checkConnection(next, effectiveOperationalLegs);
    } catch (error) {
      setMappings((current) => ({ ...current, [id]: { ...current[id], state: "unmapped", code: error.payload?.code || "MAPPING_VALIDATION_FAILED", note: "DATA_GAP · 서버 검증에 실패했습니다." } }));
    }
  }

  async function saveConnection() {
    setSettingsError("");
    try {
      BusroApi.setBase(apiBase);
      persistMappingIdentifiers(mappings, operationalLegs);
      await checkConnection(mappings, effectiveOperationalLegs);
    } catch (error) { setSettingsError(error.message || "연결 설정을 저장하지 못했습니다."); }
  }

  return (
    <div className="stage">
      <main className={`app-shell tab-${tab}`} aria-label="버스로 잇다">
        <AppHeader connection={connection} onSettings={adminEnabled ? () => setSettingsOpen(true) : null} />
        <div className="screen-scroll">
          {tab === "explore" && <NationwideScreen connection={connection} onChooseJourney={openJourney} />}
          {tab === "live" && <LiveScreen journey={activeJourney} connection={connection} legs={effectiveLiveLegs} selectedLeg={selectedLeg} setSelectedLeg={setSelectedLeg} arrivals={arrivals} history={history} passageCoverage={legCoverage} mappingSummary={liveMappingSummary} loading={liveLoading} error={liveError} notice={liveNotice} onRefresh={() => loadLive(leg)} onCollect={collectSnapshot} onExplore={() => changeTab("explore")} />}
          {tab === "simulation" && <SimulationScreen journey={activeJourney} replayReady={replayReady} replayApplicability={replayApplicability} connection={connection} simulation={simulation} days={days} setDays={setDays} passageCoverage={passageCoverage} mappingSummary={replayMappingSummary} loading={simLoading} onRun={runSimulation} onExplore={() => changeTab("explore")} />}
          {tab === "journey" && <JourneyScreen journey={activeJourney} connection={connection} onExplore={() => changeTab("explore")} />}
        </div>
        <BottomDock tab={tab} onChange={changeTab} />
        {adminEnabled && <SettingsSheet open={settingsOpen} onClose={() => setSettingsOpen(false)} apiBase={apiBase} setApiBase={setApiBase} connection={connection} journey={activeJourney} mappings={mappings} legs={effectiveOperationalLegs} mappingSummary={operationalMappingSummary} settingsError={settingsError} onMappingChange={updateMapping} onVerifyMapping={verifyMapping} onReconnect={saveConnection} />}
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
