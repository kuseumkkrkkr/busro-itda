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
  const primaryStatus = nationwideComplete ? "\uC804\uAD6D \uACBD\uB85C\uB9DD \uC5F0\uACB0\uB428" : graphReady ? "\uACF5\uC2DD \uAC80\uC99D \uAD6C\uAC04 \uC5F0\uACB0\uB428" : "\uC804\uAD6D \uACBD\uB85C\uB9DD \uC900\uBE44 \uC911";
  const catalogSummary = stopRows && routeRows ? `\uC815\uB958\uC7A5 ${formatCount(stopRows)} \xB7 \uB178\uC120 ${formatCount(routeRows)}` : "\uC804\uAD6D \uBAA9\uB85D DATA_GAP";
  const topologySummary = activeRoutes ? `\uBC29\uD5A5 \uB178\uC120 ${formatCount(activeRoutes)} \xB7 \uADF8\uB798\uD504 \uC815\uB958\uC7A5 ${formatCount(activeStops)}` : topologyTargets ? `\uBC29\uD5A5 \uC21C\uC11C ${formatCount(topologyComplete)}/${formatCount(topologyTargets)}` : "\uBC29\uD5A5 \uC21C\uC11C DATA_GAP";
  return /* @__PURE__ */ React.createElement("div", { className: `graph-coverage ${graphReady ? "catalog-ready" : "catalog-gap"}` }, /* @__PURE__ */ React.createElement("span", { className: "graph-pulse", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("strong", null, primaryStatus), /* @__PURE__ */ React.createElement("small", null, catalogSummary, " \xB7 ", topologySummary)), /* @__PURE__ */ React.createElement("span", { className: "graph-method" }, "\uB2E8\uBC29\uD5A5 Dijkstra"), graphReady && !nationwideComplete && /* @__PURE__ */ React.createElement("small", { className: "coverage-query" }, "\uACF5\uC2DD \uC9C0\uC790\uCCB4 \uC790\uB8CC ", formatCount(activeCities), "\uAC1C \uC9C0\uC5ED\uBD80\uD130 \uC2E4\uC81C \uBC29\uD5A5\uC73C\uB85C \uAC80\uC0C9\uD569\uB2C8\uB2E4. \uC804\uAD6D \uD655\uB300 \uC911\uC785\uB2C8\uB2E4."), !graphReady && /* @__PURE__ */ React.createElement("small", { className: "coverage-gap" }, "TAGO \uB178\uC120\uBCC4 \uACBD\uC720 \uC21C\uC11C\uC758 \uC804\uAD6D \uC801\uC7AC\uAC00 \uB05D\uB098\uC9C0 \uC54A\uC544, \uD655\uC778\uB41C \uAD6C\uAC04\uB9CC \uAC80\uC0C9\uD569\uB2C8\uB2E4."), graph && /* @__PURE__ */ React.createElement("small", { className: topologyReady ? "coverage-query" : "coverage-gap" }, "\uC774\uBC88 \uAC80\uC0C9: ", formatCount(graph.nodes), "\uAC1C \uC0C1\uD0DC \xB7 ", formatCount(graph.edges), "\uAC1C \uC2B9\uCC28 \uAC04\uC120 \xB7 ", graph.algorithm || "directed_dijkstra"), graph && !topologyReady && /* @__PURE__ */ React.createElement("small", { className: "coverage-gap" }, "DATA_GAP \xB7 \uAC80\uC0C9 \uAC00\uB2A5\uD55C \uAC80\uC99D \uB178\uC120 \uC21C\uC11C\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4."));
}
function JourneyGenerator({ seededStop, onChooseJourney, connection }) {
  const [fromText, setFromText] = useState("");
  const [toText, setToText] = useState("");
  const [fromStop, setFromStop] = useState(null);
  const [toStop, setToStop] = useState(null);
  const [preference, setPreference] = useState("diverse");
  const [result, setResult] = useState(null);
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
      setResult(await BusroApi.generateJourneys({ from_stop_id: fromStop.node_id, to_stop_id: toStop.node_id, from_city_code: fromStop.city_code || void 0, to_city_code: toStop.city_code || void 0, preference, max_alternatives: 1 }));
    } catch (reason) {
      setError(reason.message || "\uD604\uC7AC \uC801\uC7AC\uB41C \uB178\uC120 \uADF8\uB798\uD504\uB85C \uC5EC\uD589\uC744 \uB9CC\uB4E4\uC9C0 \uBABB\uD588\uC2B5\uB2C8\uB2E4.");
    } finally {
      setLoading(false);
    }
  }
  const candidates = result?.alternatives || result?.candidates || result?.journeys || [];
  const criterionLabels = {
    minimum_transfers: "\uCD5C\uC18C \uD658\uC2B9",
    generalized_cost: "\uADE0\uD615 \uACBD\uB85C",
    explorer: "\uD0D0\uD5D8 \uACBD\uB85C"
  };
  const gapReasons = {
    STOP_NOT_IN_HYDRATED_SEQUENCE: "\uC120\uD0DD\uD55C \uC815\uB958\uC7A5\uC740 \uC804\uAD6D \uBAA9\uB85D\uC5D0 \uC788\uC9C0\uB9CC \uAC80\uC99D\uB41C \uB178\uC120 \uC21C\uC11C \uADF8\uB798\uD504\uC5D0\uB294 \uC544\uC9C1 \uD3EC\uD568\uB418\uC9C0 \uC54A\uC558\uC2B5\uB2C8\uB2E4.",
    NO_DIRECTED_PATH_IN_HYDRATED_GRAPH: "\uD604\uC7AC \uAC80\uC99D \uADF8\uB798\uD504\uC5D0\uC11C \uCD9C\uBC1C \uBC29\uD5A5\uBD80\uD130 \uB3C4\uCC29 \uBC29\uD5A5\uAE4C\uC9C0 \uC774\uC5B4\uC9C0\uB294 \uACBD\uB85C\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4. \uC5ED\uBC29\uD5A5 \uAC04\uC120\uC744 \uC784\uC758\uB85C \uB9CC\uB4E4\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.",
    EVIDENCE_INCOMPLETE: "\uACBD\uB85C \uAD6C\uC870\uB294 \uCC3E\uC558\uC9C0\uB9CC \uC2DC\uAC04\uD45C \uB610\uB294 \uD1B5\uACFC \uC774\uB825\uC774 \uBD80\uC871\uD569\uB2C8\uB2E4."
  };
  return /* @__PURE__ */ React.createElement("section", { className: "journey-generator" }, /* @__PURE__ */ React.createElement(GlassCard, { className: "generator-card" }, /* @__PURE__ */ React.createElement("div", { className: "generator-heading" }, /* @__PURE__ */ React.createElement("div", { className: "generator-kicker" }, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uC804\uAD6D \uBC84\uC2A4 \uC5EC\uD589"), /* @__PURE__ */ React.createElement(SourceBadge, { mode: connection.mode, label: connection.label })), /* @__PURE__ */ React.createElement("h1", null, "\uC5B4\uB514\uAE4C\uC9C0 \uAC00\uC138\uC694?"), /* @__PURE__ */ React.createElement("p", null, "\uCD9C\uBC1C\uC9C0\uC640 \uB3C4\uCC29\uC9C0\uB9CC \uACE0\uB974\uBA74, \uC804\uAD6D \uB178\uC120\uC758 \uC2E4\uC81C \uC9C4\uD589 \uBC29\uD5A5\uC744 \uB530\uB77C \uAE38\uC744 \uCC3E\uC544\uB4DC\uB824\uC694.")), /* @__PURE__ */ React.createElement("form", { onSubmit: generate }, /* @__PURE__ */ React.createElement("div", { className: "route-point-sheet" }, /* @__PURE__ */ React.createElement("div", { className: "route-point origin" }, /* @__PURE__ */ React.createElement("span", { className: "point-mark", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement(StopLookup, { label: "\uCD9C\uBC1C", value: fromText, onChange: setFromText, selected: fromStop, onSelect: setFromStop })), /* @__PURE__ */ React.createElement("div", { className: "route-point destination" }, /* @__PURE__ */ React.createElement("span", { className: "point-mark", "aria-hidden": "true" }), /* @__PURE__ */ React.createElement(StopLookup, { label: "\uB3C4\uCC29", value: toText, onChange: setToText, selected: toStop, onSelect: setToStop })), /* @__PURE__ */ React.createElement("button", { className: "generator-swap", type: "button", onClick: () => {
    setFromStop(toStop);
    setToStop(fromStop);
    setFromText(toText);
    setToText(fromText);
  }, "aria-label": "\uCD9C\uBC1C\uACFC \uB3C4\uCC29 \uBC14\uAFB8\uAE30" }, /* @__PURE__ */ React.createElement(Icon, { name: "arrows-down-up" }))), /* @__PURE__ */ React.createElement("fieldset", null, /* @__PURE__ */ React.createElement("legend", null, "\uC5B4\uB5A4 \uAE38\uB85C \uAC08\uAE4C\uC694?"), /* @__PURE__ */ React.createElement("div", { className: "preference-grid" }, [["diverse", "\uCD94\uCC9C", "sparkle"], ["low_transfer", "\uCD5C\uC18C \uD658\uC2B9", "arrows-left-right"], ["reliable", "\uADFC\uAC70 \uC6B0\uC120", "shield-check"], ["challenge", "\uAD6D\uD1A0\uC885\uC8FC", "flag-banner"]].map(([value, label, icon]) => /* @__PURE__ */ React.createElement("button", { type: "button", key: value, className: preference === value ? "active" : "", onClick: () => setPreference(value) }, /* @__PURE__ */ React.createElement(Icon, { name: icon }), label)))), /* @__PURE__ */ React.createElement("button", { className: "liquid-button route-search-primary", type: "submit", disabled: !fromStop || !toStop || loading }, loading ? "\uC804\uAD6D \uB178\uC120\uC5D0\uC11C \uCC3E\uB294 \uC911\u2026" : "\uACBD\uB85C \uCC3E\uAE30", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" })), (!fromStop || !toStop) && /* @__PURE__ */ React.createElement("small", { className: "search-help" }, "\uC815\uB958\uC7A5\uBA85\uC744 \uC785\uB825\uD558\uACE0 \uC804\uAD6D \uBAA9\uB85D\uC5D0\uC11C \uCD9C\uBC1C\xB7\uB3C4\uCC29\uC744 \uAC01\uAC01 \uC120\uD0DD\uD558\uC138\uC694."))), /* @__PURE__ */ React.createElement(GraphCoverage, { networkStatus, result }), error && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "warning-circle", title: "DATA_GAP" }, error, " \uAC80\uC99D\uB41C \uB178\uC120 \uACBD\uC720 \uC815\uB958\uC7A5\uC774 \uC801\uC7AC\uB418\uC5B4\uC57C \uACBD\uB85C\uC5D0 \uD3EC\uD568\uB429\uB2C8\uB2E4."), result && candidates.length === 0 && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "warning-circle", title: result.status || "DATA_GAP" }, gapReasons[result.reason] || result.reason || "\uC0DD\uC131 \uAC00\uB2A5\uD55C \uBC29\uD5A5\uC131 \uACBD\uB85C\uAC00 \uC801\uC7AC\uB41C \uADF8\uB798\uD504\uC5D0 \uC5C6\uC2B5\uB2C8\uB2E4."), candidates.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "generated-journeys" }, /* @__PURE__ */ React.createElement("div", { className: "catalog-heading" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uC120\uD0DD \uAE30\uC900 \uACBD\uB85C"), /* @__PURE__ */ React.createElement("h2", null, candidates.length, "\uAC00\uC9C0 \uAE38\uC744 \uCC3E\uC558\uC5B4\uC694")), /* @__PURE__ */ React.createElement("span", null, "\uBE60\uB978 1\uCC28 \uAC80\uC0C9")), /* @__PURE__ */ React.createElement("p", { className: "alternative-hint" }, "\uB2E4\uB978 \uC5EC\uD589 \uC885\uB958\uB294 \uC704 \uAE30\uC900\uC744 \uBC14\uAFD4 \uB2E4\uC2DC \uCC3E\uC544\uBCF4\uC138\uC694."), candidates.map((candidate, index) => {
    const routeIds = Array.isArray(candidate.route_ids) ? candidate.route_ids.filter(Boolean) : [];
    const legs = summarizeJourneyLegs(candidate);
    const coverage = candidate.coverage && typeof candidate.coverage === "object" ? candidate.coverage : {};
    const evidence = candidate.evidence && typeof candidate.evidence === "object" ? candidate.evidence : {};
    const hasProbability = typeof candidate.success_probability === "number" && Number.isFinite(candidate.success_probability);
    return /* @__PURE__ */ React.createElement("article", { key: `${candidate.criterion || "candidate"}-${routeIds.join("-")}-${index}` }, /* @__PURE__ */ React.createElement("div", { className: "candidate-rank" }, index + 1), /* @__PURE__ */ React.createElement("div", { className: "candidate-copy" }, /* @__PURE__ */ React.createElement("div", { className: "candidate-title" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", null, criterionLabels[candidate.criterion] || candidate.criterion || "\uACBD\uB85C \uD6C4\uBCF4"), /* @__PURE__ */ React.createElement("h3", null, routeIds.length > 0 ? `${candidate.transfers || 0}\uD68C \uD658\uC2B9 \xB7 ${routeIds.length}\uAC1C \uB178\uC120` : "\uB178\uC120 DATA_GAP")), /* @__PURE__ */ React.createElement("small", null, candidate.status || "DATA_GAP")), /* @__PURE__ */ React.createElement("div", { className: "candidate-leg-list" }, legs.map((leg, legIndex) => /* @__PURE__ */ React.createElement("div", { className: "candidate-leg", key: `${leg.routeId}-${legIndex}` }, /* @__PURE__ */ React.createElement("span", { className: "timeline-rail" }, /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("b", null)), /* @__PURE__ */ React.createElement("div", { className: "leg-copy" }, /* @__PURE__ */ React.createElement("span", { className: "route-pill" }, /* @__PURE__ */ React.createElement(Icon, { name: "bus" }), " ", leg.routeId), /* @__PURE__ */ React.createElement("strong", null, leg.from?.node_name || leg.from?.node_id || "\uC2B9\uCC28 \uC815\uB958\uC7A5"), /* @__PURE__ */ React.createElement("small", null, "\uCD1D ", leg.edgeCount + 1, "\uAC1C \uC815\uB958\uC7A5"), /* @__PURE__ */ React.createElement("strong", null, leg.to?.node_name || leg.to?.node_id || "\uD558\uCC28 \uC815\uB958\uC7A5"))))), /* @__PURE__ */ React.createElement("footer", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "arrows-left-right" }), " ", typeof candidate.transfers === "number" ? `${candidate.transfers}\uD68C \uD658\uC2B9` : "\uD658\uC2B9 DATA_GAP"), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "database" }), " \uC2B9\uCC28 ", evidence.ride_edges ?? "\u2014", " \xB7 \uD658\uC2B9 ", evidence.transfer_edges ?? "\u2014", " \uAC04\uC120"), /* @__PURE__ */ React.createElement("strong", null, hasProbability ? `\uC131\uACF5\uB960 ${Math.round(candidate.success_probability * 100)}%` : "\uC131\uACF5\uB960 DATA_GAP")), typeof coverage.schedule_routes === "number" && typeof coverage.total_routes === "number" && /* @__PURE__ */ React.createElement("small", { className: "evidence-copy" }, "\uC2DC\uAC04\uD45C \uADFC\uAC70 ", coverage.schedule_routes, "/", coverage.total_routes, typeof coverage.passage_routes === "number" ? ` \xB7 \uD1B5\uACFC \uC774\uB825 ${coverage.passage_routes}/${coverage.total_routes}` : ""), /* @__PURE__ */ React.createElement("button", { className: "open-candidate", type: "button", onClick: () => onChooseJourney?.({ ...candidate, from_stop: fromStop, to_stop: toStop, preference }) }, "\uACBD\uB85C \uC790\uC138\uD788 \uBCF4\uAE30 ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" }))));
  })));
}
function NationwideScreen({ connection, onChooseJourney }) {
  const [seededStop, setSeededStop] = useState(null);
  const [toolsOpen, setToolsOpen] = useState(false);
  return /* @__PURE__ */ React.createElement("main", { className: "screen nationwide-screen" }, /* @__PURE__ */ React.createElement(JourneyGenerator, { seededStop, onChooseJourney, connection }), /* @__PURE__ */ React.createElement("details", { className: "route-admin-tools", open: toolsOpen, onToggle: (event) => setToolsOpen(event.currentTarget.open) }, /* @__PURE__ */ React.createElement("summary", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "wrench" }), /* @__PURE__ */ React.createElement("strong", null, "\uB178\uC120 \uB370\uC774\uD130 \uB3C4\uAD6C"), /* @__PURE__ */ React.createElement("small", null, "\uC6B4\uC601\xB7\uAC80\uC99D\uC6A9")), /* @__PURE__ */ React.createElement(Icon, { name: "caret-down" })), toolsOpen && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "route-admin-intro" }, /* @__PURE__ */ React.createElement("p", null, "\uAC1C\uBCC4 TAGO \uB178\uC120 \uC870\uD68C\xB7OSM \uD615\uC0C1\xB7\uACBD\uC720\uC21C\uC11C \uC801\uC7AC\uB294 \uB370\uC774\uD130 \uC810\uAC80\uC6A9\uC785\uB2C8\uB2E4. \uC5EC\uD589\uC790\uB294 \uC704 \uC804\uAD6D \uACBD\uB85C \uAC80\uC0C9\uB9CC \uC0AC\uC6A9\uD558\uBA74 \uB429\uB2C8\uB2E4.")), /* @__PURE__ */ React.createElement(RouteBrowser, { connection, onUseStop: setSeededStop }))));
}
