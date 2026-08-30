const { useRef } = React;
function normalizeCity(item) {
  return { code: String(item.city_code ?? item.citycode ?? item.code ?? ""), name: String(item.city_name ?? item.cityname ?? item.name ?? "") };
}
function normalizeRoute(item) {
  return {
    ...item,
    routeId: String(item.route_id ?? item.routeid ?? ""),
    routeNo: String(item.route_no ?? item.routeno ?? item.route_name ?? ""),
    routeType: String(item.route_type ?? item.routetp ?? "\uC2DC\uB0B4\uBC84\uC2A4"),
    startName: String(item.start_node_name ?? item.startnodenm ?? "\uAE30\uC810 \uC815\uBCF4 \uC5C6\uC74C"),
    endName: String(item.end_node_name ?? item.endnodenm ?? "\uC885\uC810 \uC815\uBCF4 \uC5C6\uC74C")
  };
}
function normalizeStop(item, index = 0) {
  return {
    ...item,
    node_id: String(item.node_id ?? item.nodeid ?? item.stop_id ?? ""),
    node_name: String(item.node_name ?? item.nodenm ?? item.stop_name ?? "\uC815\uB958\uC7A5"),
    node_order: Number(item.node_order ?? item.nodeord ?? item.stop_sequence ?? index + 1),
    latitude: Number(item.latitude ?? item.gpslati ?? item.lat),
    longitude: Number(item.longitude ?? item.gpslong ?? item.lon),
    city_code: String(item.city_code ?? item.citycode ?? ""),
    city_name: String(item.city_name ?? item.cityname ?? "")
  };
}
function OSMRouteMap({ geometry, stops, positions, loading, ariaLabel = "OpenStreetMap \uAE30\uBC18 \uC804\uAD6D \uBC84\uC2A4 \uC9C0\uB3C4", badgeLabel = "OSM" }) {
  const elementRef = useRef(null);
  const mapRef = useRef(null);
  useEffect(() => {
    if (!elementRef.current || !window.BusroMap) return void 0;
    mapRef.current = BusroMap.create(elementRef.current);
    return () => {
      mapRef.current?.destroy();
      mapRef.current = null;
    };
  }, []);
  useEffect(() => {
    mapRef.current?.render({ geometry, stops, positions });
  }, [geometry, stops, positions]);
  return /* @__PURE__ */ React.createElement("div", { className: "osm-map-wrap" }, /* @__PURE__ */ React.createElement("div", { ref: elementRef, className: "osm-map", "aria-label": ariaLabel }), /* @__PURE__ */ React.createElement("span", { className: "osm-attribution-pill" }, /* @__PURE__ */ React.createElement(Icon, { name: "globe-hemisphere-east" }), " ", badgeLabel), loading && /* @__PURE__ */ React.createElement("div", { className: "map-loading" }, /* @__PURE__ */ React.createElement("span", null), /* @__PURE__ */ React.createElement("p", null, "\uACF5\uC2DD \uC815\uB958\uC7A5\uACFC \uB178\uC120 \uD615\uC0C1 \uBD88\uB7EC\uC624\uB294 \uC911")));
}
function PrecisionBadge({ geometryPayload }) {
  if (!geometryPayload) return /* @__PURE__ */ React.createElement("span", { className: "precision-badge gap" }, /* @__PURE__ */ React.createElement(Icon, { name: "warning-circle" }), " \uD615\uC0C1 \uB300\uAE30");
  const relation = geometryPayload.geometry_source === "osm_bus_relation";
  return /* @__PURE__ */ React.createElement("span", { className: `precision-badge ${relation ? "relation" : "estimate"}` }, /* @__PURE__ */ React.createElement(Icon, { name: relation ? "path" : "road-horizon" }), relation ? "OSM \uBC84\uC2A4 \uAD00\uACC4" : "\uC815\uB958\uC7A5 \uC21C\uC11C \uB3C4\uB85C \uCD94\uC815");
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
      setCities(normalized);
      setCityCode((value) => value || normalized[0]?.code || "");
    }).catch((reason) => active && setError(reason.status === 503 ? "TAGO \uC804\uAD6D \uB3C4\uC2DC \uBAA9\uB85D\uC744 \uC4F0\uB824\uBA74 \uC11C\uBC84\uC5D0 \uC778\uC99D\uD0A4\uB97C \uC5F0\uACB0\uD574\uC57C \uD569\uB2C8\uB2E4." : "\uC804\uAD6D \uB3C4\uC2DC \uBAA9\uB85D\uC744 \uBD88\uB7EC\uC624\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4."));
    return () => {
      active = false;
    };
  }, []);
  async function search(event) {
    event?.preventDefault();
    if (!cityCode) return;
    setLoading(true);
    setError("");
    setSelected(null);
    setStops([]);
    setGeometryPayload(null);
    try {
      const payload = await BusroApi.routes(cityCode, routeQuery);
      setRoutes((payload.routes || payload.items || []).map(normalizeRoute).filter((item) => item.routeId));
    } catch (reason) {
      setRoutes([]);
      setError(reason.message || "\uB178\uC120\uC744 \uAC80\uC0C9\uD558\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.");
    } finally {
      setLoading(false);
    }
  }
  async function openRoute(route) {
    setSelected(route);
    setLoading(true);
    setError("");
    setHydrationGap("");
    setGeometryPayload(null);
    setPositions([]);
    try {
      const [stopPayload, infoPayload, positionPayload] = await Promise.all([
        BusroApi.routeStops(cityCode, route.routeId),
        BusroApi.routeInfo(cityCode, route.routeId).catch(() => ({ route })),
        BusroApi.positions({ cityCode, routeId: route.routeId }).catch(() => ({ positions: [] }))
      ]);
      const normalizedStops = (stopPayload.stops || stopPayload.items || []).map(normalizeStop).filter((item) => Number.isFinite(item.latitude) && Number.isFinite(item.longitude));
      setStops(normalizedStops);
      setRouteInfo(normalizeRoute(infoPayload.route || infoPayload.item || route));
      setPositions(positionPayload.positions || []);
      try {
        await BusroApi.hydrateRoute(cityCode, route.routeId);
      } catch (reason) {
        setHydrationGap(reason.message || "\uACF5\uC2DD \uACBD\uC720 \uC21C\uC11C\uB97C \uC5EC\uD589 \uADF8\uB798\uD504\uC5D0 \uC801\uC7AC\uD558\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.");
      }
      if (normalizedStops.length >= 2) {
        try {
          setGeometryPayload(await BusroApi.routeGeometry(route.routeNo, normalizedStops));
        } catch (reason) {
          setError(`\uB178\uC120\uC740 \uCC3E\uC558\uC9C0\uB9CC OSM \uD615\uC0C1\uC744 \uB9CC\uB4E4\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4. ${reason.message || "DATA_GAP"}`);
        }
      } else setError("\uACF5\uC2DD \uACBD\uC720 \uC815\uB958\uC7A5\uC758 \uC88C\uD45C\uAC00 2\uAC1C \uBBF8\uB9CC\uC774\uB77C \uC9C0\uB3C4 \uD615\uC0C1\uC744 \uB9CC\uB4E4 \uC218 \uC5C6\uC2B5\uB2C8\uB2E4.");
    } catch (reason) {
      setStops([]);
      setError(reason.message || "\uACBD\uC720 \uC815\uB958\uC7A5\uC744 \uBD88\uB7EC\uC624\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.");
    } finally {
      setLoading(false);
    }
  }
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(OSMRouteMap, { geometry: geometryPayload?.geometry, stops, positions, loading }), /* @__PURE__ */ React.createElement(GlassCard, { className: "nation-search-card" }, /* @__PURE__ */ React.createElement("div", { className: "nation-mode-title" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "DATA ADMIN \xB7 ROUTE INSPECTOR"), /* @__PURE__ */ React.createElement("h1", null, "\uAC1C\uBCC4 \uB178\uC120", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "\uB370\uC774\uD130 \uAC80\uC99D"))), /* @__PURE__ */ React.createElement(SourceBadge, { mode: connection.mode, label: connection.label })), /* @__PURE__ */ React.createElement("form", { className: "route-search-form", onSubmit: search }, /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, "\uC9C0\uC5ED"), /* @__PURE__ */ React.createElement("select", { value: cityCode, onChange: (event) => setCityCode(event.target.value), "aria-label": "\uBC84\uC2A4 \uC9C0\uC5ED \uC120\uD0DD" }, /* @__PURE__ */ React.createElement("option", { value: "" }, "\uC9C0\uC5ED \uC120\uD0DD"), cities.map((city) => /* @__PURE__ */ React.createElement("option", { key: city.code, value: city.code }, city.name)))), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, "\uB178\uC120\uBC88\uD638"), /* @__PURE__ */ React.createElement("input", { value: routeQuery, onChange: (event) => setRouteQuery(event.target.value), placeholder: "\uC608: 601", maxLength: "24" })), /* @__PURE__ */ React.createElement("button", { type: "submit", disabled: !cityCode || loading }, /* @__PURE__ */ React.createElement(Icon, { name: "magnifying-glass" }), loading ? "\uC870\uD68C \uC911" : "\uB178\uC120 \uCC3E\uAE30")), /* @__PURE__ */ React.createElement("p", { className: "source-note" }, /* @__PURE__ */ React.createElement(Icon, { name: "database" }), " \uC9C0\uC5ED\xB7\uB178\uC120\xB7\uC815\uB958\uC7A5\uC740 TAGO \uACF5\uC2DD \uC2DD\uBCC4\uC790\uB85C \uC870\uD68C\uD569\uB2C8\uB2E4. \uC11C\uBE44\uC2A4 \uD0A4\uB294 \uC11C\uBC84\uC5D0\uB9CC \uC788\uC2B5\uB2C8\uB2E4.")), error && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "warning-circle", title: "DATA_GAP" }, error), hydrationGap && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "database", title: "\uADF8\uB798\uD504 DATA_GAP" }, hydrationGap, " \uB178\uC120 \uC9C0\uB3C4\uC640 \uC815\uB958\uC7A5 \uBCF4\uAE30\uB294 \uACC4\uC18D \uC0AC\uC6A9\uD560 \uC218 \uC788\uC2B5\uB2C8\uB2E4."), routes.length > 0 && /* @__PURE__ */ React.createElement("section", { className: "route-catalog" }, /* @__PURE__ */ React.createElement("div", { className: "catalog-heading" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "ROUTES"), /* @__PURE__ */ React.createElement("h2", null, cities.find((item) => item.code === cityCode)?.name || "\uC120\uD0DD \uC9C0\uC5ED", " \uB178\uC120")), /* @__PURE__ */ React.createElement("span", null, routes.length, "\uAC1C")), /* @__PURE__ */ React.createElement("div", { className: "route-result-list" }, routes.map((route) => /* @__PURE__ */ React.createElement("button", { type: "button", key: route.routeId, className: selected?.routeId === route.routeId ? "active" : "", onClick: () => openRoute(route) }, /* @__PURE__ */ React.createElement("span", { className: "route-number" }, route.routeNo || "\u2014"), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("strong", null, route.startName, " \u2192 ", route.endName), /* @__PURE__ */ React.createElement("small", null, route.routeType, " \xB7 ID ", route.routeId)), /* @__PURE__ */ React.createElement(Icon, { name: "caret-right" }))))), selected && /* @__PURE__ */ React.createElement(GlassCard, { className: "selected-route-card" }, /* @__PURE__ */ React.createElement("div", { className: "selected-route-head" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "SELECTED LINE"), /* @__PURE__ */ React.createElement("h2", null, /* @__PURE__ */ React.createElement("span", null, selected.routeNo), routeInfo?.routeType || selected.routeType)), /* @__PURE__ */ React.createElement(PrecisionBadge, { geometryPayload })), /* @__PURE__ */ React.createElement("div", { className: "route-terminal-row" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "\uAE30\uC810"), /* @__PURE__ */ React.createElement("strong", null, routeInfo?.startName || selected.startName), /* @__PURE__ */ React.createElement("span", null, routeInfo?.first_vehicle_time || "\uC2DC\uAC04\uD45C \uCD9C\uCC98 \uD655\uC778 \uD544\uC694")), /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" }), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "\uC885\uC810"), /* @__PURE__ */ React.createElement("strong", null, routeInfo?.endName || selected.endName), /* @__PURE__ */ React.createElement("span", null, routeInfo?.last_vehicle_time || "\uC2DC\uAC04\uD45C \uCD9C\uCC98 \uD655\uC778 \uD544\uC694"))), /* @__PURE__ */ React.createElement("div", { className: "route-evidence-row" }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "map-pin" }), " \uACBD\uC720 ", stops.length, "\uAC1C"), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "bus" }), " \uD604\uC7AC \uCC28\uB7C9 ", positions.length, "\uB300"), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), " \uD3C9\uC77C \uBC30\uCC28 ", routeInfo?.weekday_interval_minutes || "\u2014", "\uBD84")), geometryPayload?.data_gap && /* @__PURE__ */ React.createElement("p", { className: "geometry-caveat" }, geometryPayload.data_gap), /* @__PURE__ */ React.createElement("div", { className: "stop-preview-list" }, stops.slice(0, 8).map((stop, index) => /* @__PURE__ */ React.createElement("button", { type: "button", key: `${stop.node_id}-${index}`, onClick: () => onUseStop(stop) }, /* @__PURE__ */ React.createElement("span", null, stop.node_order || index + 1), /* @__PURE__ */ React.createElement("strong", null, stop.node_name), /* @__PURE__ */ React.createElement("small", null, stop.node_id)))), stops.length > 8 && /* @__PURE__ */ React.createElement("p", { className: "more-stops" }, "\uC678 ", stops.length - 8, "\uAC1C \uC815\uB958\uC7A5 \xB7 \uC9C0\uB3C4\uC5D0\uC11C \uC804\uCCB4 \uD655\uC778")));
}
function StopLookup({ label, value, onChange, selected, onSelect, cityCode }) {
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const query = String(value || "").trim();
    if (selected || query.length < 2) {
      setResults([]);
      setLoading(false);
      return void 0;
    }
    let active = true;
    const timer = window.setTimeout(async () => {
      setLoading(true);
      try {
        const payload = await BusroApi.searchStops(query, cityCode);
        if (active) setResults((payload.stops || payload.items || []).map(normalizeStop).slice(0, 8));
      } catch {
        if (active) setResults([]);
      } finally {
        if (active) setLoading(false);
      }
    }, 260);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [value, selected, cityCode]);
  return /* @__PURE__ */ React.createElement("div", { className: "stop-lookup" }, /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, label), /* @__PURE__ */ React.createElement("div", { className: "stop-input-shell" }, /* @__PURE__ */ React.createElement(Icon, { name: "map-pin" }), /* @__PURE__ */ React.createElement("input", { value: selected ? selected.node_name : value, onChange: (event) => {
    onSelect(null);
    onChange(event.target.value);
  }, placeholder: "\uC804\uAD6D \uC815\uB958\uC7A5\uBA85 2\uC790 \uC774\uC0C1", autoComplete: "off" }), /* @__PURE__ */ React.createElement("span", { className: loading ? "lookup-state loading" : "lookup-state" }, /* @__PURE__ */ React.createElement(Icon, { name: loading ? "spinner-gap" : selected ? "check-circle" : "magnifying-glass" })))), results.length > 0 && !selected && /* @__PURE__ */ React.createElement("div", { className: "stop-suggestions" }, results.map((stop, index) => /* @__PURE__ */ React.createElement("button", { type: "button", key: `${stop.city_code}-${stop.node_id}-${index}`, onClick: () => {
    onSelect(stop);
    onChange(stop.node_name);
    setResults([]);
  } }, /* @__PURE__ */ React.createElement("strong", null, stop.node_name), /* @__PURE__ */ React.createElement("small", null, /* @__PURE__ */ React.createElement("span", null, stop.city_name || stop.city_code || "\uC9C0\uC5ED \uBBF8\uC0C1", " \xB7 ", stop.node_id), /* @__PURE__ */ React.createElement("em", { className: stop.graph_ready ? "graph-ready" : "graph-gap" }, stop.graph_ready ? "\uC5EC\uD589 \uACBD\uB85C \uC5F0\uACB0" : "\uC815\uB958\uC7A5 \uC815\uBCF4\uB9CC"))))));
}
function formatCount(value) {
  return Number.isFinite(Number(value)) ? Number(value).toLocaleString("ko-KR") : "\u2014";
}
function localDateValue(date = /* @__PURE__ */ new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}
function localTimeValue(date = /* @__PURE__ */ new Date()) {
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}
function gtfsClockMinutes(value) {
  const clean = String(value ?? "").trim();
  const matched = clean.match(/^(\d{1,3}):([0-5]\d)(?::[0-5]\d)?$/);
  return matched ? Number(matched[1]) * 60 + Number(matched[2]) : null;
}
function formatGtfsClock(value, secondsValue) {
  let totalMinutes = gtfsClockMinutes(value);
  if (totalMinutes === null && secondsValue !== null && secondsValue !== void 0 && String(secondsValue).trim() !== "" && Number.isFinite(Number(secondsValue))) totalMinutes = Math.floor(Number(secondsValue) / 60);
  if (!Number.isFinite(totalMinutes) || totalMinutes < 0) return null;
  const dayOffset = Math.floor(totalMinutes / 1440);
  const minuteOfDay = totalMinutes % 1440;
  const hour = String(Math.floor(minuteOfDay / 60)).padStart(2, "0");
  const minute = String(minuteOfDay % 60).padStart(2, "0");
  return `${hour}:${minute}${dayOffset ? ` (+${dayOffset}\uC77C)` : ""}`;
}
function replayClock(value, minutesValue) {
  const fromRaw = gtfsClockMinutes(value);
  const minutes = fromRaw ?? (minutesValue === null || minutesValue === void 0 || String(minutesValue).trim() === "" ? NaN : Number(minutesValue));
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
    feedId: schedule.feed_id || result?.schedule_feed_id || ""
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
  const basisLabel = basis === "OFFICIAL_STATIC_GTFS_RAW_EVIDENCE" ? "\uACF5\uC2DD \uC815\uC801 GTFS \uC6D0\uBCF8 \uADFC\uAC70" : String(basis || "").replaceAll("_", " ");
  return {
    ready: Boolean(provider || feedId || basis),
    label: [provider ? `${provider}${official ? " \uACF5\uC2DD GTFS" : " GTFS"}` : "", feedId, basisLabel].filter(Boolean).join(" \xB7 ")
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
    const scheduledSeconds = replayRow.scheduled_minutes === null || replayRow.scheduled_minutes === void 0 ? void 0 : Number(replayRow.scheduled_minutes) * 60;
    const nextDepartureSeconds = replayRow.next_departure_minutes === null || replayRow.next_departure_minutes === void 0 ? void 0 : Number(replayRow.next_departure_minutes) * 60;
    leg.arrivalTime ||= formatGtfsClock(replayRow.scheduled_arrival, scheduledSeconds);
    leg.nextDepartureTime = formatGtfsClock(replayRow.next_departure, nextDepartureSeconds);
  });
  return legs;
}
function prepareJourneyForDetail(candidate, context) {
  const replayLegs = (Array.isArray(candidate?.replay_legs) ? candidate.replay_legs : []).map((row) => {
    const sourceId = typeof row.time_evidence_source === "object" ? String(row.time_evidence_source.source_id || "") : String(row.time_evidence_source || "");
    const scheduledMinutes = gtfsClockMinutes(row.scheduled_arrival) ?? (row.scheduled_minutes !== null && row.scheduled_minutes !== void 0 && Number.isFinite(Number(row.scheduled_minutes)) ? Number(row.scheduled_minutes) : null);
    const nextDepartureMinutes = gtfsClockMinutes(row.next_departure) ?? (row.next_departure_minutes !== null && row.next_departure_minutes !== void 0 && Number.isFinite(Number(row.next_departure_minutes)) ? Number(row.next_departure_minutes) : null);
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
      next_time_evidence_feed_id: String(row.next_time_evidence_feed_id || "")
    };
  });
  return { ...candidate, ...context, replay_legs: replayLegs };
}
const JOURNEY_CRITERION_LABELS = {
  minimum_transfers: "\uCD5C\uC18C \uD658\uC2B9",
  generalized_cost: "\uADE0\uD615 \uACBD\uB85C",
  explorer: "\uD0D0\uD5D8 \uACBD\uB85C",
  earliest_arrival: "\uAC00\uC7A5 \uC774\uB978 \uB3C4\uCC29"
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
  const timeSummary = [departureTime ? `\uCD9C\uBC1C ${departureTime}` : "", arrivalTime ? `\uB3C4\uCC29 ${arrivalTime}` : "", Number.isFinite(minutes) ? `${Math.max(0, Math.round(minutes))}\uBD84` : ""].filter(Boolean);
  return /* @__PURE__ */ React.createElement("article", { className: structural ? "structural-candidate" : "scheduled-candidate" }, /* @__PURE__ */ React.createElement("div", { className: "candidate-rank" }, index + 1), /* @__PURE__ */ React.createElement("div", { className: "candidate-copy" }, /* @__PURE__ */ React.createElement("div", { className: "candidate-title" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", null, JOURNEY_CRITERION_LABELS[candidate?.criterion] || candidate?.criterion || (structural ? "\uBC29\uD5A5 \uACBD\uB85C \uD6C4\uBCF4" : "\uC2DC\uAC04\uD45C \uACBD\uB85C")), /* @__PURE__ */ React.createElement("h3", null, routeIds.length > 0 ? `${candidate?.transfers || 0}\uD68C \uD658\uC2B9 \xB7 ${routeIds.length}\uAC1C \uB178\uC120` : "\uB178\uC120 DATA_GAP")), /* @__PURE__ */ React.createElement("small", { className: structural ? "schedule-gap" : "schedule-ready" }, structural ? "\uC2DC\uAC04 \uBBF8\uAC80\uC99D" : "\uC2DC\uAC04\uD45C \uD655\uC778")), !structural && timeSummary.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "schedule-summary" }, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), /* @__PURE__ */ React.createElement("strong", null, timeSummary.join(" \xB7 "))), structural && /* @__PURE__ */ React.createElement("div", { className: "schedule-gap-copy" }, /* @__PURE__ */ React.createElement(Icon, { name: "warning-circle" }), /* @__PURE__ */ React.createElement("span", null, "\uC815\uB958\uC7A5 \uC9C4\uD589 \uBC29\uD5A5\uB9CC \uD655\uC778\uD588\uC2B5\uB2C8\uB2E4. \uC774 \uB0A0\uC9DC\xB7\uC2DC\uAC01\uC5D0 \uC2E4\uC81C \uC6B4\uD589 \uAC00\uB2A5\uD55C \uACBD\uB85C\uB85C \uD655\uC815\uB418\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.")), /* @__PURE__ */ React.createElement("div", { className: "candidate-leg-list" }, legs.map((leg, legIndex) => /* @__PURE__ */ React.createElement("div", { className: "candidate-leg", key: `${leg.routeId}-${leg.tripId}-${legIndex}` }, /* @__PURE__ */ React.createElement("span", { className: "timeline-rail" }, /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("b", null)), /* @__PURE__ */ React.createElement("div", { className: "leg-copy" }, /* @__PURE__ */ React.createElement("span", { className: "route-pill" }, /* @__PURE__ */ React.createElement(Icon, { name: "bus" }), " ", leg.routeId), /* @__PURE__ */ React.createElement("strong", null, leg.from?.node_name || leg.from?.node_id || "\uC2B9\uCC28 \uC815\uB958\uC7A5"), !structural && leg.departureTime && /* @__PURE__ */ React.createElement("span", { className: "leg-time" }, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), " ", leg.departureTime, " \uCD9C\uBC1C"), /* @__PURE__ */ React.createElement("small", null, "\uCD1D ", leg.edgeCount + 1, "\uAC1C \uC815\uB958\uC7A5"), /* @__PURE__ */ React.createElement("strong", null, leg.to?.node_name || leg.to?.node_id || "\uD558\uCC28 \uC815\uB958\uC7A5"), !structural && leg.arrivalTime && /* @__PURE__ */ React.createElement("span", { className: "leg-time arrival" }, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), " ", leg.arrivalTime, " \uB3C4\uCC29"), !structural && leg.nextDepartureTime && /* @__PURE__ */ React.createElement("span", { className: "transfer-time" }, "\uB2E4\uC74C \uBC84\uC2A4 ", leg.nextDepartureTime, " \uCD9C\uBC1C"))))), /* @__PURE__ */ React.createElement("footer", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "arrows-left-right" }), " ", typeof candidate?.transfers === "number" ? `${candidate.transfers}\uD68C \uD658\uC2B9` : "\uD658\uC2B9 DATA_GAP"), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "database" }), " \uC2B9\uCC28 ", evidence.ride_edges ?? "\u2014", " \xB7 \uD658\uC2B9 ", evidence.transfer_edges ?? "\u2014", " \uAC04\uC120"), /* @__PURE__ */ React.createElement("strong", null, hasProbability ? `\uC131\uACF5\uB960 ${Math.round(candidate.success_probability * 100)}%` : "\uC131\uACF5\uB960 DATA_GAP")), !structural && /* @__PURE__ */ React.createElement("small", { className: provenance.ready ? "official-schedule-evidence" : "schedule-evidence-gap" }, /* @__PURE__ */ React.createElement(Icon, { name: provenance.ready ? "shield-check" : "warning-circle" }), " ", provenance.ready ? provenance.label : "\uC2DC\uAC04\uD45C \uCD9C\uCC98 DATA_GAP"), typeof coverage.schedule_routes === "number" && typeof coverage.total_routes === "number" && /* @__PURE__ */ React.createElement("small", { className: "evidence-copy" }, "\uC2DC\uAC04\uD45C \uADFC\uAC70 ", coverage.schedule_routes, "/", coverage.total_routes, typeof coverage.passage_routes === "number" ? ` \xB7 \uD1B5\uACFC \uC774\uB825 ${coverage.passage_routes}/${coverage.total_routes}` : ""), /* @__PURE__ */ React.createElement("button", { className: structural ? "open-candidate structural" : "open-candidate", type: "button", onClick: () => onChooseJourney?.(prepareJourneyForDetail(candidate, context)) }, structural ? "\uC815\uB958\uC7A5 \uC21C\uC11C \uBCF4\uAE30" : "\uC2DC\uAC04\uD45C \uACBD\uB85C \uC790\uC138\uD788 \uBCF4\uAE30", " ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" }))));
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
  const scheduleSearchState = graph?.search_complete === true ? "\uAC80\uC0C9 \uC644\uB8CC" : graph?.search_complete === false ? "\uAC80\uC0C9 \uBBF8\uC644\uB8CC" : "\uC644\uB8CC \uC0C1\uD0DC DATA_GAP";
  const scheduleDetailReason = graph?.detail_reason || result?.schedule?.detail_reason || "";
  const primaryStatus = nationwideComplete ? "\uC804\uAD6D \uACBD\uB85C\uB9DD \uC5F0\uACB0\uB428" : graphReady ? "\uACF5\uC2DD \uAC80\uC99D \uAD6C\uAC04 \uC5F0\uACB0\uB428" : "\uC804\uAD6D \uACBD\uB85C\uB9DD \uC900\uBE44 \uC911";
  const catalogSummary = stopRows && routeRows ? `\uC815\uB958\uC7A5 ${formatCount(stopRows)} \xB7 \uB178\uC120 ${formatCount(routeRows)}` : "\uC804\uAD6D \uBAA9\uB85D DATA_GAP";
  const topologySummary = activeRoutes ? `\uBC29\uD5A5 \uB178\uC120 ${formatCount(activeRoutes)} \xB7 \uADF8\uB798\uD504 \uC815\uB958\uC7A5 ${formatCount(activeStops)}` : topologyTargets ? `\uBC29\uD5A5 \uC21C\uC11C ${formatCount(topologyComplete)}/${formatCount(topologyTargets)}` : "\uBC29\uD5A5 \uC21C\uC11C DATA_GAP";
  return /* @__PURE__ */ React.createElement("div", { className: `graph-coverage ${graphReady ? "catalog-ready" : "catalog-gap"}` }, /* @__PURE__ */ React.createElement("span", { className: "graph-pulse", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("strong", null, primaryStatus), /* @__PURE__ */ React.createElement("small", null, catalogSummary, " \xB7 ", topologySummary)), /* @__PURE__ */ React.createElement("span", { className: "graph-method" }, scheduleGraph ? "\uC2DC\uAC04\uC758\uC874 Dijkstra" : "\uB2E8\uBC29\uD5A5 Dijkstra"), graphReady && !nationwideComplete && /* @__PURE__ */ React.createElement("small", { className: "coverage-query" }, "\uACF5\uC2DD \uACBD\uC720 \uC21C\uC11C\uAC00 \uC5F0\uACB0\uB41C ", formatCount(activeCities), "\uAC1C \uC9C0\uC5ED\uBD80\uD130 \uC2E4\uC81C \uBC29\uD5A5\uC73C\uB85C \uAC80\uC0C9\uD569\uB2C8\uB2E4. \uC804\uAD6D \uD655\uB300 \uC911\uC785\uB2C8\uB2E4."), !graphReady && /* @__PURE__ */ React.createElement("small", { className: "coverage-gap" }, "TAGO \uB178\uC120\uBCC4 \uACBD\uC720 \uC21C\uC11C\uC758 \uC804\uAD6D \uC801\uC7AC\uAC00 \uB05D\uB098\uC9C0 \uC54A\uC544, \uD655\uC778\uB41C \uAD6C\uAC04\uB9CC \uAC80\uC0C9\uD569\uB2C8\uB2E4."), scheduleGraph && /* @__PURE__ */ React.createElement("small", { className: schedule.ready ? "coverage-query" : "coverage-gap" }, "\uC774\uBC88 \uC77C\uC815 \uAC80\uC0C9: ", formatCount(graph.expanded_stops), "\uAC1C \uC815\uB958\uC7A5 \uD655\uC7A5 \xB7 ", formatCount(graph.departures_scanned), "\uAC1C \uCD9C\uBC1C\uD3B8 \uD655\uC778 \xB7 ", scheduleSearchState, " \xB7 ", graph.algorithm), scheduleGraph && scheduleDetailReason && /* @__PURE__ */ React.createElement("small", { className: "coverage-gap" }, "\uC2DC\uAC04\uD45C \uC0C1\uC138: ", scheduleDetailReason), staticAlternativeCount > 0 && /* @__PURE__ */ React.createElement("small", { className: "coverage-query" }, "\uBC29\uD5A5 \uAD6C\uC870 \uD6C4\uBCF4 ", formatCount(staticAlternativeCount), "\uAC74 \uD655\uC778 \xB7 \uC2DC\uAC04\uD45C \uC6B4\uD589 \uAC00\uB2A5\uC131 \uBBF8\uD655\uC815"), graph && !scheduleGraph && /* @__PURE__ */ React.createElement("small", { className: topologyReady ? "coverage-query" : "coverage-gap" }, "\uC774\uBC88 \uAC80\uC0C9: ", formatCount(graph.nodes), "\uAC1C \uC0C1\uD0DC \xB7 ", formatCount(graph.edges), "\uAC1C \uC2B9\uCC28 \uAC04\uC120 \xB7 ", graph.algorithm || "directed_dijkstra"), graph && !scheduleGraph && !topologyReady && staticAlternativeCount === 0 && /* @__PURE__ */ React.createElement("small", { className: "coverage-gap" }, "DATA_GAP \xB7 \uAC80\uC0C9 \uAC00\uB2A5\uD55C \uAC80\uC99D \uB178\uC120 \uC21C\uC11C\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4."));
}
function JourneyGenerator({ seededStop, onChooseJourney, connection }) {
  const [fromText, setFromText] = useState("");
  const [toText, setToText] = useState("");
  const [fromStop, setFromStop] = useState(null);
  const [toStop, setToStop] = useState(null);
  const [preference, setPreference] = useState("diverse");
  const [result, setResult] = useState(null);
  const [serviceDate, setServiceDate] = useState(() => localDateValue());
  const [departureTime, setDepartureTime] = useState(() => localTimeValue());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [networkStatus, setNetworkStatus] = useState(null);
  useEffect(() => {
    if (seededStop) {
      if (!fromStop) {
        setFromStop(seededStop);
        setFromText(seededStop.node_name);
      } else {
        setToStop(seededStop);
        setToText(seededStop.node_name);
      }
    }
  }, [seededStop]);
  useEffect(() => {
    let active = true;
    BusroApi.networkStatus().then((payload) => active && setNetworkStatus(payload)).catch(() => active && setNetworkStatus({ ready: false, sources: [] }));
    return () => {
      active = false;
    };
  }, []);
  async function generate(event) {
    event.preventDefault();
    if (!fromStop || !toStop) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      setResult(await BusroApi.generateJourneys({ from_stop_id: fromStop.node_id, to_stop_id: toStop.node_id, from_city_code: fromStop.city_code || void 0, to_city_code: toStop.city_code || void 0, service_date: serviceDate, departure_time: departureTime, preference, max_alternatives: 3 }));
    } catch (reason) {
      setError(reason.message || "\uD604\uC7AC \uC801\uC7AC\uB41C \uB178\uC120 \uADF8\uB798\uD504\uB85C \uC5EC\uD589\uC744 \uB9CC\uB4E4\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.");
    } finally {
      setLoading(false);
    }
  }
  const schedule = normalizeSchedule(result);
  const candidateRows = result?.candidates || result?.journeys || result?.alternatives || [];
  const returnedCandidates = Array.isArray(candidateRows) ? candidateRows : [];
  const scheduled = schedule.ready ? returnedCandidates.filter((candidate) => candidate?.scheduled !== false) : [];
  const staticRows = Array.isArray(result?.static_alternatives) ? result.static_alternatives : [];
  const structuralCandidates = [...staticRows, ...returnedCandidates.filter((candidate) => !scheduled.includes(candidate) && !staticRows.includes(candidate))];
  const journeyContext = { from_stop: fromStop, to_stop: toStop, preference, service_date: schedule.serviceDate || serviceDate, departure_time: schedule.departureTime || departureTime, schedule: result?.schedule || { status: schedule.status, reason: schedule.reason } };
  const gapReasons = {
    STOP_NOT_IN_HYDRATED_SEQUENCE: "\uC120\uD0DD\uD55C \uC815\uB958\uC7A5\uC740 \uC804\uAD6D \uBAA9\uB85D\uC5D0 \uC788\uC9C0\uB9CC \uAC80\uC99D\uB41C \uB178\uC120 \uC21C\uC11C \uADF8\uB798\uD504\uC5D0\uB294 \uC544\uC9C1 \uD3EC\uD568\uB418\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.",
    NO_DIRECTED_PATH_IN_HYDRATED_GRAPH: "\uD604\uC7AC \uAC80\uC99D \uADF8\uB798\uD504\uC5D0\uC11C \uCD9C\uBC1C \uBC29\uD5A5\uBD80\uD130 \uB3C4\uCC29 \uBC29\uD5A5\uAE4C\uC9C0 \uC774\uC5B4\uC9C0\uB294 \uACBD\uB85C\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4. \uC5ED\uBC29\uD5A5 \uAC04\uC120\uC744 \uC784\uC758\uB85C \uB9CC\uB4E4\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.",
    EVIDENCE_INCOMPLETE: "\uACBD\uB85C \uAD6C\uC870\uB294 \uCC3E\uC558\uC9C0\uB9CC \uC2DC\uAC04\uD45C \uB610\uB294 \uD1B5\uACFC \uC774\uB825\uC774 \uBD80\uC871\uD569\uB2C8\uB2E4.",
    SCHEDULE_DATA_GAP: "\uC120\uD0DD\uD55C \uB0A0\uC9DC\xB7\uCD9C\uBC1C \uC2DC\uAC01\uC5D0 \uC801\uC6A9\uD560 \uACF5\uC2DD GTFS \uC6B4\uD589 \uAE30\uB85D\uC774 \uC5C6\uC2B5\uB2C8\uB2E4. \uC544\uB798 \uAD6C\uC870 \uD6C4\uBCF4\uAC00 \uC788\uB354\uB77C\uB3C4 \uC2E4\uC81C \uC6B4\uD589 \uAC00\uB2A5 \uACBD\uB85C\uB85C \uD655\uC815\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4."
  };
  return /* @__PURE__ */ React.createElement("section", { className: "journey-generator" }, /* @__PURE__ */ React.createElement(GlassCard, { className: "generator-card" }, /* @__PURE__ */ React.createElement("div", { className: "generator-heading" }, /* @__PURE__ */ React.createElement("div", { className: "generator-kicker" }, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uC804\uAD6D \uBC84\uC2A4 \uC5EC\uD589"), /* @__PURE__ */ React.createElement(SourceBadge, { mode: connection.mode, label: connection.label })), /* @__PURE__ */ React.createElement("h1", null, "\uC5B4\uB514\uAE4C\uC9C0 \uAC00\uC138\uC694?"), /* @__PURE__ */ React.createElement("p", null, "\uCD9C\uBC1C\uC9C0\uC640 \uB3C4\uCC29\uC9C0, \uB5A0\uB0A0 \uB54C\uB97C \uACE0\uB974\uBA74 \uC2E4\uC81C \uC9C4\uD589 \uBC29\uD5A5\uACFC \uACF5\uC2DD \uC2DC\uAC04\uD45C\uB97C \uD568\uAED8 \uD655\uC778\uD574\uC694.")), /* @__PURE__ */ React.createElement("form", { onSubmit: generate }, /* @__PURE__ */ React.createElement("div", { className: "route-point-sheet" }, /* @__PURE__ */ React.createElement("div", { className: "route-point origin" }, /* @__PURE__ */ React.createElement("span", { className: "point-mark", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement(StopLookup, { label: "\uCD9C\uBC1C", value: fromText, onChange: setFromText, selected: fromStop, onSelect: setFromStop })), /* @__PURE__ */ React.createElement("div", { className: "route-point destination" }, /* @__PURE__ */ React.createElement("span", { className: "point-mark", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement(StopLookup, { label: "\uB3C4\uCC29", value: toText, onChange: setToText, selected: toStop, onSelect: setToStop })), /* @__PURE__ */ React.createElement("button", { className: "generator-swap", type: "button", onClick: () => {
    setFromStop(toStop);
    setToStop(fromStop);
    setFromText(toText);
    setToText(fromText);
  }, "aria-label": "\uCD9C\uBC1C\uACFC \uB3C4\uCC29 \uBC14\uAFB8\uAE30" }, /* @__PURE__ */ React.createElement(Icon, { name: "arrows-down-up" }))), /* @__PURE__ */ React.createElement("fieldset", { className: "schedule-fieldset" }, /* @__PURE__ */ React.createElement("legend", null, "\uC5B8\uC81C \uB5A0\uB0A0\uAE4C\uC694?"), /* @__PURE__ */ React.createElement("div", { className: "schedule-input-grid" }, /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "calendar-blank" }), " \uC5EC\uD589 \uB0A0\uC9DC"), /* @__PURE__ */ React.createElement("input", { type: "date", value: serviceDate, onChange: (event) => setServiceDate(event.target.value), required: true })), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), " \uCD9C\uBC1C \uC2DC\uAC01"), /* @__PURE__ */ React.createElement("input", { type: "time", value: departureTime, onChange: (event) => setDepartureTime(event.target.value), step: "60", required: true }))), /* @__PURE__ */ React.createElement("small", null, "\uC120\uD0DD\uD55C \uB0A0\uC9DC\uC758 \uACF5\uC2DD GTFS \uC6B4\uD589 \uAE30\uB85D\uB9CC \uC2DC\uAC04 \uAC00\uB2A5 \uACBD\uB85C\uB85C \uD45C\uC2DC\uD569\uB2C8\uB2E4.")), /* @__PURE__ */ React.createElement("fieldset", null, /* @__PURE__ */ React.createElement("legend", null, "\uC5B4\uB5A4 \uAE38\uB85C \uAC08\uAE4C\uC694?"), /* @__PURE__ */ React.createElement("div", { className: "preference-grid" }, [["diverse", "\uCD94\uCC9C", "sparkle"], ["low_transfer", "\uCD5C\uC18C \uD658\uC2B9", "arrows-left-right"], ["reliable", "\uADFC\uAC70 \uC6B0\uC120", "shield-check"], ["challenge", "\uAD6D\uD1A0\uC885\uC8FC", "flag-banner"]].map(([value, label, icon]) => /* @__PURE__ */ React.createElement("button", { type: "button", key: value, className: preference === value ? "active" : "", onClick: () => setPreference(value) }, /* @__PURE__ */ React.createElement(Icon, { name: icon }), label)))), /* @__PURE__ */ React.createElement("button", { className: "liquid-button route-search-primary", type: "submit", disabled: !fromStop || !toStop || !serviceDate || !departureTime || loading }, loading ? "\uACF5\uC2DD \uC2DC\uAC04\uD45C\uC5D0\uC11C \uCC3E\uB294 \uC911\u2026" : "\uC2DC\uAC04\uD45C \uACBD\uB85C \uCC3E\uAE30", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" })), (!fromStop || !toStop) && /* @__PURE__ */ React.createElement("small", { className: "search-help" }, "\uC815\uB958\uC7A5\uBA85\uC744 \uC785\uB825\uD558\uACE0 \uC804\uAD6D \uBAA9\uB85D\uC5D0\uC11C \uCD9C\uBC1C\xB7\uB3C4\uCC29\uC744 \uAC01\uAC01 \uC120\uD0DD\uD558\uC138\uC694."))), /* @__PURE__ */ React.createElement(GraphCoverage, { networkStatus, result }), error && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "warning-circle", title: "DATA_GAP" }, error, " \uAC80\uC99D\uB41C \uB178\uC120 \uACBD\uC720 \uC815\uB958\uC7A5\uC774 \uC801\uC7AC\uB418\uC5B4\uC57C \uACBD\uB85C\uC5D0 \uD3EC\uD568\uB429\uB2C8\uB2E4."), result && !schedule.ready && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "warning-circle", title: "SCHEDULE_DATA_GAP" }, gapReasons[schedule.reason] || gapReasons[result.reason] || schedule.reason || "\uC120\uD0DD\uD55C \uC77C\uC815\uC758 \uACF5\uC2DD \uC2DC\uAC04\uD45C \uADFC\uAC70\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4."), result && schedule.ready && scheduled.length === 0 && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "warning-circle", title: result.status || "SCHEDULE_DATA_GAP" }, gapReasons[result.reason] || result.reason || "\uC120\uD0DD\uD55C \uC77C\uC815\uC5D0 \uCD9C\uBC1C \uAC00\uB2A5\uD55C \uACF5\uC2DD \uC2DC\uAC04\uD45C \uACBD\uB85C\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4."), scheduled.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "generated-journeys scheduled-results" }, /* @__PURE__ */ React.createElement("div", { className: "catalog-heading" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uD655\uC778\uB41C \uC2DC\uAC04\uD45C \uACBD\uB85C"), /* @__PURE__ */ React.createElement("h2", null, scheduled.length, "\uAC00\uC9C0 \uAE38\uC744 \uD655\uC778\uD588\uC5B4\uC694")), /* @__PURE__ */ React.createElement("span", null, schedule.serviceDate || serviceDate, " \xB7 ", schedule.departureTime || departureTime)), /* @__PURE__ */ React.createElement("p", { className: "alternative-hint" }, "\uCD9C\uBC1C\xB7\uB3C4\uCC29 \uC2DC\uAC01\uC740 \uD45C\uC2DC\uB41C GTFS \uC6D0\uBCF8 \uADFC\uAC70\uB97C \uB530\uB985\uB2C8\uB2E4. \uC131\uACF5\uB960\uC740 \uBCC4\uB3C4 \uD1B5\uACFC \uC774\uB825\uC774 \uC788\uC744 \uB54C\uB9CC \uACC4\uC0B0\uD569\uB2C8\uB2E4."), scheduled.map((candidate, index) => /* @__PURE__ */ React.createElement(JourneyCandidateCard, { key: `scheduled-${candidate.id || candidate.criterion || "candidate"}-${index}`, candidate, index, schedule, context: journeyContext, onChooseJourney }))), structuralCandidates.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "generated-journeys structural-results" }, /* @__PURE__ */ React.createElement("div", { className: "catalog-heading" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uBC29\uD5A5 \uAD6C\uC870 \uD6C4\uBCF4"), /* @__PURE__ */ React.createElement("h2", null, "\uC2DC\uAC04\uD45C \uD655\uC778 \uC804 \uACBD\uB85C")), /* @__PURE__ */ React.createElement("span", null, "\uC6B4\uD589 \uAC00\uB2A5\uC131 \uBBF8\uD655\uC815")), /* @__PURE__ */ React.createElement("p", { className: "alternative-hint warning" }, "\uB2E8\uBC29\uD5A5 \uC815\uB958\uC7A5 \uC21C\uC11C\uB9CC \uC5F0\uACB0\uB41C \uACB0\uACFC\uC785\uB2C8\uB2E4. \uC120\uD0DD\uD55C \uC77C\uC815\uC758 \uC2E4\uC81C \uBC84\uC2A4\uAC00 \uC788\uB2E4\uACE0 \uD574\uC11D\uD558\uBA74 \uC548 \uB429\uB2C8\uB2E4."), structuralCandidates.map((candidate, index) => /* @__PURE__ */ React.createElement(JourneyCandidateCard, { key: `structural-${candidate.id || candidate.criterion || "candidate"}-${index}`, candidate, index, schedule, structural: true, context: journeyContext, onChooseJourney }))));
}
function NationwideScreen({ connection, onChooseJourney }) {
  const [seededStop, setSeededStop] = useState(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  return /* @__PURE__ */ React.createElement("main", { className: "screen nationwide-screen" }, /* @__PURE__ */ React.createElement(JourneyGenerator, { seededStop, onChooseJourney, connection }), /* @__PURE__ */ React.createElement("details", { className: "route-admin-tools", open: toolsOpen, onToggle: (event) => setToolsOpen(event.currentTarget.open) }, /* @__PURE__ */ React.createElement("summary", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "wrench" }), /* @__PURE__ */ React.createElement("strong", null, "\uB178\uC120 \uB370\uC774\uD130 \uB3C4\uAD6C"), /* @__PURE__ */ React.createElement("small", null, "\uC6B4\uC601\xB7\uAC80\uC99D\uC6A9")), /* @__PURE__ */ React.createElement(Icon, { name: "caret-down" })), toolsOpen && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "route-admin-intro" }, /* @__PURE__ */ React.createElement("p", null, "\uAC1C\uBCC4 TAGO \uB178\uC120 \uC870\uD68C\xB7OSM \uD615\uC0C1\xB7\uACBD\uC720\uC21C\uC11C \uC801\uC7AC\uB294 \uB370\uC774\uD130 \uC810\uAC80\uC6A9\uC785\uB2C8\uB2E4. \uC5EC\uD589\uC790\uB294 \uC704 \uC804\uAD6D \uACBD\uB85C \uAC80\uC0C9\uB9CC \uC0AC\uC6A9\uD558\uBA74 \uB429\uB2C8\uB2E4.")), /* @__PURE__ */ React.createElement(RouteBrowser, { connection, onUseStop: setSeededStop }))));
}
