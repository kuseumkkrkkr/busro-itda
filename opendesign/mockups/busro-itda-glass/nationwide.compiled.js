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
    }).catch((reason) => active && setError(reason.status === 503 ? "TAGO \uACF5\uC2DD \uB370\uC774\uD130 \uC5F0\uACB0\uC774 \uC900\uBE44\uB418\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4." : "\uC804\uAD6D \uB3C4\uC2DC \uBAA9\uB85D\uC744 \uBD88\uB7EC\uC624\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4."));
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
      if (connection.hydrationReady) {
        try {
          await BusroApi.hydrateRoute(cityCode, route.routeId);
        } catch (reason) {
          setHydrationGap(reason.message || "\uACF5\uC2DD \uACBD\uC720 \uC21C\uC11C\uB97C \uC5EC\uD589 \uADF8\uB798\uD504\uC5D0 \uC801\uC7AC\uD558\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.");
        }
      } else setHydrationGap("\uAC80\uC99D\uB41C \uACF5\uC720 \uCE74\uD0C8\uB85C\uADF8 \uC4F0\uAE30\uAC00 \uBE44\uD65C\uC131\uD654\uB418\uC5B4 \uC0C8 \uB178\uC120\uC740 \uC801\uC7AC\uD558\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.");
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
  const selectedFirstTime = validRouteClock(routeInfo?.first_vehicle_time);
  const selectedLastTime = validRouteClock(routeInfo?.last_vehicle_time);
  const selectedWeekdayHeadway = validHeadway(routeInfo?.weekday_interval_minutes);
  return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(OSMRouteMap, { geometry: geometryPayload?.geometry, stops, positions, loading }), /* @__PURE__ */ React.createElement(GlassCard, { className: "nation-search-card" }, /* @__PURE__ */ React.createElement("div", { className: "nation-mode-title" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "DATA ADMIN \xB7 ROUTE INSPECTOR"), /* @__PURE__ */ React.createElement("h1", null, "\uAC1C\uBCC4 \uB178\uC120", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "\uB370\uC774\uD130 \uAC80\uC99D"))), /* @__PURE__ */ React.createElement(SourceBadge, { mode: connection.mode, label: connection.label })), /* @__PURE__ */ React.createElement("form", { className: "route-search-form", onSubmit: search }, /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, "\uC9C0\uC5ED"), /* @__PURE__ */ React.createElement("select", { value: cityCode, onChange: (event) => setCityCode(event.target.value), "aria-label": "\uBC84\uC2A4 \uC9C0\uC5ED \uC120\uD0DD" }, /* @__PURE__ */ React.createElement("option", { value: "" }, "\uC9C0\uC5ED \uC120\uD0DD"), cities.map((city) => /* @__PURE__ */ React.createElement("option", { key: city.code, value: city.code }, city.name)))), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, "\uB178\uC120\uBC88\uD638"), /* @__PURE__ */ React.createElement("input", { value: routeQuery, onChange: (event) => setRouteQuery(event.target.value), placeholder: "\uC608: 601", maxLength: "24" })), /* @__PURE__ */ React.createElement("button", { type: "submit", disabled: !cityCode || loading }, /* @__PURE__ */ React.createElement(Icon, { name: "magnifying-glass" }), loading ? "\uC870\uD68C \uC911" : "\uB178\uC120 \uCC3E\uAE30")), /* @__PURE__ */ React.createElement("p", { className: "source-note" }, /* @__PURE__ */ React.createElement(Icon, { name: "database" }), " \uC9C0\uC5ED\xB7\uB178\uC120\xB7\uC815\uB958\uC7A5\uC740 TAGO \uACF5\uC2DD \uC2DD\uBCC4\uC790\uB85C \uC870\uD68C\uD569\uB2C8\uB2E4. \uC11C\uBE44\uC2A4 \uD0A4\uB294 \uBE0C\uB77C\uC6B0\uC800\uC5D0 \uC800\uC7A5\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.")), error && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "warning-circle", title: "DATA_GAP" }, error), hydrationGap && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "database", title: "\uADF8\uB798\uD504 DATA_GAP" }, hydrationGap, " \uB178\uC120 \uC9C0\uB3C4\uC640 \uC815\uB958\uC7A5 \uBCF4\uAE30\uB294 \uACC4\uC18D \uC0AC\uC6A9\uD560 \uC218 \uC788\uC2B5\uB2C8\uB2E4."), routes.length > 0 && /* @__PURE__ */ React.createElement("section", { className: "route-catalog" }, /* @__PURE__ */ React.createElement("div", { className: "catalog-heading" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "ROUTES"), /* @__PURE__ */ React.createElement("h2", null, cities.find((item) => item.code === cityCode)?.name || "\uC120\uD0DD \uC9C0\uC5ED", " \uB178\uC120")), /* @__PURE__ */ React.createElement("span", null, routes.length, "\uAC1C")), /* @__PURE__ */ React.createElement("div", { className: "route-result-list" }, routes.map((route) => /* @__PURE__ */ React.createElement("button", { type: "button", key: route.routeId, className: selected?.routeId === route.routeId ? "active" : "", onClick: () => openRoute(route) }, /* @__PURE__ */ React.createElement("span", { className: "route-number" }, route.routeNo || "\u2014"), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("strong", null, route.startName, " \u2192 ", route.endName), /* @__PURE__ */ React.createElement("small", null, route.routeType, " \xB7 ID ", route.routeId)), /* @__PURE__ */ React.createElement(Icon, { name: "caret-right" }))))), selected && /* @__PURE__ */ React.createElement(GlassCard, { className: "selected-route-card" }, /* @__PURE__ */ React.createElement("div", { className: "selected-route-head" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "SELECTED LINE"), /* @__PURE__ */ React.createElement("h2", null, /* @__PURE__ */ React.createElement("span", null, selected.routeNo), routeInfo?.routeType || selected.routeType)), /* @__PURE__ */ React.createElement(PrecisionBadge, { geometryPayload })), /* @__PURE__ */ React.createElement("div", { className: "route-terminal-row" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "\uAE30\uC810"), /* @__PURE__ */ React.createElement("strong", null, routeInfo?.startName || selected.startName), /* @__PURE__ */ React.createElement("span", null, selectedFirstTime ? `\uCCAB\uCC28 ${selectedFirstTime}` : "\uCCAB\uCC28 \uC815\uBCF4 \uBBF8\uD655\uBCF4")), /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" }), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "\uC885\uC810"), /* @__PURE__ */ React.createElement("strong", null, routeInfo?.endName || selected.endName), /* @__PURE__ */ React.createElement("span", null, selectedLastTime ? `\uB9C9\uCC28 ${selectedLastTime}` : "\uB9C9\uCC28 \uC815\uBCF4 \uBBF8\uD655\uBCF4"))), /* @__PURE__ */ React.createElement("div", { className: "route-evidence-row" }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "map-pin" }), " \uACBD\uC720 ", stops.length, "\uAC1C"), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "bus" }), " \uD604\uC7AC \uCC28\uB7C9 ", positions.length, "\uB300"), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), " \uD3C9\uC77C \uBC30\uCC28 ", selectedWeekdayHeadway ?? "\u2014", selectedWeekdayHeadway ? "\uBD84" : "")), /* @__PURE__ */ React.createElement("p", { className: "route-window-note" }, /* @__PURE__ */ React.createElement(Icon, { name: "info" }), " \uCCAB\uCC28\xB7\uB9C9\uCC28\xB7\uBC30\uCC28\uB294 TAGO \uB178\uC120 \uC6B4\uD589\uCC3D\uC785\uB2C8\uB2E4. \uC815\uB958\uC7A5\uBCC4 \uCD9C\uBC1C\uC2DC\uAC01\uC73C\uB85C \uBC14\uAFB8\uC5B4 \uACC4\uC0B0\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4."), geometryPayload?.data_gap && /* @__PURE__ */ React.createElement("p", { className: "geometry-caveat" }, geometryPayload.data_gap), /* @__PURE__ */ React.createElement("div", { className: "stop-preview-list" }, stops.slice(0, 8).map((stop, index) => /* @__PURE__ */ React.createElement("button", { type: "button", key: `${stop.node_id}-${index}`, onClick: () => onUseStop(stop) }, /* @__PURE__ */ React.createElement("span", null, stop.node_order || index + 1), /* @__PURE__ */ React.createElement("strong", null, stop.node_name), /* @__PURE__ */ React.createElement("small", null, stop.node_id)))), stops.length > 8 && /* @__PURE__ */ React.createElement("p", { className: "more-stops" }, "\uC678 ", stops.length - 8, "\uAC1C \uC815\uB958\uC7A5 \xB7 \uC9C0\uB3C4\uC5D0\uC11C \uC804\uCCB4 \uD655\uC778")));
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
  } }, /* @__PURE__ */ React.createElement("strong", null, stop.node_name), /* @__PURE__ */ React.createElement("small", null, /* @__PURE__ */ React.createElement("span", null, stop.city_name || "\uC9C0\uC5ED \uD655\uC778 \uC911", stop.mobile_short_no ? ` \xB7 \uC815\uB958\uC7A5 ${stop.mobile_short_no}` : ""), Number(stop.route_count) > 0 && /* @__PURE__ */ React.createElement("em", { className: stop.graph_ready ? "graph-ready" : "graph-gap" }, "\uB178\uC120 ", stop.route_count, "\uAC1C"))))));
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
    historicalPrior: evidenceObject(schedule.historical_prior || result?.historical_gtfs_prior || result?.reliability?.historical_prior)
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
    label: [provider ? `${provider}${official && !isGtfsEvidence({ provider, basis, feedId }) ? " \uACF5\uC2DD" : ""}` : "", feedId, basisLabel].filter(Boolean).join(" \xB7 ")
  };
}
const candidateRouteWindowCache = /* @__PURE__ */ new Map();
const candidateRouteWindowPending = /* @__PURE__ */ new Map();
const CANDIDATE_ROUTE_WINDOW_CACHE_LIMIT = 96;
const CANDIDATE_ROUTE_WINDOW_CACHE_TTL_MS = 5 * 60 * 1e3;
let activeCandidateRouteWindowBase = "";
let candidateRouteWindowBaseEpoch = 0;
function normalizeCandidateRouteWindowBase(value = BusroApi.getBase()) {
  const raw = String(value || "").trim();
  try {
    const parsed = new URL(raw, window.location.href);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return "";
    parsed.search = "";
    parsed.hash = "";
    return parsed.href.replace(/\/+$/, "");
  } catch {
    return "";
  }
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
    ready: mode === "live" && !fixture && !/SCHEMA_ONLY/i.test(fixtureNotice)
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
      expiresAt: fetchedAt + CANDIDATE_ROUTE_WINDOW_CACHE_TTL_MS
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
  const [windows, setWindows] = useState(() => /* @__PURE__ */ new Map());
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
      const next = /* @__PURE__ */ new Map();
      let nextExpiry = Number.POSITIVE_INFINITY;
      for (const request of requests) {
        const entry = cachedCandidateRouteWindow(candidateRouteWindowKey(apiBase, request));
        if (!entry) continue;
        next.set(`${request.cityCode}|${request.routeId}`, entry);
        nextExpiry = Math.min(nextExpiry, entry.expiresAt);
      }
      setWindows(next);
      if (Number.isFinite(nextExpiry)) refreshTimer = window.setTimeout(() => active && setExpiryTick((value) => value + 1), Math.max(1e3, nextExpiry - Date.now() + 25));
    });
    return () => {
      active = false;
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    };
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
function routeWindowRows(candidate, context = {}, fetchedWindows = /* @__PURE__ */ new Map()) {
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
      route: evidenceText(row.route_no || row.routeNo || row.route_id || row.routeId || `\uB178\uC120 ${index + 1}`),
      first,
      last,
      weekday,
      saturday,
      sunday
    };
  }).filter((row, index, rows) => (row.first || row.last || row.weekday || row.saturday || row.sunday) && rows.findIndex((item) => item.key === row.key) === index).slice(0, 3);
}
function currentTimetableEvidence(candidate, context, schedule, timeSummary, fetchedWindows) {
  const raw = evidenceObject(candidate?.current_timetable || candidate?.current_static_timetable || context?.current_timetable || context?.current_static_timetable);
  const provider = evidenceText(raw.provider || raw.source_name || raw.municipality || (schedule.ready ? schedule.provider : ""));
  const granularity = evidenceText(raw.schedule_granularity || raw.granularity || raw.tier);
  const date = evidenceText(raw.effective_date || raw.valid_from || raw.published_at || raw.source_date);
  const windows = routeWindowRows(candidate, context, fetchedWindows);
  const granularityLabel = { EXACT_STOP_TIMES: "\uC815\uB958\uC7A5\uBCC4 \uC2DC\uAC01", TRIP_ORIGIN_ONLY: "\uAE30\uC810 \uCD9C\uBC1C\uD45C", ROUTE_WINDOW: "\uCCAB\uCC28\xB7\uB9C9\uCC28\xB7\uBC30\uCC28" }[granularity] || granularity.replaceAll("_", " ");
  if (schedule.ready && timeSummary.length > 0) return {
    ready: true,
    label: [provider || "\uD604\uC7AC \uACF5\uC2DD \uC2DC\uAC04\uD45C", granularityLabel, date].filter(Boolean).join(" \xB7 "),
    detail: timeSummary.join(" \xB7 "),
    windows
  };
  if (windows.length > 0 || provider || granularity) return {
    ready: true,
    label: [provider || "TAGO", granularityLabel || "ROUTE_WINDOW", date].filter(Boolean).join(" \xB7 "),
    detail: windows.length > 0 ? "\uC2E4\uC81C \uC218\uC2E0\uB41C \uCCAB\uCC28\xB7\uB9C9\uCC28\xB7\uBC30\uCC28 \uBC94\uC704" : "\uD604\uC7AC \uCD9C\uCC98\uC758 \uC81C\uACF5 \uBC94\uC704\uB9CC \uD45C\uC2DC",
    windows
  };
  return { ready: false, label: "\uC815\uB958\uC7A5\uBCC4 \uCD9C\uBC1C\uC2DC\uAC01 \uBBF8\uD655\uBCF4", detail: "TAGO \uACBD\uB85C\uB294 \uACC4\uC18D \uD45C\uC2DC \xB7 \uC784\uC758 \uC2DC\uAC01 \uC0DD\uC131 \uC548 \uD568", windows: [] };
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
    label: present ? [provider, feedId || (hash ? `SHA-256 ${hash.slice(0, 12)}\u2026` : "")].filter(Boolean).join(" \xB7 ") : matched ? "GTFS \uBAA8\uB378 \uADFC\uAC70 \uC2DD\uBCC4\uC790 \uC5C6\uC74C" : hasRawPrior ? "\uD604\uC7AC \uB178\uC120\uACFC GTFS \uBAA8\uB378 \uADFC\uAC70 \uBBF8\uB9E4\uCE6D" : "GTFS \uACFC\uAC70 \uBAA8\uB378 \uADFC\uAC70 \uBBF8\uC5F0\uACB0",
    detail: present ? "\uD604\uC7AC \uB178\uC120\uC5D0 \uB9E4\uCE6D\uB41C \uBAA8\uB378 \uAC00\uC911\uCE58 \uC804\uC6A9 \xB7 \uD604\uC7AC \uC2DC\uAC04\uD45C \uD22C\uC601 \uC548 \uD568 \xB7 \uB2E8\uB3C5 \uC131\uACF5\uB960 \uBBF8\uC0B0\uCD9C" : "\uD604\uC7AC \uB178\uC120\uACFC \uBA85\uC2DC\uC801\uC73C\uB85C \uB9E4\uCE6D\uB41C \uADFC\uAC70\uAC00 \uC0DD\uAE30\uAE30 \uC804\uC5D0\uB294 \uAC00\uC911\uCE58\uB85C \uC0AC\uC6A9\uD558\uC9C0 \uC54A\uC74C"
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
function JourneyEvidenceStack({ candidate, context = {}, schedule, provenance, timeSummary, connection, fetchedWindows = /* @__PURE__ */ new Map() }) {
  const timetable = currentTimetableEvidence(candidate, context, schedule, timeSummary, fetchedWindows);
  const prior = historicalPriorEvidence(candidate, context, schedule, provenance);
  const reliability = evidenceObject(candidate?.reliability || context?.reliability);
  const trust = evidenceObject(reliability.trust_assumption);
  const live = evidenceObject(candidate?.realtime_adjustment || candidate?.live_adjustment || context?.realtime_adjustment);
  const liveReady = /READY|LIVE|APPLIED/i.test(evidenceText(live.status || live.state));
  const liveDetail = liveReady ? evidenceText(live.summary || live.detail || "TAGO \uB3C4\uCC29\xB7\uCC28\uB7C9 \uC704\uCE58 \uBCF4\uC815 \uBC18\uC601") : connection?.mode === "live" || connection?.mode === "ready" ? "TAGO \uC5F0\uACB0\uB428 \xB7 \uC120\uD0DD \uD6C4 \uC815\uB958\uC7A5\uBCC4 \uB3C4\uCC29\uC815\uBCF4\uB85C \uBCF4\uC815" : "\uC120\uD0DD \uD6C4 TAGO \uB3C4\uCC29\uC815\uBCF4\uB85C \uBCF4\uC815 \xB7 \uD604\uC7AC\uAC12 \uC5C6\uC74C";
  return /* @__PURE__ */ React.createElement("div", { className: "journey-evidence-stack", "aria-label": "\uACBD\uB85C \uB370\uC774\uD130 \uADFC\uAC70" }, /* @__PURE__ */ React.createElement("div", { className: "evidence-tier current" }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "path" })), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "1 \xB7 \uD604\uC7AC \uACBD\uB85C"), /* @__PURE__ */ React.createElement("strong", null, "TAGO \uACBD\uC720 \uC21C\uC11C"), /* @__PURE__ */ React.createElement("p", null, "\uD604\uC7AC \uACF5\uC2DD \uC2DD\uBCC4\uC790\uB85C \uC5F0\uACB0\uD55C \uB2E8\uBC29\uD5A5 \uACBD\uB85C"))), /* @__PURE__ */ React.createElement("div", { className: `evidence-tier ${timetable.ready ? "current" : "gap"}` }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "clock" })), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "2 \xB7 \uD604\uC7AC \uC815\uC801 \uC2DC\uAC04\uD45C"), /* @__PURE__ */ React.createElement("strong", null, timetable.label), /* @__PURE__ */ React.createElement("p", null, timetable.detail), timetable.windows.map((row) => /* @__PURE__ */ React.createElement("em", { key: row.key }, row.route, " \xB7 ", [row.first ? `\uCCAB\uCC28 ${row.first}` : "", row.last ? `\uB9C9\uCC28 ${row.last}` : "", row.weekday ? `\uD3C9\uC77C ${row.weekday}\uBD84` : "", row.saturday ? `\uD1A0 ${row.saturday}\uBD84` : "", row.sunday ? `\uC77C ${row.sunday}\uBD84` : ""].filter(Boolean).join(" \xB7 "))))), /* @__PURE__ */ React.createElement("div", { className: `evidence-tier ${liveReady ? "live" : "pending"}` }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "broadcast" })), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "3 \xB7 \uC2E4\uC2DC\uAC04 \uBCF4\uC815"), /* @__PURE__ */ React.createElement("strong", null, "TAGO \uB3C4\uCC29\xB7\uCC28\uB7C9 \uC704\uCE58"), /* @__PURE__ */ React.createElement("p", null, liveDetail))), /* @__PURE__ */ React.createElement("div", { className: `evidence-tier ${prior.present ? "prior" : "pending"}` }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "flask" })), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "4 \xB7 \uACFC\uAC70 \uBAA8\uB378 \uADFC\uAC70"), /* @__PURE__ */ React.createElement("strong", null, prior.label), /* @__PURE__ */ React.createElement("p", null, prior.detail))), trust.code === "USUALLY_ON_TIME" && /* @__PURE__ */ React.createElement("small", { className: "trust-assumption" }, /* @__PURE__ */ React.createElement(Icon, { name: "shield-check" }), " \uC6B4\uC601 \uAC00\uC815: \uB300\uCCB4\uB85C \uC815\uC2DC \xB7 \uC2E4\uCE21 \uD655\uB960 \uC544\uB2D8"));
}
function summarizeJourneyLegs(candidate) {
  const legs = [];
  for (const step of Array.isArray(candidate?.steps) ? candidate.steps : []) {
    if (step?.kind !== "ride" || !step.route_id) continue;
    const routeId = String(step.route_id);
    const tripId = String(step.trip_id || "");
    const explicitStopCount = Number(step.stop_count);
    const orderDelta = Number(step.stop_order_delta);
    const edgeCount = Number.isFinite(explicitStopCount) && explicitStopCount >= 2 ? Math.max(1, Math.round(explicitStopCount) - 1) : Number.isFinite(orderDelta) && orderDelta >= 1 ? Math.round(orderDelta) : 1;
    const departureTime = formatGtfsClock(step.departure_time ?? step.from?.departure_time, step.departure_seconds ?? step.from?.departure_seconds);
    const arrivalTime = formatGtfsClock(step.arrival_time ?? step.to?.arrival_time, step.arrival_seconds ?? step.to?.arrival_seconds);
    const previous = legs[legs.length - 1];
    if (previous && previous.routeId === routeId && (!previous.tripId || !tripId || previous.tripId === tripId)) {
      previous.to = step.to || previous.to;
      previous.edgeCount += edgeCount;
      previous.arrivalTime = arrivalTime || previous.arrivalTime;
    } else {
      legs.push({ routeId, tripId, from: step.from || {}, to: step.to || {}, edgeCount, departureTime, arrivalTime });
    }
  }
  const replayRows = Array.isArray(candidate?.replay_legs) ? candidate.replay_legs : [];
  legs.forEach((leg, index) => {
    const replayRow = replayRows[index] || {};
    const scheduledSeconds = replayRow.scheduled_minutes === null || replayRow.scheduled_minutes === void 0 ? void 0 : Number(replayRow.scheduled_minutes) * 60;
    const nextDepartureSeconds = replayRow.next_departure_minutes === null || replayRow.next_departure_minutes === void 0 ? void 0 : Number(replayRow.next_departure_minutes) * 60;
    leg.arrivalTime || (leg.arrivalTime = formatGtfsClock(replayRow.scheduled_arrival, scheduledSeconds));
    leg.nextDepartureTime = formatGtfsClock(replayRow.next_departure, nextDepartureSeconds);
  });
  return legs;
}
function prepareJourneyForDetail(candidate, context) {
  const currentSchedule = normalizeSchedule({ schedule: context?.schedule || {} });
  const replayLegs = (Array.isArray(candidate?.replay_legs) ? candidate.replay_legs : []).map((row) => {
    const sourceId = typeof row.time_evidence_source === "object" ? String(row.time_evidence_source.source_id || "") : String(row.time_evidence_source || "");
    const scheduledMinutes = gtfsClockMinutes(row.scheduled_arrival) ?? (row.scheduled_minutes !== null && row.scheduled_minutes !== void 0 && Number.isFinite(Number(row.scheduled_minutes)) ? Number(row.scheduled_minutes) : null);
    const nextDepartureMinutes = gtfsClockMinutes(row.next_departure) ?? (row.next_departure_minutes !== null && row.next_departure_minutes !== void 0 && Number.isFinite(Number(row.next_departure_minutes)) ? Number(row.next_departure_minutes) : null);
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
function JourneyCandidateCard({ candidate, index, schedule, structural = false, context, connection, fetchedWindows, onChooseJourney }) {
  const routeRows = Array.isArray(candidate?.routes) ? candidate.routes : [];
  const routeIds = Array.isArray(candidate?.route_ids) ? candidate.route_ids.filter(Boolean) : routeRows.map((item) => item?.route_id || item?.routeId).filter(Boolean);
  const displayRoutes = routeRows.map((item, routeIndex) => {
    const routeId = String(item?.route_id || item?.routeId || "");
    const cityCode = String(item?.city_code || item?.cityCode || "");
    const fetched = fetchedWindows?.get(`${cityCode}|${routeId}`)?.route || {};
    const rawLabel = String(fetched.route_no || fetched.routeNo || item?.route_no || item?.routeNo || "");
    const label = rawLabel && rawLabel !== routeId && !/^[A-Z]{2,}\d{6,}$/i.test(rawLabel) ? rawLabel : `\uBC84\uC2A4 ${routeIndex + 1}`;
    return { ...item, route_id: routeId, route_no: label };
  });
  const routeLabels = new Map(displayRoutes.map((item) => [item.route_id, item.route_no]));
  const legs = summarizeJourneyLegs(candidate);
  const departureTime = formatGtfsClock(candidate?.departure_time, candidate?.departure_seconds);
  const arrivalTime = formatGtfsClock(candidate?.arrival_time, candidate?.arrival_seconds);
  const minutes = Number(candidate?.estimated_minutes);
  const timeSummary = [departureTime ? `\uCD9C\uBC1C ${departureTime}` : "", arrivalTime ? `\uB3C4\uCC29 ${arrivalTime}` : "", Number.isFinite(minutes) ? `${Math.max(0, Math.round(minutes))}\uBD84` : ""].filter(Boolean);
  return /* @__PURE__ */ React.createElement("article", { className: structural ? "structural-candidate" : "scheduled-candidate" }, /* @__PURE__ */ React.createElement("div", { className: "candidate-rank" }, index + 1), /* @__PURE__ */ React.createElement("div", { className: "candidate-copy" }, /* @__PURE__ */ React.createElement("div", { className: "candidate-title" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", null, index === 0 ? "\uCD94\uCC9C \uACBD\uB85C" : JOURNEY_CRITERION_LABELS[candidate?.criterion] || "\uB2E4\uB978 \uACBD\uB85C"), /* @__PURE__ */ React.createElement("h3", null, routeIds.length > 0 ? `${candidate?.transfers || 0}\uD68C \uD658\uC2B9 \xB7 \uBC84\uC2A4 ${routeIds.length}\uB300` : "\uACBD\uB85C \uC815\uBCF4 \uD655\uC778 \uC911")), index === 0 && /* @__PURE__ */ React.createElement("small", { className: "topology-ready" }, "\uCD94\uCC9C")), !structural && timeSummary.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "schedule-summary" }, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), /* @__PURE__ */ React.createElement("strong", null, timeSummary.join(" \xB7 "))), /* @__PURE__ */ React.createElement("div", { className: "candidate-leg-list" }, legs.map((leg, legIndex) => /* @__PURE__ */ React.createElement(React.Fragment, { key: `${leg.routeId}-${leg.tripId}-${legIndex}` }, legIndex > 0 && /* @__PURE__ */ React.createElement("div", { className: "candidate-transfer-note" }, /* @__PURE__ */ React.createElement(Icon, { name: "person-simple-walk" }), " \uAC78\uC5B4\uC11C \uB2E4\uC74C \uBC84\uC2A4\uB85C \uD658\uC2B9"), /* @__PURE__ */ React.createElement("div", { className: "candidate-leg" }, /* @__PURE__ */ React.createElement("span", { className: "timeline-rail" }, /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("b", null)), /* @__PURE__ */ React.createElement("div", { className: "leg-copy" }, /* @__PURE__ */ React.createElement("span", { className: "route-pill" }, /* @__PURE__ */ React.createElement(Icon, { name: "bus" }), " ", routeLabels.get(leg.routeId) || leg.routeId), /* @__PURE__ */ React.createElement("strong", null, leg.from?.node_name || leg.from?.node_id || "\uC2B9\uCC28 \uC815\uB958\uC7A5"), !structural && leg.departureTime && /* @__PURE__ */ React.createElement("span", { className: "leg-time" }, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), " ", leg.departureTime, " \uCD9C\uBC1C"), /* @__PURE__ */ React.createElement("small", null, leg.edgeCount + 1, "\uAC1C \uC815\uB958\uC7A5 \uC774\uB3D9"), /* @__PURE__ */ React.createElement("strong", null, leg.to?.node_name || leg.to?.node_id || "\uD558\uCC28 \uC815\uB958\uC7A5"), !structural && leg.arrivalTime && /* @__PURE__ */ React.createElement("span", { className: "leg-time arrival" }, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), " ", leg.arrivalTime, " \uB3C4\uCC29"), !structural && leg.nextDepartureTime && /* @__PURE__ */ React.createElement("span", { className: "transfer-time" }, "\uB2E4\uC74C \uBC84\uC2A4 ", leg.nextDepartureTime, " \uCD9C\uBC1C")))))), /* @__PURE__ */ React.createElement("footer", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "arrows-left-right" }), " ", typeof candidate?.transfers === "number" ? `${candidate.transfers}\uD68C \uD658\uC2B9` : "\uD658\uC2B9 \uD655\uC778 \uC911"), typeof candidate?.walking_m === "number" && /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "person-simple-walk" }), " \uB3C4\uBCF4 ", Math.round(candidate.walking_m), "m")), /* @__PURE__ */ React.createElement("button", { className: structural ? "open-candidate structural" : "open-candidate", type: "button", onClick: () => onChooseJourney?.(prepareJourneyForDetail({ ...candidate, routes: displayRoutes }, context)) }, "\uC9C0\uB3C4\uC5D0\uC11C \uACBD\uB85C \uBCF4\uAE30 ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" }))));
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
  const scheduleSearchState = graph?.search_complete === true ? "\uAC80\uC0C9 \uC644\uB8CC" : graph?.search_complete === false ? "\uAC80\uC0C9 \uBBF8\uC644\uB8CC" : "\uC644\uB8CC \uC0C1\uD0DC DATA_GAP";
  const scheduleDetailReason = graph?.detail_reason || result?.schedule?.detail_reason || "";
  const primaryStatus = nationwideComplete ? "\uC804\uAD6D \uACBD\uB85C\uB9DD \uC5F0\uACB0\uB428" : graphReady ? "\uACF5\uC2DD \uAC80\uC99D \uAD6C\uAC04 \uC5F0\uACB0\uB428" : "\uC804\uAD6D \uACBD\uB85C\uB9DD \uC900\uBE44 \uC911";
  const catalogSummary = stopRows && routeRows ? `\uC815\uB958\uC7A5 ${formatCount(stopRows)} \xB7 \uB178\uC120 ${formatCount(routeRows)}` : "\uC804\uAD6D \uBAA9\uB85D DATA_GAP";
  const topologySummary = activeRoutes ? `\uBC29\uD5A5 \uB178\uC120 ${formatCount(activeRoutes)} \xB7 \uADF8\uB798\uD504 \uC815\uB958\uC7A5 ${formatCount(activeStops)}` : topologyTargets ? `\uBC29\uD5A5 \uC21C\uC11C ${formatCount(topologyComplete)}/${formatCount(topologyTargets)}` : "\uBC29\uD5A5 \uC21C\uC11C DATA_GAP";
  return /* @__PURE__ */ React.createElement("div", { className: `graph-coverage ${graphReady ? "catalog-ready" : "catalog-gap"}` }, /* @__PURE__ */ React.createElement("span", { className: "graph-pulse", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("strong", null, primaryStatus), /* @__PURE__ */ React.createElement("small", null, catalogSummary, " \xB7 ", topologySummary)), /* @__PURE__ */ React.createElement("span", { className: "graph-method" }, scheduleGraph ? "\uD604\uC7AC \uC2DC\uAC04\uD45C Dijkstra" : "TAGO \uBC29\uD5A5 Dijkstra"), graphReady && !nationwideComplete && /* @__PURE__ */ React.createElement("small", { className: "coverage-query" }, "\uACF5\uC2DD \uACBD\uC720 \uC21C\uC11C\uAC00 \uC5F0\uACB0\uB41C ", formatCount(activeCities), "\uAC1C \uC9C0\uC5ED\uBD80\uD130 \uC2E4\uC81C \uBC29\uD5A5\uC73C\uB85C \uAC80\uC0C9\uD569\uB2C8\uB2E4. \uC804\uAD6D \uD655\uB300 \uC911\uC785\uB2C8\uB2E4."), !graphReady && /* @__PURE__ */ React.createElement("small", { className: "coverage-gap" }, "TAGO \uB178\uC120\uBCC4 \uACBD\uC720 \uC21C\uC11C\uC758 \uC804\uAD6D \uC801\uC7AC\uAC00 \uB05D\uB098\uC9C0 \uC54A\uC544, \uD655\uC778\uB41C \uAD6C\uAC04\uB9CC \uAC80\uC0C9\uD569\uB2C8\uB2E4."), scheduleGraph && /* @__PURE__ */ React.createElement("small", { className: schedule.ready ? "coverage-query" : "coverage-gap" }, "\uC774\uBC88 \uC77C\uC815 \uAC80\uC0C9: ", formatCount(graph.expanded_stops), "\uAC1C \uC815\uB958\uC7A5 \uD655\uC7A5 \xB7 ", formatCount(graph.departures_scanned), "\uAC1C \uCD9C\uBC1C\uD3B8 \uD655\uC778 \xB7 ", scheduleSearchState, " \xB7 ", graph.algorithm), scheduleGraph && scheduleDetailReason && /* @__PURE__ */ React.createElement("small", { className: "coverage-gap" }, "\uC2DC\uAC04\uD45C \uC0C1\uC138: ", scheduleDetailReason), staticAlternativeCount > 0 && /* @__PURE__ */ React.createElement("small", { className: "coverage-query" }, "\uD604\uC7AC TAGO \uBC29\uD5A5 \uACBD\uB85C ", formatCount(staticAlternativeCount), "\uAC74 \uD655\uC778 \xB7 \uC815\uB958\uC7A5\uBCC4 \uC2DC\uAC01\uC774 \uC5C6\uC5B4\uB3C4 \uC6B0\uC120 \uD45C\uC2DC"), schedule.historical && /* @__PURE__ */ React.createElement("small", { className: "coverage-prior" }, "\uACFC\uAC70 GTFS\uB294 \uBAA8\uB378 \uAC00\uC911\uCE58 \uC804\uC6A9 \xB7 \uD604\uC7AC \uB0A0\uC9DC \uC2DC\uAC04\uD45C\uB85C \uD22C\uC601\uD558\uC9C0 \uC54A\uC74C"), graph && !scheduleGraph && /* @__PURE__ */ React.createElement("small", { className: topologyReady ? "coverage-query" : "coverage-gap" }, "\uC774\uBC88 \uAC80\uC0C9: ", formatCount(graph.nodes), "\uAC1C \uC0C1\uD0DC \xB7 ", formatCount(graph.edges), "\uAC1C \uC2B9\uCC28 \uAC04\uC120 \xB7 ", graph.algorithm || "directed_dijkstra"), graph && !scheduleGraph && !topologyReady && staticAlternativeCount === 0 && /* @__PURE__ */ React.createElement("small", { className: "coverage-gap" }, "DATA_GAP \xB7 \uAC80\uC0C9 \uAC00\uB2A5\uD55C \uAC80\uC99D \uB178\uC120 \uC21C\uC11C\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4."));
}
const journeySearchSession = {
  fromText: "",
  toText: "",
  fromStop: null,
  toStop: null,
  travelMode: "country",
  preference: "diverse",
  result: null
};
function JourneyGenerator({ seededStop, onChooseJourney, connection }) {
  const [fromText, setFromText] = useState(journeySearchSession.fromText);
  const [toText, setToText] = useState(journeySearchSession.toText);
  const [fromStop, setFromStop] = useState(journeySearchSession.fromStop);
  const [toStop, setToStop] = useState(journeySearchSession.toStop);
  const [travelMode, setTravelMode] = useState(journeySearchSession.travelMode);
  const [preference, setPreference] = useState(journeySearchSession.preference);
  const [result, setResult] = useState(journeySearchSession.result);
  const [checkTime, setCheckTime] = useState(false);
  const [serviceDate, setServiceDate] = useState(() => localDateValue());
  const [departureTime, setDepartureTime] = useState(() => localTimeValue());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
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
    Object.assign(journeySearchSession, { fromText, toText, fromStop, toStop, travelMode, preference, result });
  }, [fromText, toText, fromStop, toStop, travelMode, preference, result]);
  async function generate(event) {
    event.preventDefault();
    if (!fromStop || !toStop) return;
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const timing = checkTime ? { service_date: serviceDate, departure_time: departureTime } : {};
      setResult(await BusroApi.generateJourneys({ from_stop_id: fromStop.node_id, to_stop_id: toStop.node_id, from_city_code: fromStop.city_code || void 0, to_city_code: toStop.city_code || void 0, ...timing, preference, max_alternatives: 5 }));
    } catch (reason) {
      const errorCode = reason?.payload?.error?.code || "";
      setError(errorCode === "SEARCH_BUDGET_REACHED" ? "\uC774\uBC88 \uAC80\uC0C9\uC740 \uCC98\uB9AC \uBC94\uC704 \uC81C\uD55C\uC5D0 \uB3C4\uB2EC\uD588\uC2B5\uB2C8\uB2E4. \uCD9C\uBC1C\xB7\uB3C4\uCC29 \uB610\uB294 \uACBD\uB85C \uAE30\uC900\uC744 \uBC14\uAFB8\uC5B4 \uB2E4\uC2DC \uAC80\uC0C9\uD574 \uC8FC\uC138\uC694." : reason.message || "\uD604\uC7AC \uC801\uC7AC\uB41C \uB178\uC120 \uADF8\uB798\uD504\uB85C \uC5EC\uD589\uC744 \uB9CC\uB4E4\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.");
    } finally {
      setLoading(false);
    }
  }
  const schedule = normalizeSchedule(result);
  const candidateRows = result?.candidates || result?.journeys || result?.alternatives || [];
  const returnedCandidates = Array.isArray(candidateRows) ? candidateRows : [];
  const scheduled = schedule.ready ? returnedCandidates.filter((candidate) => candidate?.scheduled !== false) : [];
  const staticRows = Array.isArray(result?.static_alternatives) ? result.static_alternatives : [];
  const structuralPool = [...staticRows, ...returnedCandidates.filter((candidate) => candidate?.scheduled !== true && !scheduled.includes(candidate))];
  const structuralCandidates = structuralPool.filter((candidate, index, rows) => rows.findIndex((item) => item?.id && item.id === candidate?.id || !item?.id && JSON.stringify(item?.route_ids || []) === JSON.stringify(candidate?.route_ids || []) && item?.criterion === candidate?.criterion) === index);
  const resultGraph = result?.graph && typeof result.graph === "object" ? result.graph : {};
  const shownCandidateCount = structuralCandidates.length + scheduled.length;
  const alternativesTruncated = resultGraph.alternatives_truncated === true && shownCandidateCount > 0;
  const returnedAlternativeCount = Number.isFinite(Number(resultGraph.alternatives_returned)) ? Number(resultGraph.alternatives_returned) : shownCandidateCount;
  const requestedAlternativeCount = Number.isFinite(Number(resultGraph.alternatives_requested)) ? Number(resultGraph.alternatives_requested) : 5;
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
    historical_prior: result?.historical_gtfs_prior || result?.reliability?.historical_prior || null
  };
  const gapReasons = {
    STOP_NOT_IN_HYDRATED_SEQUENCE: "\uC120\uD0DD\uD55C \uC815\uB958\uC7A5\uC740 \uC804\uAD6D \uBAA9\uB85D\uC5D0 \uC788\uC9C0\uB9CC \uAC80\uC99D\uB41C \uB178\uC120 \uC21C\uC11C \uADF8\uB798\uD504\uC5D0\uB294 \uC544\uC9C1 \uD3EC\uD568\uB418\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.",
    STOP_NOT_IN_ACTIVE_SEQUENCE: "\uC120\uD0DD\uD55C \uC815\uB958\uC7A5\uC740 \uC804\uAD6D \uBAA9\uB85D\uC5D0 \uC788\uC9C0\uB9CC \uD604\uC7AC \uC801\uC7AC\uB41C TAGO \uC6B4\uD589 \uC21C\uC11C\uC5D0\uB294 \uC544\uC9C1 \uD3EC\uD568\uB418\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.",
    STOP_NOT_ROUTABLE_NEARBY: "\uC120\uD0DD\uD55C \uC815\uB958\uC7A5\uACFC 300m \uC548\uC5D0\uC11C \uC2E4\uC81C \uC2B9\uCC28 \uAC00\uB2A5\uD55C \uBC84\uC2A4 \uC815\uB958\uC7A5\uC744 \uCC3E\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.",
    NO_DIRECTED_PATH_IN_HYDRATED_GRAPH: "\uD604\uC7AC \uAC80\uC99D \uADF8\uB798\uD504\uC5D0\uC11C \uCD9C\uBC1C \uBC29\uD5A5\uBD80\uD130 \uB3C4\uCC29 \uBC29\uD5A5\uAE4C\uC9C0 \uC774\uC5B4\uC9C0\uB294 \uACBD\uB85C\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4. \uC5ED\uBC29\uD5A5 \uAC04\uC120\uC744 \uC784\uC758\uB85C \uB9CC\uB4E4\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.",
    NO_DIRECTED_PATH_IN_SQLITE_GRAPH: "\uD604\uC7AC \uC801\uC7AC\uB41C TAGO \uBC29\uD5A5 \uB178\uC120\uC5D0\uC11C \uCD9C\uBC1C\uC9C0\uBD80\uD130 \uB3C4\uCC29\uC9C0\uAE4C\uC9C0 \uC774\uC5B4\uC9C0\uB294 \uACBD\uB85C\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4. \uC0C1\xB7\uD558\uD589\uC744 \uC784\uC758\uB85C \uC774\uC5B4 \uBD99\uC774\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.",
    SEARCH_BUDGET_REACHED: "\uC774\uBC88 \uAC80\uC0C9\uC740 \uCC98\uB9AC \uBC94\uC704 \uC81C\uD55C\uC5D0 \uB3C4\uB2EC\uD574 \uACBD\uB85C \uD655\uC778\uC744 \uB9C8\uCE58\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4. \uCD9C\uBC1C\xB7\uB3C4\uCC29 \uB610\uB294 \uACBD\uB85C \uAE30\uC900\uC744 \uBC14\uAFB8\uC5B4 \uB2E4\uC2DC \uAC80\uC0C9\uD574 \uC8FC\uC138\uC694.",
    EVIDENCE_INCOMPLETE: "\uD604\uC7AC TAGO \uACBD\uB85C\uB294 \uCC3E\uC558\uC9C0\uB9CC \uC815\uB958\uC7A5\uBCC4 \uC2DC\uAC04\uD45C \uB610\uB294 \uC2E4\uC81C \uD1B5\uACFC \uC774\uB825\uC774 \uBD80\uC871\uD569\uB2C8\uB2E4.",
    SCHEDULE_DATA_GAP: "\uC815\uB958\uC7A5\uBCC4 \uD604\uC7AC \uCD9C\uBC1C\uC2DC\uAC01\uC740 \uD655\uBCF4\uB418\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4. TAGO \uBC29\uD5A5 \uACBD\uB85C\uC640 \uD655\uBCF4\uB41C \uB178\uC120 \uC6B4\uD589\uCC3D\uC740 \uACC4\uC18D \uD45C\uC2DC\uD569\uB2C8\uB2E4.",
    HISTORICAL_GTFS_PRIOR_ONLY: "\uACFC\uAC70 GTFS\uB294 \uC2E0\uB8B0\uB3C4 \uBAA8\uB378 \uADFC\uAC70\uB85C\uB9CC \uC0AC\uC6A9\uD558\uBA70 \uC624\uB298 \uC2DC\uAC04\uD45C\uB85C \uD22C\uC601\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4."
  };
  const emptyReason = result?.reason || result?.schedule?.reason || "";
  const emptyMessage = gapReasons[emptyReason] || "\uD604\uC7AC \uAC80\uC99D\uB41C \uBC29\uD5A5 \uB178\uC120 \uC548\uC5D0\uC11C \uCD9C\uBC1C\uC9C0\uBD80\uD130 \uB3C4\uCC29\uC9C0\uAE4C\uC9C0 \uC774\uC5B4\uC9C0\uB294 \uACBD\uB85C\uB97C \uCC3E\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.";
  const emptyTitle = emptyReason === "SEARCH_BUDGET_REACHED" ? "\uC774\uBC88 \uAC80\uC0C9 \uBC94\uC704\uB97C \uBAA8\uB450 \uD655\uC778\uD558\uC9C0 \uBABB\uD588\uC5B4\uC694" : ["STOP_NOT_IN_HYDRATED_SEQUENCE", "STOP_NOT_IN_ACTIVE_SEQUENCE", "STOP_NOT_ROUTABLE_NEARBY"].includes(emptyReason) ? "\uC774 \uC815\uB958\uC7A5\uC758 \uB178\uC120 \uB370\uC774\uD130\uB97C \uC900\uBE44\uD558\uACE0 \uC788\uC5B4\uC694" : "\uC774\uC5B4\uC9C0\uB294 \uBC84\uC2A4 \uACBD\uB85C\uAC00 \uC5C6\uC5B4\uC694";
  return /* @__PURE__ */ React.createElement("section", { className: "journey-generator" }, /* @__PURE__ */ React.createElement(GlassCard, { className: "generator-card" }, /* @__PURE__ */ React.createElement("div", { className: "generator-heading" }, /* @__PURE__ */ React.createElement("div", { className: "travel-mode-switch", role: "group", "aria-label": "\uC5EC\uD589 \uBC29\uC2DD" }, /* @__PURE__ */ React.createElement("button", { type: "button", className: travelMode === "country" ? "active" : "", onClick: () => {
    setTravelMode("country");
    setPreference("diverse");
  } }, /* @__PURE__ */ React.createElement(Icon, { name: "flag-banner" }), " \uAD6D\uD1A0\uC885\uC8FC"), /* @__PURE__ */ React.createElement("button", { type: "button", className: travelMode === "outing" ? "active" : "", onClick: () => {
    setTravelMode("outing");
    setPreference("low_transfer");
  } }, /* @__PURE__ */ React.createElement(Icon, { name: "map-pin" }), " \uB3D9\uB124 \uB098\uB4E4\uC774")), /* @__PURE__ */ React.createElement("h1", null, travelMode === "country" ? "\uC2DC\uB0B4\uBC84\uC2A4\uB85C \uC5B4\uB514\uAE4C\uC9C0 \uAC00\uBCFC\uAE4C\uC694?" : "\uAC00\uAE4C\uC6B4 \uACF3\uB3C4 \uBC84\uC2A4\uB85C \uC774\uC5B4\uAC00\uC694"), /* @__PURE__ */ React.createElement("p", null, travelMode === "country" ? "\uCD9C\uBC1C\uACFC \uB3C4\uCC29\uC744 \uACE0\uB974\uBA74 \uC804\uAD6D \uBC84\uC2A4 \uB178\uC120\uC744 \uC774\uC5B4 \uC5EC\uB7EC \uACBD\uB85C\uB97C \uCC3E\uC544\uB4DC\uB824\uC694." : "\uCD9C\uBC1C\uACFC \uB3C4\uCC29\uC744 \uACE0\uB974\uBA74 \uD658\uC2B9\uC774 \uC801\uACE0 \uAC77\uAE30 \uD3B8\uD55C \uACBD\uB85C\uBD80\uD130 \uBCF4\uC5EC\uB4DC\uB824\uC694.")), /* @__PURE__ */ React.createElement("form", { onSubmit: generate }, /* @__PURE__ */ React.createElement("div", { className: "route-point-sheet" }, /* @__PURE__ */ React.createElement("div", { className: "route-point origin" }, /* @__PURE__ */ React.createElement("span", { className: "point-mark", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement(StopLookup, { label: "\uCD9C\uBC1C", value: fromText, onChange: setFromText, selected: fromStop, onSelect: setFromStop })), /* @__PURE__ */ React.createElement("div", { className: "route-point destination" }, /* @__PURE__ */ React.createElement("span", { className: "point-mark", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement(StopLookup, { label: "\uB3C4\uCC29", value: toText, onChange: setToText, selected: toStop, onSelect: setToStop })), /* @__PURE__ */ React.createElement("button", { className: "generator-swap", type: "button", onClick: () => {
    setFromStop(toStop);
    setToStop(fromStop);
    setFromText(toText);
    setToText(fromText);
  }, "aria-label": "\uCD9C\uBC1C\uACFC \uB3C4\uCC29 \uBC14\uAFB8\uAE30" }, /* @__PURE__ */ React.createElement(Icon, { name: "arrows-down-up" }))), /* @__PURE__ */ React.createElement("details", { className: "timing-option", open: checkTime }, /* @__PURE__ */ React.createElement("summary", { onClick: (event) => {
    event.preventDefault();
    setCheckTime((value) => !value);
  } }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), " \uCD9C\uBC1C \uC2DC\uAC04\uB3C4 \uD655\uC778\uD558\uAE30"), /* @__PURE__ */ React.createElement(Icon, { name: checkTime ? "caret-up" : "caret-down" })), checkTime && /* @__PURE__ */ React.createElement("div", { className: "schedule-input-grid" }, /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "calendar-blank" }), " \uC5EC\uD589 \uB0A0\uC9DC"), /* @__PURE__ */ React.createElement("input", { type: "date", value: serviceDate, onChange: (event) => setServiceDate(event.target.value), required: true })), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), " \uCD9C\uBC1C \uC2DC\uAC01"), /* @__PURE__ */ React.createElement("input", { type: "time", value: departureTime, onChange: (event) => setDepartureTime(event.target.value), step: "60", required: true })))), /* @__PURE__ */ React.createElement("fieldset", null, /* @__PURE__ */ React.createElement("legend", null, "\uACBD\uB85C \uAE30\uC900"), /* @__PURE__ */ React.createElement("div", { className: "preference-grid" }, (travelMode === "country" ? [["diverse", "\uCD94\uCC9C", "sparkle"], ["low_transfer", "\uD658\uC2B9 \uC801\uAC8C", "arrows-left-right"], ["challenge", "\uBC84\uC2A4 \uB9CE\uC774", "flag-banner"]] : [["low_transfer", "\uD658\uC2B9 \uC801\uAC8C", "arrows-left-right"], ["diverse", "\uC5EC\uB7EC \uACBD\uB85C", "map-trifold"]]).map(([value, label, icon]) => /* @__PURE__ */ React.createElement("button", { type: "button", key: value, className: preference === value ? "active" : "", onClick: () => setPreference(value) }, /* @__PURE__ */ React.createElement(Icon, { name: icon }), label)))), /* @__PURE__ */ React.createElement("button", { className: "liquid-button route-search-primary", type: "submit", disabled: !fromStop || !toStop || loading }, loading ? "\uBC84\uC2A4 \uAE38\uC744 \uC787\uB294 \uC911\u2026" : "\uC5EC\uD589 \uACBD\uB85C \uCC3E\uAE30", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" })), (!fromStop || !toStop) && /* @__PURE__ */ React.createElement("small", { className: "search-help" }, "\uC815\uB958\uC7A5\uBA85\uC744 \uC785\uB825\uD558\uACE0 \uC804\uAD6D \uBAA9\uB85D\uC5D0\uC11C \uCD9C\uBC1C\xB7\uB3C4\uCC29\uC744 \uAC01\uAC01 \uC120\uD0DD\uD558\uC138\uC694."))), error && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "warning-circle", title: "\uACBD\uB85C\uB97C \uCC3E\uC9C0 \uBABB\uD588\uC5B4\uC694" }, error), alternativesTruncated && /* @__PURE__ */ React.createElement("p", { className: "alternative-hint" }, "\uAC80\uC99D\uC744 \uB9C8\uCE5C \uC989\uC2DC \uACB0\uACFC ", formatCount(returnedAlternativeCount), "/", formatCount(requestedAlternativeCount), "\uAC74\uB9CC \uBA3C\uC800 \uD45C\uC2DC\uD569\uB2C8\uB2E4."), structuralCandidates.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "generated-journeys structural-results" }, /* @__PURE__ */ React.createElement("div", { className: "catalog-heading" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uC5EC\uD589 \uACBD\uB85C"), /* @__PURE__ */ React.createElement("h2", null, structuralCandidates.length, "\uAC00\uC9C0 \uAE38\uC744 \uCC3E\uC558\uC5B4\uC694"))), structuralCandidates.map((candidate, index) => /* @__PURE__ */ React.createElement(JourneyCandidateCard, { key: `structural-${candidate.id || candidate.criterion || "candidate"}-${index}`, candidate, index, schedule, structural: true, context: journeyContext, connection, fetchedWindows, onChooseJourney }))), scheduled.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "generated-journeys scheduled-results" }, /* @__PURE__ */ React.createElement("div", { className: "catalog-heading" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uD604\uC7AC \uC2DC\uAC04\uD45C \uD655\uC778 \uACBD\uB85C"), /* @__PURE__ */ React.createElement("h2", null, "\uCD9C\uBC1C\uC2DC\uAC01\uAE4C\uC9C0 \uD655\uC778\uD588\uC5B4\uC694")), /* @__PURE__ */ React.createElement("span", null, schedule.serviceDate || serviceDate, " \xB7 ", schedule.departureTime || departureTime)), /* @__PURE__ */ React.createElement("p", { className: "alternative-hint" }, "\uD45C\uC2DC\uB41C \uD604\uC7AC \uACF5\uC2DD \uC2DC\uAC04\uD45C \uBC94\uC704\uB9CC \uC0AC\uC6A9\uD569\uB2C8\uB2E4. \uACFC\uAC70 GTFS \uC2DC\uAC01\uC774\uB098 \uC784\uC758 \uC131\uACF5\uB960\uC740 \uC11E\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4."), scheduled.map((candidate, index) => /* @__PURE__ */ React.createElement(JourneyCandidateCard, { key: `scheduled-${candidate.id || candidate.criterion || "candidate"}-${index}`, candidate, index, schedule, context: journeyContext, connection, fetchedWindows, onChooseJourney }))), result && structuralCandidates.length === 0 && scheduled.length === 0 && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "map-trifold", title: emptyTitle }, emptyMessage), checkTime && result && !schedule.ready && structuralCandidates.length > 0 && /* @__PURE__ */ React.createElement("p", { className: "timing-help" }, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), " \uACBD\uB85C\uB294 \uCC3E\uC558\uC9C0\uB9CC \uC774 \uAD6C\uAC04\uC758 \uCD9C\uBC1C \uC2DC\uAC04\uC740 \uC544\uC9C1 \uD655\uC778 \uC911\uC774\uC5D0\uC694."));
}
function NationwideScreen({ connection, onChooseJourney }) {
  const [seededStop, setSeededStop] = useState(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  const adminEnabled = new URLSearchParams(window.location.search).get("admin") === "1";
  return /* @__PURE__ */ React.createElement("main", { className: "screen nationwide-screen" }, /* @__PURE__ */ React.createElement(JourneyGenerator, { seededStop, onChooseJourney, connection }), adminEnabled && /* @__PURE__ */ React.createElement("details", { className: "route-admin-tools", open: toolsOpen, onToggle: (event) => setToolsOpen(event.currentTarget.open) }, /* @__PURE__ */ React.createElement("summary", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "wrench" }), /* @__PURE__ */ React.createElement("strong", null, "\uB178\uC120 \uB370\uC774\uD130 \uB3C4\uAD6C"), /* @__PURE__ */ React.createElement("small", null, "\uC6B4\uC601\xB7\uAC80\uC99D\uC6A9")), /* @__PURE__ */ React.createElement(Icon, { name: "caret-down" })), toolsOpen && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "route-admin-intro" }, /* @__PURE__ */ React.createElement("p", null, "\uAC1C\uBCC4 \uB178\uC120 \uC870\uD68C\uC640 \uACBD\uC720 \uC21C\uC11C \uC810\uAC80\uC6A9 \uD654\uBA74\uC785\uB2C8\uB2E4.")), /* @__PURE__ */ React.createElement(RouteBrowser, { connection, onUseStop: setSeededStop }))));
}
