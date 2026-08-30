const APP_STORAGE_KEY = "busro-itda-glass-v1";
const MAPPING_STORAGE_KEY = "busro-itda-official-identifiers-v1";
const EMPTY_PASSAGE_COVERAGE = {
  supported: false,
  count: 0,
  eligibleDays: 0,
  gapCount: 0,
  dataGap: true,
  code: "JOURNEY_REQUIRED"
};
function cleanIdentifier(value) {
  return String(value || "").replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, 128);
}
function isClockTime(value) {
  return /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(String(value || ""));
}
function buildDataGapSimulation(days, code = "JOURNEY_REQUIRED", reason = "\uC804\uAD6D \uC5EC\uD589 \uD6C4\uBCF4\uB97C \uBA3C\uC800 \uC120\uD0DD\uD558\uC138\uC694.") {
  const end = /* @__PURE__ */ new Date();
  const perDay = Array.from({ length: Math.max(1, Number(days) || 7) }, (_, index) => {
    const date = new Date(end);
    date.setDate(end.getDate() - (Math.max(1, Number(days) || 7) - index - 1));
    return { date: date.toISOString().slice(0, 10), probability: null, success: null, status: "gap", reasons: [code] };
  });
  return {
    mode: "offline",
    perDay,
    summary: { probability: null, successfulDays: 0, totalDays: perDay.length, weakestLeg: reason, coverage: 0, dataGap: true, code }
  };
}
function replayValue(step, key) {
  return step?.replay?.[key] ?? step?.timetable?.[key] ?? step?.time_evidence?.[key] ?? step?.[key];
}
function deriveJourneyLegs(journey) {
  const steps = Array.isArray(journey?.steps) ? journey.steps : [];
  const groups = [];
  let current = null;
  steps.forEach((step) => {
    if (step?.kind !== "ride" || !step.route_id || !step.from?.node_id || !step.to?.node_id) {
      current = null;
      return;
    }
    const cityCode = String(step.from.city_code || step.to.city_code || "");
    const continues = current && current.routeId === String(step.route_id) && current.cityCode === cityCode && current.to.node_id === step.from.node_id && Number(current.to.node_order) === Number(step.from.node_order);
    if (!continues) {
      current = { routeId: String(step.route_id), cityCode, from: step.from, to: step.to, rides: [step] };
      groups.push(current);
    } else {
      current.to = step.to;
      current.rides.push(step);
    }
  });
  const replayRows = Array.isArray(journey?.replay_legs) ? journey.replay_legs : [];
  return groups.map((group, index) => {
    const lastRide = group.rides[group.rides.length - 1];
    const replayRow = replayRows[index] || replayRows.find((item) => String(item?.route_id || "") === group.routeId) || {};
    const scheduledArrival = replayRow.scheduled_arrival ?? replayValue(lastRide, "scheduled_arrival");
    const nextDeparture = replayRow.next_departure ?? replayValue(lastRide, "next_departure");
    const minimumTransfer = replayRow.minimum_transfer_minutes ?? replayValue(lastRide, "minimum_transfer_minutes");
    const evidenceBlock = replayRow.time_evidence || lastRide?.time_evidence || lastRide?.timetable || lastRide?.replay || {};
    const timeEvidenceSource = cleanIdentifier(replayRow.time_evidence_source || evidenceBlock.source || "");
    const timeEvidenceVerified = replayRow.time_evidence_verified === true || evidenceBlock.verified === true;
    const parsedMinimumTransfer = minimumTransfer !== null && minimumTransfer !== void 0 && String(minimumTransfer).trim() !== "" && Number.isInteger(Number(minimumTransfer)) ? Number(minimumTransfer) : null;
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
    return {
      id: `leg-${index + 1}-${safeRoute}`.slice(0, 64),
      city: group.from.city_name || group.to.city_name || (group.cityCode ? `\uB3C4\uC2DC\uCF54\uB4DC ${group.cityCode}` : "\uB3C4\uC2DC DATA_GAP"),
      cityCode: group.cityCode,
      routeId: group.routeId,
      routeNo: String(lastRide.route_no || group.rides[0].route_no || group.routeId),
      board: group.from.node_name || group.from.node_id,
      nodeId: String(group.from.node_id),
      nodeOrder: Number(group.from.node_order),
      alight: group.to.node_name || group.to.node_id,
      alightNodeId: String(group.to.node_id),
      alightNodeOrder: Number(group.to.node_order),
      scheduledArrival: isClockTime(scheduledArrival) ? String(scheduledArrival) : null,
      nextDeparture: isClockTime(nextDeparture) ? String(nextDeparture) : null,
      minimumTransfer: parsedMinimumTransfer,
      buffer,
      timeEvidenceSource,
      timeEvidenceVerified
    };
  });
}
function loadMappingState(legs = []) {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(MAPPING_STORAGE_KEY) || "{}");
  } catch {
    saved = {};
  }
  return Object.fromEntries(legs.map((leg) => {
    const identifiers = saved?.[leg.id] || {};
    return [leg.id, {
      id: leg.id,
      cityCode: cleanIdentifier(identifiers.cityCode || leg.cityCode),
      nodeId: cleanIdentifier(identifiers.nodeId || leg.nodeId),
      routeId: cleanIdentifier(identifiers.routeId || leg.routeId),
      state: "unmapped",
      note: "\uC11C\uBC84 \uAC80\uC99D \uC804",
      code: "MAPPING_UNVERIFIED"
    }];
  }));
}
function persistMappingIdentifiers(mappings, legs) {
  const identifiersOnly = Object.fromEntries((legs || []).map((leg) => {
    const mapping = mappings[leg.id] || {};
    return [leg.id, {
      cityCode: cleanIdentifier(mapping.cityCode),
      nodeId: cleanIdentifier(mapping.nodeId),
      routeId: cleanIdentifier(mapping.routeId)
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
    if (!result.supported || !entry) return [id, { ...mapping, state: "unmapped", code: result.code || "DATA_GAP", note: result.message || "DATA_GAP \xB7 \uAC80\uC99D \uAE30\uB2A5\uC744 \uD655\uC778\uD560 \uC218 \uC5C6\uC2B5\uB2C8\uB2E4." }];
    return [id, { ...mapping, state: entry.verified ? "verified" : "unmapped", code: entry.code, note: entry.message }];
  }));
}
function loadUiState() {
  try {
    const saved = JSON.parse(localStorage.getItem(APP_STORAGE_KEY) || "{}");
    return {
      tab: ["explore", "live", "simulation", "journey"].includes(saved.tab) ? saved.tab : "explore",
      selectedLeg: cleanIdentifier(saved.selectedLeg)
    };
  } catch {
    return { tab: "explore", selectedLeg: "" };
  }
}
function App() {
  const initial = useMemo(loadUiState, []);
  const [tab, setTab] = useState(initial.tab);
  const [activeJourney, setActiveJourney] = useState(null);
  const [selectedLeg, setSelectedLeg] = useState(initial.selectedLeg);
  const [connection, setConnection] = useState({ mode: "offline", label: "\uC5F0\uACB0 \uD655\uC778 \uC911", message: "\uB85C\uCEEC \uB370\uC774\uD130 \uC11C\uBE44\uC2A4 \uC0C1\uD0DC\uB97C \uD655\uC778\uD558\uACE0 \uC788\uC2B5\uB2C8\uB2E4." });
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
  const activeLegs = useMemo(() => deriveJourneyLegs(activeJourney), [activeJourney]);
  const effectiveLegs = useMemo(() => activeLegs.map((item) => {
    const mapping = mappings[item.id] || {};
    return {
      ...item,
      cityCode: mapping.cityCode || item.cityCode,
      nodeId: mapping.nodeId || item.nodeId,
      routeId: mapping.routeId || item.routeId,
      apiMapped: mapping.state === "verified",
      mappingState: mapping.state || "unmapped",
      mappingNote: mapping.note || "\uC11C\uBC84 \uAC80\uC99D \uC804"
    };
  }), [activeLegs, mappings]);
  const leg = useMemo(() => effectiveLegs.find((item) => item.id === selectedLeg) || effectiveLegs[0], [effectiveLegs, selectedLeg]);
  const mappingSummary = useMemo(() => ({
    verified: effectiveLegs.filter((item) => item.apiMapped).length,
    checking: effectiveLegs.filter((item) => item.mappingState === "checking").length,
    total: effectiveLegs.length
  }), [effectiveLegs]);
  const replayReady = useMemo(() => effectiveLegs.length > 0 && effectiveLegs.every((item) => item.timeEvidenceVerified && item.timeEvidenceSource && isClockTime(item.scheduledArrival) && isClockTime(item.nextDeparture) && Number.isInteger(item.minimumTransfer) && item.alightNodeId && Number.isInteger(item.alightNodeOrder)), [effectiveLegs]);
  useEffect(() => {
    localStorage.setItem(APP_STORAGE_KEY, JSON.stringify({ tab, selectedLeg }));
  }, [tab, selectedLeg]);
  async function checkConnection(mappingSnapshot = mappings, legsSnapshot = effectiveLegs) {
    setConnection({ mode: "offline", label: "\uC5F0\uACB0 \uD655\uC778 \uC911", message: "\uB85C\uCEEC \uB370\uC774\uD130 \uC11C\uBE44\uC2A4 \uC0C1\uD0DC\uB97C \uD655\uC778\uD558\uACE0 \uC788\uC2B5\uB2C8\uB2E4." });
    try {
      const status = await BusroApi.status();
      setStatusPayload(status);
      let mappingResult = { supported: false, entries: [], code: "JOURNEY_REQUIRED", message: "\uC804\uAD6D \uC5EC\uD589 \uD6C4\uBCF4\uB97C \uC120\uD0DD\uD558\uBA74 \uD574\uB2F9 \uAD6C\uAC04\uB9CC \uAC80\uC99D\uD569\uB2C8\uB2E4." };
      try {
        if (legsSnapshot.length > 0) mappingResult = await BusroApi.mappingStatus(status, Object.values(mappingSnapshot));
      } catch (error) {
        mappingResult = { supported: false, code: error.payload?.code || "MAPPING_STATUS_FAILED", entries: [], message: "DATA_GAP \xB7 \uACF5\uC2DD \uC2DD\uBCC4\uC790 \uAC80\uC99D \uC0C1\uD0DC\uB97C \uBD88\uB7EC\uC624\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4." };
      }
      const nextMappings = mergeMappingResult(mappingSnapshot, mappingResult);
      setMappings(nextMappings);
      const tagoState = status.tago?.state;
      const mappedCount = Object.values(nextMappings).filter((item) => item.state === "verified").length;
      const total = legsSnapshot.length;
      const allMapped = total > 0 && mappedCount === total;
      const mode = tagoState === "fixture" ? "fixture" : tagoState === "ready" && allMapped ? "live" : tagoState === "ready" ? "ready" : "offline";
      const label = mode === "live" ? "TAGO LIVE" : mode === "ready" ? total ? "TAGO \uC5F0\uACB0 \xB7 \uB9E4\uD551 \uD544\uC694" : "TAGO \uC5F0\uACB0 \xB7 \uC5EC\uD589 \uC120\uD0DD \uD544\uC694" : mode === "fixture" ? "FIXTURE \uC5F0\uACB0" : "TAGO \uD0A4 \uD544\uC694";
      const message = mode === "live" ? `\uC11C\uBE44\uC2A4 \uD0A4\uC640 \uC120\uD0DD \uC5EC\uD589 ${total}\uAC1C \uAD6C\uAC04\uC758 \uACF5\uC2DD \uC2DD\uBCC4\uC790\uAC00 \uBAA8\uB450 \uAC80\uC99D\uB410\uC2B5\uB2C8\uB2E4.` : mode === "ready" ? total ? `\uC11C\uBE44\uC2A4 \uD0A4\uB294 \uD655\uC778\uB410\uC9C0\uB9CC \uC120\uD0DD \uC5EC\uD589 \uB9E4\uD551\uC740 ${mappedCount}/${total}\uAC1C\uC785\uB2C8\uB2E4. LIVE\uB85C \uD45C\uC2DC\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.` : "\uC11C\uBE44\uC2A4 \uD0A4\uB294 \uD655\uC778\uB410\uC2B5\uB2C8\uB2E4. \uC804\uAD6D \uD0D0\uC0C9\uC5D0\uC11C \uC5EC\uD589 \uD6C4\uBCF4\uB97C \uBA3C\uC800 \uC120\uD0DD\uD558\uC138\uC694." : mode === "fixture" ? "\uC2A4\uD0A4\uB9C8 \uAC80\uC99D\uC6A9 FIXTURE\uC785\uB2C8\uB2E4. \uC120\uD0DD \uC5EC\uD589\uC758 \uC2E4\uC2DC\uAC04 \uB3C4\uCC29\uC815\uBCF4\uB098 \uC131\uACF5\uB960\uB85C \uD45C\uC2DC\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4." : "\uC11C\uBC84\uB294 \uCF1C\uC838 \uC788\uC9C0\uB9CC TAGO \uC11C\uBE44\uC2A4 \uD0A4\uAC00 \uC124\uC815\uB418\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.";
      setConnection({ mode, label, message, tagoReady: tagoState === "ready", apiMapped: allMapped, mappingSupported: mappingResult.supported });
      return { status, mappings: nextMappings };
    } catch {
      setStatusPayload(null);
      setConnection({ mode: "offline", label: "\uB85C\uCEEC \uC11C\uBE44\uC2A4 \uAEBC\uC9D0", message: "\uB85C\uCEEC \uB370\uC774\uD130 \uC11C\uBE44\uC2A4\uB97C \uC2DC\uC791\uD558\uAC70\uB098 \uC124\uC815 \uC8FC\uC18C\uB97C \uD655\uC778\uD558\uBA74 \uC2E4\uC2DC\uAC04 \uC870\uD68C\uC640 \uC774\uB825 \uC800\uC7A5\uC774 \uD65C\uC131\uD654\uB429\uB2C8\uB2E4." });
      return null;
    }
  }
  async function loadLive(target = leg) {
    setLiveLoading(true);
    setLiveError("");
    if (!target) {
      setArrivals([]);
      setHistory([]);
      setLegCoverage(EMPTY_PASSAGE_COVERAGE);
      setLiveError("");
      setLiveLoading(false);
      return;
    }
    if (connection.mode !== "live" || !target.apiMapped) {
      const code = connection.mode === "fixture" ? "FIXTURE_NOT_LIVE" : connection.tagoReady ? "MAPPING_REQUIRED" : "TAGO_KEY_REQUIRED";
      const message = connection.mode === "fixture" ? "DATA_GAP \xB7 FIXTURE \uC751\uB2F5\uC740 \uC120\uD0DD \uC5EC\uD589\uC758 \uC2E4\uC2DC\uAC04 \uB370\uC774\uD130\uB85C \uD45C\uC2DC\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4." : connection.tagoReady ? "DATA_GAP \xB7 \uC774 \uAD6C\uAC04\uC758 \uACF5\uC2DD cityCode\xB7nodeId\xB7routeId \uB9E4\uD551\uC774 \uC544\uC9C1 \uAC80\uC99D\uB418\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4." : "DATA_GAP \xB7 TAGO \uC11C\uBE44\uC2A4 \uD0A4 \uC5F0\uACB0 \uD6C4 \uC774 \uAD6C\uAC04\uC758 \uACF5\uC2DD \uB3C4\uCC29\uC815\uBCF4\uB97C \uC870\uD68C\uD560 \uC218 \uC788\uC2B5\uB2C8\uB2E4.";
      setArrivals([]);
      setHistory([]);
      setLegCoverage({ ...EMPTY_PASSAGE_COVERAGE, supported: Boolean(statusPayload), code });
      setLiveError(message);
      setLiveLoading(false);
      return;
    }
    try {
      const [arrivalPayload, historyPayload, coveragePayload] = await Promise.all([
        BusroApi.arrivals(target),
        BusroApi.history(target, 14),
        BusroApi.passageCoverage(statusPayload, target, 14).catch(() => ({ supported: false, count: 0, eligibleDays: 0, gapCount: 0, dataGap: true, code: "PASSAGE_HISTORY_UNAVAILABLE" }))
      ]);
      const normalizedArrivals = (arrivalPayload.arrivals || arrivalPayload.items || []).filter((item) => {
        const itemRouteId = item.route_id ?? item.routeid ?? item.routeId;
        return itemRouteId && String(itemRouteId) === String(target.routeId);
      }).map((item) => ({
        ...item,
        minutes: item.minutes ?? item.arrival_minutes ?? Math.max(0, Math.ceil(Number(item.arrival_seconds || 0) / 60)),
        stops: item.stops ?? item.remaining_stops,
        vehicleNo: item.vehicleNo || item.vehicle_no || item.vehicle_type
      }));
      const normalizedHistory = (historyPayload.snapshots || []).flatMap((snapshot) => (snapshot.arrivals || []).filter((item) => String(item.route_id || item.routeid || "") === String(target.routeId)).map((item) => ({
        timestamp: snapshot.captured_at,
        label: String(snapshot.captured_at || "").slice(5, 10),
        delay: Math.max(0, Math.ceil(Number(item.arrival_seconds || 0) / 60))
      })));
      setArrivals(normalizedArrivals);
      setHistory(normalizedHistory);
      setLegCoverage(coveragePayload);
    } catch (error) {
      setArrivals([]);
      setHistory([]);
      setLegCoverage({ supported: false, count: 0, eligibleDays: 0, gapCount: 0, dataGap: true, code: "PASSAGE_HISTORY_UNAVAILABLE" });
      setLiveError(error.status === 503 ? "DATA_GAP \xB7 TAGO \uC11C\uBE44\uC2A4 \uD0A4\uAC00 \uC544\uC9C1 \uC5C6\uC2B5\uB2C8\uB2E4." : "DATA_GAP \xB7 \uC120\uD0DD \uAD6C\uAC04\uC758 \uACF5\uC2DD \uB370\uC774\uD130\uB97C \uBD88\uB7EC\uC624\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.");
    } finally {
      setLiveLoading(false);
    }
  }
  async function collectSnapshot() {
    setLiveLoading(true);
    setLiveError("");
    setLiveNotice("");
    if (!leg || connection.mode !== "live" || !leg.apiMapped) {
      setLiveError("DATA_GAP \xB7 TAGO LIVE\uC640 \uACF5\uC2DD \uAD6C\uAC04 \uAC80\uC99D\uC774 \uC644\uB8CC\uB41C \uB4A4\uC5D0\uB9CC \uC774\uB825\uC744 \uC800\uC7A5\uD569\uB2C8\uB2E4.");
      setLiveLoading(false);
      return;
    }
    try {
      const [arrivalResult, positionResult] = await Promise.all([
        BusroApi.collect(leg),
        BusroApi.collectPositions(leg)
      ]);
      const passages = Array.isArray(positionResult.passages) ? positionResult.passages.length : 0;
      const duplicated = arrivalResult.created === false && positionResult.created === false;
      setLiveNotice(duplicated ? "\uAC19\uC740 \uAD00\uCE21 \uAD6C\uAC04\uC740 \uC774\uBBF8 \uC800\uC7A5\uB418\uC5B4 \uC911\uBCF5 \uC801\uC7AC\uD558\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4." : `\uB3C4\uCC29\xB7\uCC28\uB7C9 \uC704\uCE58\uB97C \uC800\uC7A5\uD588\uC2B5\uB2C8\uB2E4. \uC774\uBC88 \uD3F4\uB9C1\uC5D0\uC11C \uD1B5\uACFC \uC0AC\uAC74 ${passages}\uAC74\uC744 \uC7AC\uAD6C\uC131\uD588\uC2B5\uB2C8\uB2E4.`);
      await loadLive(leg);
    } catch (error) {
      setLiveError(error.message || "\uD604\uC7AC \uC751\uB2F5\uC744 \uC800\uC7A5\uD558\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.");
      setLiveLoading(false);
    }
  }
  async function loadPassageCoverage(targetLegs = effectiveLegs) {
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
    if (!activeJourney || effectiveLegs.length === 0) {
      setSimulation(buildDataGapSimulation(days));
      setSimLoading(false);
      return;
    }
    if (!replayReady) {
      setSimulation(buildDataGapSimulation(days, "VERIFIED_TIMETABLE_TIMES_REQUIRED", "\uC2E4\uC81C \uC2DC\uAC04\uD45C\uC758 \uD658\uC2B9 \uC2DC\uAC01 \uADFC\uAC70\uAC00 \uD544\uC694\uD569\uB2C8\uB2E4."));
      setSimLoading(false);
      return;
    }
    if (connection.mode !== "live") {
      setSimulation(buildDataGapSimulation(days, "TAGO_LIVE_REQUIRED", "TAGO LIVE \uC5F0\uACB0\uACFC \uC2E4\uC81C \uD1B5\uACFC \uC774\uB825\uC774 \uD544\uC694\uD569\uB2C8\uB2E4."));
      setSimLoading(false);
      return;
    }
    if (effectiveLegs.some((item) => !item.apiMapped)) {
      setSimulation(buildDataGapSimulation(days, "UNMAPPED_OFFICIAL_STOP", "\uC120\uD0DD \uC5EC\uD589\uC758 \uACF5\uC2DD \uAD6C\uAC04 \uB9E4\uD551\uC774 \uD544\uC694\uD569\uB2C8\uB2E4."));
      setSimLoading(false);
      return;
    }
    try {
      const payload = await BusroApi.replay(days, effectiveLegs, activeJourney);
      const rawDays = payload.daily || payload.perDay || payload.per_day || [];
      const perDay = rawDays.map((day) => {
        const status = String(day.status || "data_gap").toLowerCase();
        return {
          date: day.date,
          success: status === "success" ? true : status === "failure" ? false : null,
          status: status === "data_gap" ? "gap" : status,
          probability: null,
          reasons: [day.reason].filter(Boolean)
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
          weakestLeg: Number(payload.summary?.failure_days || 0) > 0 ? "\uC2E4\uD328 \uC77C\uC790 \uD655\uC778" : eligibleDays > 0 ? "\uD655\uC778\uB41C \uC2E4\uD328 \uC5C6\uC74C" : "\uD310\uC815 \uBD88\uAC00",
          coverage: eventCount,
          passageEvidence: eventCount > 0,
          dataGap: eligibleDays === 0
        }
      });
    } catch {
      setSimulation(buildDataGapSimulation(days, "PASSAGE_HISTORY_REQUIRED", "\uC120\uD0DD \uC5EC\uD589\uC758 \uC2E4\uC81C \uD1B5\uACFC \uC774\uB825\uC774 \uBD80\uC871\uD569\uB2C8\uB2E4."));
    } finally {
      setSimLoading(false);
    }
  }
  useEffect(() => {
    checkConnection({}, []);
  }, []);
  useEffect(() => {
    if (tab === "live" && leg) loadLive(leg);
  }, [tab, selectedLeg, leg?.apiMapped, statusPayload]);
  useEffect(() => {
    if (tab === "simulation") loadPassageCoverage();
  }, [tab, statusPayload, mappingSummary.verified, days]);
  useEffect(() => {
    if (!activeJourney) setSimulation(buildDataGapSimulation(days));
    else if (!replayReady) setSimulation(buildDataGapSimulation(days, "VERIFIED_TIMETABLE_TIMES_REQUIRED", "\uC2E4\uC81C \uC2DC\uAC04\uD45C\uC758 \uD658\uC2B9 \uC2DC\uAC01 \uADFC\uAC70\uAC00 \uD544\uC694\uD569\uB2C8\uB2E4."));
    else setSimulation(buildDataGapSimulation(days, "REPLAY_NOT_RUN", "\uC120\uD0DD \uAE30\uAC04\uC758 \uC2E4\uC81C \uD1B5\uACFC \uC774\uB825\uC744 \uC7AC\uC0DD\uD558\uC138\uC694."));
  }, [days]);
  function changeTab(next) {
    setTab(next);
    document.querySelector(".screen-scroll")?.scrollTo({ top: 0, behavior: "smooth" });
  }
  function openJourney(candidate) {
    const nextLegs = deriveJourneyLegs(candidate);
    const nextMappings = loadMappingState(nextLegs);
    setActiveJourney(candidate);
    setMappings(nextMappings);
    setSelectedLeg(nextLegs[0]?.id || "");
    setArrivals([]);
    setHistory([]);
    setLiveError("");
    setLiveNotice("");
    setLegCoverage({ ...EMPTY_PASSAGE_COVERAGE, code: nextLegs.length ? "PASSAGE_HISTORY_UNAVAILABLE" : "JOURNEY_RIDE_LEGS_REQUIRED" });
    setPassageCoverage({ ...EMPTY_PASSAGE_COVERAGE, code: nextLegs.length ? "PASSAGE_HISTORY_UNAVAILABLE" : "JOURNEY_RIDE_LEGS_REQUIRED" });
    setSimulation(buildDataGapSimulation(days, "VERIFIED_TIMETABLE_TIMES_REQUIRED", "\uC2E4\uC81C \uC2DC\uAC04\uD45C\uC758 \uD658\uC2B9 \uC2DC\uAC01 \uADFC\uAC70\uAC00 \uD544\uC694\uD569\uB2C8\uB2E4."));
    checkConnection(nextMappings, nextLegs);
    changeTab("journey");
  }
  function updateMapping(id, field, value) {
    setMappings((current) => ({ ...current, [id]: { ...current[id], [field]: cleanIdentifier(value), state: "unmapped", code: "MAPPING_CHANGED", note: "\uBCC0\uACBD\uB41C \uC2DD\uBCC4\uC790\uB294 \uB2E4\uC2DC \uAC80\uC99D\uD574\uC57C \uD569\uB2C8\uB2E4." } }));
    setConnection((current) => current.mode === "live" ? { ...current, mode: "ready", apiMapped: false, label: "TAGO \uC5F0\uACB0 \xB7 \uB9E4\uD551 \uD544\uC694", message: "\uC2DD\uBCC4\uC790\uAC00 \uBCC0\uACBD\uB418\uC5B4 LIVE \uD45C\uC2DC\uB97C \uC911\uC9C0\uD588\uC2B5\uB2C8\uB2E4. \uC120\uD0DD \uC5EC\uD589 \uAD6C\uAC04\uC744 \uB2E4\uC2DC \uAC80\uC99D\uD558\uC138\uC694." } : current);
  }
  async function verifyMapping(id) {
    const candidate = mappings[id];
    if (!candidate?.cityCode || !candidate?.nodeId || !candidate?.routeId) return;
    persistMappingIdentifiers(mappings, activeLegs);
    setMappings((current) => ({ ...current, [id]: { ...current[id], state: "checking", note: "\uACF5\uC2DD \uC2DD\uBCC4\uC790 \uAC80\uC99D \uC911\u2026" } }));
    try {
      const latestStatus = statusPayload || await BusroApi.status();
      if (!statusPayload) setStatusPayload(latestStatus);
      const result = await BusroApi.validateMapping(latestStatus, candidate);
      const next = mergeMappingResult({ ...mappings, [id]: { ...candidate, state: "checking" } }, result, [id]);
      setMappings(next);
      await checkConnection(next, effectiveLegs);
    } catch (error) {
      setMappings((current) => ({ ...current, [id]: { ...current[id], state: "unmapped", code: error.payload?.code || "MAPPING_VALIDATION_FAILED", note: "DATA_GAP \xB7 \uC11C\uBC84 \uAC80\uC99D\uC5D0 \uC2E4\uD328\uD588\uC2B5\uB2C8\uB2E4." } }));
    }
  }
  async function saveConnection() {
    setSettingsError("");
    try {
      BusroApi.setBase(apiBase);
      persistMappingIdentifiers(mappings, activeLegs);
      await checkConnection(mappings, effectiveLegs);
    } catch (error) {
      setSettingsError(error.message || "\uC5F0\uACB0 \uC124\uC815\uC744 \uC800\uC7A5\uD558\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.");
    }
  }
  return /* @__PURE__ */ React.createElement("div", { className: "stage" }, /* @__PURE__ */ React.createElement("main", { className: `app-shell tab-${tab}`, "aria-label": "\uBC84\uC2A4\uB85C \uC787\uB2E4" }, /* @__PURE__ */ React.createElement(AppHeader, { connection, onSettings: () => setSettingsOpen(true) }), /* @__PURE__ */ React.createElement("div", { className: "screen-scroll" }, tab === "explore" && /* @__PURE__ */ React.createElement(NationwideScreen, { connection, onChooseJourney: openJourney }), tab === "live" && /* @__PURE__ */ React.createElement(LiveScreen, { journey: activeJourney, connection, legs: effectiveLegs, selectedLeg, setSelectedLeg, arrivals, history, passageCoverage: legCoverage, mappingSummary, loading: liveLoading, error: liveError, notice: liveNotice, onRefresh: () => loadLive(leg), onCollect: collectSnapshot, onExplore: () => changeTab("explore") }), tab === "simulation" && /* @__PURE__ */ React.createElement(SimulationScreen, { journey: activeJourney, replayReady, connection, simulation, days, setDays, passageCoverage, mappingSummary, loading: simLoading, onRun: runSimulation, onExplore: () => changeTab("explore") }), tab === "journey" && /* @__PURE__ */ React.createElement(JourneyScreen, { journey: activeJourney, onExplore: () => changeTab("explore") })), /* @__PURE__ */ React.createElement(BottomDock, { tab, onChange: changeTab }), /* @__PURE__ */ React.createElement(SettingsSheet, { open: settingsOpen, onClose: () => setSettingsOpen(false), apiBase, setApiBase, connection, journey: activeJourney, mappings, legs: effectiveLegs, mappingSummary, settingsError, onMappingChange: updateMapping, onVerifyMapping: verifyMapping, onReconnect: saveConnection })));
}
ReactDOM.createRoot(document.getElementById("root")).render(/* @__PURE__ */ React.createElement(App, null));
