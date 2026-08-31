function ExploreScreen({ form, setForm, connection, simulation, onSearch, onOpenSimulation, onOpenLive }) {
  const summary = simulation.summary || { probability: null, weakestLeg: "\uC9D1\uACC4 \uC804", coverage: 0 };
  return /* @__PURE__ */ React.createElement("main", { className: "screen explore-screen" }, /* @__PURE__ */ React.createElement("div", { className: "map-atmosphere", "aria-hidden": "true" }, /* @__PURE__ */ React.createElement("div", { className: "terrain terrain-one" }), /* @__PURE__ */ React.createElement("div", { className: "terrain terrain-two" }), /* @__PURE__ */ React.createElement("div", { className: "route-stroke route-one" }), /* @__PURE__ */ React.createElement("div", { className: "route-stroke route-two" }), /* @__PURE__ */ React.createElement("span", { className: "map-node node-one" }), /* @__PURE__ */ React.createElement("span", { className: "map-node node-two" }), /* @__PURE__ */ React.createElement("span", { className: "map-node node-three" }), /* @__PURE__ */ React.createElement("p", { className: "map-label label-one" }, "\uC138\uC885"), /* @__PURE__ */ React.createElement("p", { className: "map-label label-two" }, "\uC601\uCC9C"), /* @__PURE__ */ React.createElement("p", { className: "map-label label-three" }, "\uBD80\uC0B0")), /* @__PURE__ */ React.createElement("section", { className: "hero-copy" }, /* @__PURE__ */ React.createElement(SourceBadge, { mode: connection.mode, label: connection.label }), /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uC624\uB298\uC758 \uB85C\uCEEC\uBC84\uC2A4 \uC6D0\uC815"), /* @__PURE__ */ React.createElement("h1", null, "\uB3C4\uC2DC\uC640 \uB3C4\uC2DC \uC0AC\uC774,", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "\uBC84\uC2A4\uB9CC\uC73C\uB85C"), " \uC787\uB2E4.")), /* @__PURE__ */ React.createElement(GlassCard, { className: "journey-search", "aria-label": "\uC5EC\uC815 \uAC80\uC0C9" }, /* @__PURE__ */ React.createElement("label", { className: "place-field" }, /* @__PURE__ */ React.createElement("span", { className: "place-dot start" }), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("small", null, "\uCD9C\uBC1C"), /* @__PURE__ */ React.createElement("input", { value: form.from, onChange: (event) => setForm({ ...form, from: event.target.value }), "aria-label": "\uCD9C\uBC1C\uC9C0" }))), /* @__PURE__ */ React.createElement("button", { className: "swap-button", type: "button", "aria-label": "\uCD9C\uBC1C\uC9C0\uC640 \uB3C4\uCC29\uC9C0 \uBC14\uAFB8\uAE30", onClick: () => setForm({ ...form, from: form.to, to: form.from }) }, /* @__PURE__ */ React.createElement(Icon, { name: "arrows-down-up" })), /* @__PURE__ */ React.createElement("label", { className: "place-field" }, /* @__PURE__ */ React.createElement("span", { className: "place-dot end" }), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("small", null, "\uB3C4\uCC29"), /* @__PURE__ */ React.createElement("input", { value: form.to, onChange: (event) => setForm({ ...form, to: event.target.value }), "aria-label": "\uB3C4\uCC29\uC9C0" }))), /* @__PURE__ */ React.createElement("div", { className: "search-meta" }, /* @__PURE__ */ React.createElement("button", { type: "button" }, /* @__PURE__ */ React.createElement(Icon, { name: "calendar-blank" }), " \uB0A0\uC9DC \uC120\uD0DD"), /* @__PURE__ */ React.createElement("button", { type: "button" }, /* @__PURE__ */ React.createElement(Icon, { name: "clock" }), " \uC2DC\uAC01 \uC120\uD0DD"), /* @__PURE__ */ React.createElement("button", { className: "search-submit", type: "button", onClick: onSearch, disabled: !form.from.trim() || !form.to.trim(), "aria-label": "\uACBD\uB85C \uAC80\uC0C9" }, /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" })))), /* @__PURE__ */ React.createElement(GlassCard, { className: "probability-hero" }, /* @__PURE__ */ React.createElement("div", { className: "probability-copy" }, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "OBSERVED CONNECTIONS"), /* @__PURE__ */ React.createElement("h2", null, "\uC2E4\uC81C\uB85C \uC774\uC5B4\uC9C4", /* @__PURE__ */ React.createElement("br", null), "\uB0A0\uC9DC\uBCC4 \uAE30\uB85D"), /* @__PURE__ */ React.createElement("p", null, summary.coverage > 0 ? `\uC2E4\uC81C \uC801\uC7AC \uC774\uB825 ${summary.coverage}\uAC74 \uAE30\uBC18` : "\uC544\uC9C1 \uC2E4\uC81C \uC774\uB825 \uC5C6\uC74C \xB7 \uD655\uB960 \uBBF8\uC0B0\uCD9C"), /* @__PURE__ */ React.createElement("button", { className: "text-link", type: "button", onClick: onOpenSimulation }, "\uB0A0\uC9DC\uBCC4 \uACB0\uACFC \uBCF4\uAE30 ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-up-right" }))), /* @__PURE__ */ React.createElement(ProbabilityRing, { value: Number.isFinite(summary.probability) ? summary.probability : null })), /* @__PURE__ */ React.createElement("div", { className: "quick-grid" }, /* @__PURE__ */ React.createElement("button", { className: "mini-glass", type: "button", onClick: onOpenLive }, /* @__PURE__ */ React.createElement("span", { className: "mini-icon live" }, /* @__PURE__ */ React.createElement(Icon, { name: "broadcast" })), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("small", null, "\uB2E4\uC74C \uBC84\uC2A4"), /* @__PURE__ */ React.createElement("strong", null, "\uB3C4\uCC29\uC815\uBCF4 \uD655\uC778")), /* @__PURE__ */ React.createElement(Icon, { name: "caret-right" })), /* @__PURE__ */ React.createElement("button", { className: "mini-glass", type: "button", onClick: onOpenSimulation }, /* @__PURE__ */ React.createElement("span", { className: "mini-icon sim" }, /* @__PURE__ */ React.createElement(Icon, { name: "waveform" })), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("small", null, "\uCDE8\uC57D \uAD6C\uAC04"), /* @__PURE__ */ React.createElement("strong", null, summary.weakestLeg)), /* @__PURE__ */ React.createElement(Icon, { name: "caret-right" }))));
}
function LiveScreen({ journey, connection, legs, selectedLeg, setSelectedLeg, arrivals, history, passageCoverage, mappingSummary, loading, error, notice, onRefresh, onCollect, onExplore }) {
  if (!journey || legs.length === 0) {
    const directWithoutTransfer = journeyUsesCurrentTimetable(journey) && Number(journey?.transfers) === 0;
    return /* @__PURE__ */ React.createElement("main", { className: "screen content-screen" }, /* @__PURE__ */ React.createElement(ScreenHeading, { eyebrow: "\uC2E4\uC2DC\uAC04", title: directWithoutTransfer ? "\uD658\uC2B9 \uAD00\uCE21 \uAD6C\uAC04\uC774 \uC5C6\uC2B5\uB2C8\uB2E4" : "\uC870\uD68C\uD560 \uC5EC\uD589\uC774 \uC5C6\uC2B5\uB2C8\uB2E4", detail: directWithoutTransfer ? "\uC9C1\uD1B5 \uACBD\uB85C\uC5D0\uB294 \uD658\uC2B9 \uCCB4\uD06C\uD3EC\uC778\uD2B8\uAC00 \uC5C6\uC5B4 \uC5F0\uACB0 \uC774\uB825 \uC218\uC9D1 \uB300\uC0C1\uC774 \uC544\uB2D9\uB2C8\uB2E4." : journey ? "\uC120\uD0DD\uD55C \uD6C4\uBCF4\uC5D0 \uAC80\uC99D\uB41C \uD658\uC2B9 \uCCB4\uD06C\uD3EC\uC778\uD2B8\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4." : "\uC804\uAD6D \uD0D0\uC0C9\uC5D0\uC11C \uC2E4\uC81C \uC0DD\uC131\uB41C \uC5EC\uD589 \uD6C4\uBCF4\uB97C \uBA3C\uC800 \uC120\uD0DD\uD558\uC138\uC694." }), /* @__PURE__ */ React.createElement(GlassCard, { className: "stop-board" }, /* @__PURE__ */ React.createElement(InlineNotice, { tone: directWithoutTransfer ? "neutral" : "warning", icon: directWithoutTransfer ? "minus-circle" : "map-trifold", title: directWithoutTransfer ? "\uC9C1\uD1B5 \xB7 \uC5F0\uACB0 \uAD00\uCE21 \uBE44\uB300\uC0C1" : "DATA_GAP \xB7 \uC804\uAD6D \uC5EC\uD589 \uD6C4\uBCF4 \uD544\uC694" }, directWithoutTransfer ? "\uD658\uC2B9 \uC131\uACF5\xB7\uC2E4\uD328 \uD310\uC815\uC744 \uC704\uD55C \uB3C4\uCC29 \uAD00\uCE21\uC740 \uB9CC\uB4E4\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4." : "\uAE30\uBCF8 \uACE0\uC815 \uACBD\uB85C\uB098 \uC0D8\uD50C \uB3C4\uCC29\uC815\uBCF4\uB85C \uB300\uC2E0 \uD45C\uC2DC\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.")), /* @__PURE__ */ React.createElement("button", { className: "liquid-button sticky-action", type: "button", onClick: onExplore }, "\uC804\uAD6D \uD0D0\uC0C9\uC73C\uB85C \uAC00\uAE30 ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" })));
  }
  const leg = legs.find((item) => item.id === selectedLeg) || legs[0];
  const values = history;
  const maxDelay = Math.max(12, ...values.map((item) => Number(item.delay || item.delay_minutes || 0)));
  return /* @__PURE__ */ React.createElement("main", { className: "screen content-screen" }, /* @__PURE__ */ React.createElement(ScreenHeading, { eyebrow: "\uC2E4\uC2DC\uAC04", title: "\uB3C4\uCC29\uC815\uBCF4", detail: "\uD604\uC7AC \uB3C4\uCC29\uC815\uBCF4\uC640 \uC800\uC7A5\uB41C \uAD00\uCE21 \uAE30\uB85D\uC744 \uD655\uC778\uD569\uB2C8\uB2E4.", action: /* @__PURE__ */ React.createElement("button", { className: `refresh-button ${loading ? "spinning" : ""}`, type: "button", onClick: onRefresh, disabled: loading, "aria-label": "\uB3C4\uCC29\uC815\uBCF4 \uC0C8\uB85C\uACE0\uCE68" }, /* @__PURE__ */ React.createElement(Icon, { name: "arrows-clockwise" })) }), /* @__PURE__ */ React.createElement(InlineNotice, { tone: connection.mode === "live" ? "success" : "warning", icon: connection.mode === "live" ? "cloud-check" : "key", title: connection.mode === "live" ? "TAGO \uACF5\uC2DD \uC751\uB2F5 \uC5F0\uACB0\uB428" : connection.mode === "ready" ? "\uACF5\uC2DD \uB370\uC774\uD130 \uC5F0\uACB0\uB428 \xB7 LIVE \uC544\uB2D8" : connection.mode === "fixture" ? "\uD604\uC7AC\uB294 FIXTURE \uBAA8\uB4DC" : "\uACF5\uC2DD \uB370\uC774\uD130 \uC5F0\uACB0 \uB300\uAE30" }, connection.message), /* @__PURE__ */ React.createElement(CoverageStrip, { mappingSummary, coverage: passageCoverage }), /* @__PURE__ */ React.createElement("div", { className: "stop-chips", role: "list", "aria-label": "\uC870\uD68C\uD560 \uC5EC\uC815 \uAD6C\uAC04" }, legs.map((item) => /* @__PURE__ */ React.createElement("button", { role: "listitem", type: "button", key: item.id, className: selectedLeg === item.id ? "active" : "", onClick: () => setSelectedLeg(item.id) }, /* @__PURE__ */ React.createElement("small", null, item.city), /* @__PURE__ */ React.createElement("strong", null, item.routeNo), /* @__PURE__ */ React.createElement("i", { className: `mapping-dot ${item.mappingState}`, "aria-label": item.mappingState === "verified" ? "\uAC80\uC99D\uB428" : item.mappingState === "checking" ? "\uAC80\uC99D\uC911" : "\uBBF8\uB9E4\uD551" })))), /* @__PURE__ */ React.createElement("div", { className: "mapping-context" }, /* @__PURE__ */ React.createElement(MappingBadge, { state: leg.mappingState }), /* @__PURE__ */ React.createElement("p", null, leg.apiMapped ? "\uC774 \uAD6C\uAC04\uC758 \uACF5\uC2DD cityCode \xB7 nodeId \xB7 routeId\uAC00 \uAC80\uC99D\uB410\uC2B5\uB2C8\uB2E4." : "\uACF5\uC2DD \uC2DD\uBCC4\uC790\uAC00 \uAC80\uC99D\uB418\uAE30 \uC804\uC5D0\uB294 \uC774 \uAD6C\uAC04\uC744 LIVE\uB85C \uD45C\uC2DC\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.")), notice && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "success", icon: "database", title: "\uC774\uB825 \uC800\uC7A5" }, notice), /* @__PURE__ */ React.createElement(GlassCard, { className: "stop-board" }, /* @__PURE__ */ React.createElement("div", { className: "stop-board-head" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, leg.city, " \xB7 ", leg.routeNo), /* @__PURE__ */ React.createElement("h2", null, leg.board), /* @__PURE__ */ React.createElement("p", null, leg.alight, " \uBC29\uBA74 \xB7 ", leg.transferCheckpoint ? "\uD658\uC2B9 \uB3C4\uCC29 \uC21C\uBC88" : "\uC2B9\uCC28 \uC21C\uBC88", " ", Number.isInteger(leg.nodeOrder) ? leg.nodeOrder : "DATA_GAP")), /* @__PURE__ */ React.createElement("span", { className: "route-orb" }, leg.routeNo)), loading ? /* @__PURE__ */ React.createElement(LoadingRows, { count: 2 }) : /* @__PURE__ */ React.createElement("div", { className: "arrival-list" }, error && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "warning-circle", title: "\uC2E4\uC2DC\uAC04 \uB370\uC774\uD130 \uC5C6\uC74C" }, error), arrivals.length ? arrivals.map((arrival, index) => /* @__PURE__ */ React.createElement("article", { className: "arrival-row", key: `${arrival.routeNo || arrival.route_no}-${index}` }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, index === 0 ? "\uACE7 \uB3C4\uCC29" : "\uB2E4\uC74C \uBC84\uC2A4"), /* @__PURE__ */ React.createElement("strong", null, arrival.minutes ?? arrival.arrival_minutes, /* @__PURE__ */ React.createElement("span", null, "\uBD84"))), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", null, arrival.stops ?? arrival.remaining_stops, "\uAC1C \uC815\uB958\uC7A5 \uC804"), /* @__PURE__ */ React.createElement("small", null, arrival.vehicleNo || arrival.vehicle_no || "\uCC28\uB7C9\uBC88\uD638 \uBBF8\uC81C\uACF5")), /* @__PURE__ */ React.createElement(SourceBadge, { mode: connection.mode, label: connection.mode === "ready" ? "\uACF5\uC2DD \uB9E4\uD551 \uAD6C\uAC04" : void 0 }))) : /* @__PURE__ */ React.createElement("div", { className: "empty-mini" }, /* @__PURE__ */ React.createElement(Icon, { name: "bus" }), /* @__PURE__ */ React.createElement("p", null, "\uD604\uC7AC \uC81C\uACF5\uB41C \uB3C4\uCC29\uC815\uBCF4\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4."))), /* @__PURE__ */ React.createElement("div", { className: "board-actions" }, /* @__PURE__ */ React.createElement("button", { type: "button", onClick: onCollect, disabled: connection.mode !== "live" || !leg.apiMapped || !connection.collectionReady }, /* @__PURE__ */ React.createElement(Icon, { name: "database" }), " \uB3C4\uCC29\xB7\uCC28\uB7C9 \uC704\uCE58 \uC774\uB825 \uC800\uC7A5"), /* @__PURE__ */ React.createElement("small", null, connection.collectionReady ? "\uBA85\uC2DC\uC801\uC73C\uB85C \uB204\uB978 TAGO LIVE \uC751\uB2F5\uB9CC \uAD00\uCE21\uC2DC\uAC01\xB7\uC6D0\uBB38 \uD574\uC2DC\uC640 \uD568\uAED8 \uC800\uC7A5\uD569\uB2C8\uB2E4." : "\uACF5\uC720 \uC774\uB825 \uC800\uC7A5\uC18C\uAC00 \uAC80\uC99D\uB41C \uB85C\uCEEC \uC11C\uBC84\uC5D0\uC11C\uB9CC \uC800\uC7A5\uD560 \uC218 \uC788\uC2B5\uB2C8\uB2E4."))), /* @__PURE__ */ React.createElement(GlassCard, { className: "history-card" }, /* @__PURE__ */ React.createElement("div", { className: "card-title" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uCD5C\uADFC \uAE30\uB85D"), /* @__PURE__ */ React.createElement("h3", null, "\uB3C4\uCC29 \uC608\uC815\uC2DC\uAC04 \uAD00\uCE21")), /* @__PURE__ */ React.createElement("span", null, history.length ? `${history.length}\uAC1C \uC801\uC7AC` : "DATA_GAP")), connection.mode === "live" && history.length === 0 && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "database", title: "DATA_GAP" }, "\uC544\uC9C1 \uC774 \uC815\uB958\uC7A5\uC758 \uC2E4\uC81C \uAD00\uCE21 \uC774\uB825\uC774 \uC5C6\uC2B5\uB2C8\uB2E4. \uC218\uC9D1 \uC2DC\uC791 \uC774\uC804 \uB0A0\uC9DC\uB294 \uC2E4\uD328\uB85C \uACC4\uC0B0\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4."), values.length ? /* @__PURE__ */ React.createElement("div", { className: "history-chart", "aria-label": "\uCD5C\uADFC \uC9C0\uC5F0 \uAD00\uCE21 \uB9C9\uB300 \uADF8\uB798\uD504" }, values.slice(-10).map((item, index) => {
    const delay = Number(item.delay || item.delay_minutes || 0);
    return /* @__PURE__ */ React.createElement("div", { key: `${item.timestamp || item.label || "history"}-${index}` }, /* @__PURE__ */ React.createElement("span", { style: { height: `${Math.max(12, delay / maxDelay * 100)}%` }, className: Number.isFinite(leg.buffer) && delay > leg.buffer ? "risk" : "" }), /* @__PURE__ */ React.createElement("small", null, String(item.label || item.observed_at || index + 1).slice(5, 10)));
  })) : /* @__PURE__ */ React.createElement("div", { className: "history-empty" }, /* @__PURE__ */ React.createElement(Icon, { name: "path" }), /* @__PURE__ */ React.createElement("p", null, "\uD1B5\uACFC \uC774\uB825\uC774 \uC5C6\uC5B4 \uC131\uACF5\xB7\uC2E4\uD328\uB97C \uD310\uC815\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.")), /* @__PURE__ */ React.createElement("p", { className: "chart-note" }, "\uB3C4\uCC29\uC608\uC815\uC2DC\uAC04 \uAD00\uCE21\uAC12", Number.isFinite(leg.buffer) ? ` \xB7 \uAC80\uC99D\uB41C \uD604\uC7AC \uC2DC\uAC04\uD45C \uAE30\uC900 \uD658\uC2B9 \uC5EC\uC720 ${leg.buffer}\uBD84` : " \xB7 \uD604\uC7AC \uC2DC\uAC04\uD45C \uD658\uC2B9 \uC2DC\uAC01 DATA_GAP")));
}
function SimulationScreen({ journey, replayReady, replayApplicability, connection, simulation, days, setDays, passageCoverage, mappingSummary, loading, onRun, onExplore }) {
  if (!journey) {
    return /* @__PURE__ */ React.createElement("main", { className: "screen content-screen simulation-screen" }, /* @__PURE__ */ React.createElement(ScreenHeading, { eyebrow: "\uC774\uB825 \uC7AC\uC0DD", title: "\uC7AC\uC0DD\uD560 \uC5EC\uD589\uC774 \uC5C6\uC2B5\uB2C8\uB2E4", detail: "\uC804\uAD6D \uD0D0\uC0C9\uC5D0\uC11C \uC2E4\uC81C \uC0DD\uC131\uB41C \uC5EC\uD589 \uD6C4\uBCF4\uB97C \uBA3C\uC800 \uC120\uD0DD\uD558\uC138\uC694." }), /* @__PURE__ */ React.createElement(GlassCard, { className: "sim-control" }, /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "map-trifold", title: "DATA_GAP \xB7 \uC804\uAD6D \uC5EC\uD589 \uD6C4\uBCF4 \uD544\uC694" }, "\uAE30\uBCF8 \uACE0\uC815 \uAD6C\uAC04\uC774\uB098 \uC0D8\uD50C \uC131\uACF5\uB960\uB85C \uB300\uC2E0 \uACC4\uC0B0\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.")), /* @__PURE__ */ React.createElement("button", { className: "liquid-button sticky-action", type: "button", onClick: onExplore }, "\uC804\uAD6D \uD0D0\uC0C9\uC73C\uB85C \uAC00\uAE30 ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" })));
  }
  const summary = simulation.summary || { probability: null, successfulDays: 0, totalDays: days, weakestLeg: "\uC9D1\uACC4 \uC804", coverage: 0 };
  const notApplicable = replayApplicability === "not_applicable";
  const allMapped = mappingSummary.total > 0 && mappingSummary.verified === mappingSummary.total;
  const canReplay = !notApplicable && replayReady && connection.mode === "live" && allMapped;
  return /* @__PURE__ */ React.createElement("main", { className: "screen content-screen simulation-screen" }, /* @__PURE__ */ React.createElement(ScreenHeading, { eyebrow: "\uC774\uB825 \uC7AC\uC0DD", title: "\uB0A0\uC9DC\uBCC4 \uC5F0\uACB0 \uACB0\uACFC", detail: "\uAC80\uC99D\uB41C \uD604\uC7AC \uC2DC\uAC04\uD45C\uC640 \uC800\uC7A5\uB41C TAGO \uCC28\uB7C9 \uD1B5\uACFC \uC774\uB825\uC774 \uD568\uAED8 \uC788\uB294 \uB0A0\uC9DC\uB9CC \uD310\uC815\uD569\uB2C8\uB2E4." }), /* @__PURE__ */ React.createElement(CoverageStrip, { mappingSummary, coverage: passageCoverage }), mappingSummary.verified < mappingSummary.total && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "map-pin-line", title: "DATA_GAP \xB7 \uACF5\uC2DD \uB9E4\uD551 \uBBF8\uC644\uB8CC" }, "\uC120\uD0DD \uC5EC\uD589 ", mappingSummary.total, "\uAC1C \uAD6C\uAC04\uC774 \uBAA8\uB450 \uAC80\uC99D\uB418\uAE30 \uC804\uC5D0\uB294 \uB0A0\uC9DC\uBCC4 \uACB0\uACFC\uB97C \uD310\uC815\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4."), notApplicable && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "neutral", icon: "minus-circle", title: "\uD658\uC2B9 \uC5F0\uACB0 \uC2DC\uBBAC\uB808\uC774\uC158 \uBE44\uB300\uC0C1" }, "\uC774 \uD6C4\uBCF4\uB294 \uC9C1\uD1B5 \uACBD\uB85C\uB77C \uD658\uC2B9 \uC5F0\uACB0\uC758 \uC131\uACF5\xB7\uC2E4\uD328\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4. \uC131\uACF5\uB960\uC744 \uB9CC\uB4E4\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4."), !notApplicable && !replayReady && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "clock", title: "DATA_GAP \xB7 \uD604\uC7AC \uD658\uC2B9 \uC2DC\uAC01 \uD544\uC694" }, "\uD6C4\uBCF4\uC5D0 \uAC80\uC99D\uB41C \uD604\uC7AC \uC2DC\uAC04\uD45C \uCD9C\uCC98, \uB3C4\uCC29 \uC608\uC815\uC2DC\uAC01, \uB2E4\uC74C \uCD9C\uBC1C\uC2DC\uAC01, \uCD5C\uC18C \uD658\uC2B9\uC2DC\uAC04\uC774 \uC5C6\uC2B5\uB2C8\uB2E4. \uACFC\uAC70 GTFS \uC2DC\uAC01\uC774\uB098 fixture \uC131\uACF5\uB960\uC744 \uC0AC\uC6A9\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4."), !notApplicable && replayReady && connection.mode !== "live" && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "database", title: "DATA_GAP \xB7 TAGO LIVE \uD544\uC694" }, "\uC2E4\uC81C \uCC28\uB7C9 \uD1B5\uACFC \uC774\uB825\uC774 \uC801\uC7AC\uB41C TAGO LIVE \uC5F0\uACB0 \uB4A4\uC5D0\uB9CC \uC7AC\uC0DD\uD569\uB2C8\uB2E4."), /* @__PURE__ */ React.createElement(GlassCard, { className: "sim-control" }, /* @__PURE__ */ React.createElement("label", null, "\uBD84\uC11D \uAE30\uAC04", /* @__PURE__ */ React.createElement(Segmented, { value: days, onChange: setDays, label: "\uBD84\uC11D \uAE30\uAC04", options: [{ value: 7, label: "7\uC77C" }, { value: 14, label: "14\uC77C" }, { value: 30, label: "30\uC77C" }] })), /* @__PURE__ */ React.createElement("button", { className: "liquid-button", type: "button", onClick: onRun, disabled: loading || !canReplay }, notApplicable ? "\uC9C1\uD1B5 \uACBD\uB85C \xB7 \uC7AC\uC0DD \uBE44\uB300\uC0C1" : loading ? "\uD1B5\uACFC \uC774\uB825 \uC7AC\uC0DD \uC911\u2026" : "\uB0A0\uC9DC\uBCC4 \uC2E4\uC81C \uC774\uB825 \uC7AC\uC0DD", /* @__PURE__ */ React.createElement(Icon, { name: notApplicable ? "minus-circle" : "sparkle" }))), /* @__PURE__ */ React.createElement(GlassCard, { className: "sim-summary" }, /* @__PURE__ */ React.createElement(ProbabilityRing, { value: Number.isFinite(summary.probability) ? summary.probability : null }), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uAD00\uCE21 \uACB0\uACFC"), /* @__PURE__ */ React.createElement("h2", null, notApplicable ? "\uBE44\uB300\uC0C1" : summary.dataGap ? "\uC790\uB8CC \uBD80\uC871" : summary.successfulDays, /* @__PURE__ */ React.createElement("span", null, notApplicable ? " \xB7 \uC9C1\uD1B5" : summary.dataGap ? " \xB7 DATA_GAP" : ` / ${summary.totalDays}\uC77C \uC131\uACF5`)), /* @__PURE__ */ React.createElement("p", null, notApplicable ? "\uD658\uC2B9 \uC5F0\uACB0\uC774 \uC5C6\uC5B4 \uC131\uACF5\uB960\uC744 \uC0B0\uCD9C\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4." : summary.dataGap ? "\uAC80\uC99D\uB41C \uC2DC\uAC01\uACFC \uD574\uB2F9 \uB0A0\uC9DC \uD1B5\uACFC \uC774\uB825\uC774 \uBAA8\uB450 \uD544\uC694\uD569\uB2C8\uB2E4." : /* @__PURE__ */ React.createElement(React.Fragment, null, "\uACB0\uACFC \uC694\uC57D\uC740 ", /* @__PURE__ */ React.createElement("strong", null, summary.weakestLeg), "\uC785\uB2C8\uB2E4.")), /* @__PURE__ */ React.createElement("div", { className: "coverage-row" }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "database" }), " \uC801\uC7AC \uD1B5\uACFC \uC774\uBCA4\uD2B8"), /* @__PURE__ */ React.createElement("strong", null, summary.coverage || 0, "\uAC74")))), /* @__PURE__ */ React.createElement("section", { className: "daily-results" }, /* @__PURE__ */ React.createElement("div", { className: "card-title" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uB0A0\uC9DC\uBCC4"), /* @__PURE__ */ React.createElement("h3", null, "\uC5F0\uACB0 \uC131\uACF5 \uC5EC\uBD80")), /* @__PURE__ */ React.createElement(SourceBadge, { mode: simulation.mode || "offline", label: notApplicable ? "\uBE44\uB300\uC0C1" : simulation.mode === "live" ? "\uC2E4\uC81C \uD1B5\uACFC \uC774\uB825" : "DATA_GAP" })), simulation.perDay?.map((day) => /* @__PURE__ */ React.createElement("article", { className: "day-row", key: day.date }, /* @__PURE__ */ React.createElement("div", { className: `day-state ${day.status === "gap" ? "gap" : day.success ? "success" : "fail"}` }, /* @__PURE__ */ React.createElement(Icon, { name: day.status === "gap" ? "question" : day.success ? "check" : "x" })), /* @__PURE__ */ React.createElement("div", { className: "day-copy" }, /* @__PURE__ */ React.createElement("strong", null, day.date), /* @__PURE__ */ React.createElement("small", null, notApplicable ? "\uBE44\uB300\uC0C1 \xB7 \uD658\uC2B9 \uC5C6\uC74C" : day.status === "gap" ? "DATA_GAP \xB7 \uAD00\uCE21 \uBD80\uC871" : day.success ? "\uBAA8\uB4E0 \uD658\uC2B9 \uC131\uACF5" : day.reasons?.[0] || "\uD658\uC2B9 \uC2E4\uD328")), /* @__PURE__ */ React.createElement("div", { className: "day-score" }, /* @__PURE__ */ React.createElement("strong", null, Number.isFinite(day.probability) ? `${day.probability}%` : "\u2014"), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("i", { style: { width: `${Number.isFinite(day.probability) ? day.probability : 0}%` } })))))), /* @__PURE__ */ React.createElement(InlineNotice, { tone: "neutral", icon: "flask", title: "\uACB0\uACFC \uD574\uC11D" }, notApplicable ? "\uC9C1\uD1B5 \uACBD\uB85C\uB294 \uD658\uC2B9 \uC5F0\uACB0 \uD310\uC815 \uB300\uC0C1\uC774 \uC544\uB2C8\uBA70 \uC131\uACF5\xB7\uC2E4\uD328 \uB610\uB294 \uC131\uACF5\uB960\uC744 \uB9CC\uB4E4\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4." : "TAGO\uB294 \uACFC\uAC70 \uC6B4\uD589 \uC774\uB825\uC744 \uC18C\uAE09 \uC81C\uACF5\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4. \uC5F0\uACB0 \uC774\uD6C4 \uC801\uC7AC\uD55C \uC2E4\uC81C \uCC28\uB7C9 \uD1B5\uACFC\uC640 \uAC80\uC99D\uB41C \uD604\uC7AC \uC2DC\uAC04\uD45C \uC2DC\uAC01\uC774 \uD568\uAED8 \uC788\uB294 \uB0A0\uC9DC\uB9CC \uC131\uACF5\xB7\uC2E4\uD328\uB85C \uD310\uC815\uD569\uB2C8\uB2E4. GTFS \uACFC\uAC70 \uC790\uB8CC\uB294 \uBAA8\uB378 \uADFC\uAC70\uC77C \uBFD0 \uB2E8\uB3C5 \uD310\uC815\uAC12\uC774 \uC544\uB2D9\uB2C8\uB2E4."));
}
function validJourneyCoordinate(stop) {
  const latitude = Number(stop?.latitude ?? stop?.lat);
  const longitude = Number(stop?.longitude ?? stop?.lon);
  return Number.isFinite(latitude) && Number.isFinite(longitude) && latitude >= -90 && latitude <= 90 && longitude >= -180 && longitude <= 180;
}
function normalizeJourneyMapStop(stop) {
  return {
    ...stop,
    node_id: String(stop?.node_id || ""),
    node_name: String(stop?.node_name || stop?.node_id || "\uC815\uB958\uC7A5"),
    node_order: Number(stop?.node_order || 0),
    latitude: Number(stop?.latitude ?? stop?.lat),
    longitude: Number(stop?.longitude ?? stop?.lon)
  };
}
function journeyStopsMatch(left, right) {
  return String(left?.city_code || "") === String(right?.city_code || "") && String(left?.node_id || "") === String(right?.node_id || "") && Number(left?.node_order) === Number(right?.node_order);
}
function summarizeJourneySections(journey) {
  const sections = [];
  let currentRide = null;
  const routeRefs = new Map((Array.isArray(journey?.routes) ? journey.routes : []).map((route) => [
    String(route?.route_id || route?.routeId || ""),
    String(route?.route_no || route?.routeNo || route?.route_id || route?.routeId || "")
  ]));
  for (const step of Array.isArray(journey?.steps) ? journey.steps : []) {
    const from = step?.from || {};
    const to = step?.to || {};
    const distance = Number(step?.distance_m);
    if (step?.kind === "ride" && step.route_id) {
      const routeId = String(step.route_id);
      const segmentStops = Array.isArray(step.segment_stops) && step.segment_stops.length >= 2 ? step.segment_stops : [from, to];
      const explicitStopCount = Number(step.stop_count);
      const orderDelta = Number(step.stop_order_delta);
      const stepStopCount = Number.isFinite(explicitStopCount) && explicitStopCount >= 2 ? Math.round(explicitStopCount) : Number.isFinite(orderDelta) && orderDelta >= 1 ? Math.round(orderDelta) + 1 : Math.max(2, segmentStops.length);
      const stepEdgeCount = Math.max(1, stepStopCount - 1);
      const continues = currentRide && currentRide.routeId === routeId && journeyStopsMatch(currentRide.to, from);
      if (continues) {
        currentRide.to = to;
        currentRide.edgeCount += stepEdgeCount;
        currentRide.stopCount += stepEdgeCount;
        currentRide.distanceM += Number.isFinite(distance) ? distance : 0;
        currentRide.stops.push(...segmentStops.slice(1));
      } else {
        currentRide = {
          kind: "ride",
          routeId,
          routeRef: routeRefs.get(routeId) || routeId,
          from,
          to,
          edgeCount: stepEdgeCount,
          stopCount: stepStopCount,
          distanceM: Number.isFinite(distance) ? distance : 0,
          stops: segmentStops
        };
        sections.push(currentRide);
      }
      continue;
    }
    currentRide = null;
    const accessKind = String(step?.access_kind || "").toLowerCase();
    const sectionKind = step?.kind === "transfer" ? "transfer" : step?.kind === "walk" && accessKind === "access" ? "access" : step?.kind === "walk" && accessKind === "egress" ? "egress" : "walk";
    sections.push({
      kind: sectionKind,
      routeId: "",
      from,
      to,
      edgeCount: 1,
      distanceM: Number.isFinite(distance) ? distance : 0,
      stops: [from, to]
    });
  }
  return sections;
}
function buildJourneyMapPayload(sections) {
  const lines = [];
  const walkingLines = [];
  const stops = [];
  for (const section of sections) {
    const sectionStops = section.stops.filter(validJourneyCoordinate).map(normalizeJourneyMapStop);
    sectionStops.forEach((stop) => {
      const previous = stops[stops.length - 1];
      if (!previous || previous.node_id !== stop.node_id || previous.node_order !== stop.node_order) stops.push(stop);
    });
    if (sectionStops.length < 2) continue;
    const line = sectionStops.map((stop) => [stop.longitude, stop.latitude]);
    if (!line.some((point, index) => index > 0 && (point[0] !== line[0][0] || point[1] !== line[0][1]))) continue;
    lines.push(line);
    if (section.kind !== "ride") walkingLines.push(line);
  }
  return {
    geometry: journeyGeometryFromLines(lines),
    walkingGeometry: journeyGeometryFromLines(walkingLines),
    stops
  };
}
const JOURNEY_GEOMETRY_REQUEST_CACHE = /* @__PURE__ */ new Map();
const MAX_JOURNEY_GEOMETRY_CACHE = 12;
const MAX_JOURNEY_GEOMETRY_POINTS = 2e4;
function normalizeJourneyGeometry(value) {
  const type = value?.type;
  const sourceLines = type === "LineString" ? [value.coordinates] : type === "MultiLineString" ? value.coordinates : null;
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
      if (!Number.isFinite(latitude) || !Number.isFinite(longitude) || latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180) return null;
      pointCount += 1;
      if (pointCount > MAX_JOURNEY_GEOMETRY_POINTS) return null;
      line.push([longitude, latitude]);
    }
    lines.push(line);
  }
  return lines.length === 1 ? { type: "LineString", coordinates: lines[0] } : { type: "MultiLineString", coordinates: lines };
}
function journeyGeometryLines(geometry) {
  if (geometry?.type === "LineString") return [geometry.coordinates];
  return geometry?.type === "MultiLineString" ? geometry.coordinates : [];
}
function journeyGeometryFromLines(lines) {
  if (!Array.isArray(lines) || lines.length === 0) return null;
  return lines.length === 1 ? { type: "LineString", coordinates: lines[0] } : { type: "MultiLineString", coordinates: lines };
}
function mergeJourneyGeometry(primary, supplemental) {
  return journeyGeometryFromLines([
    ...journeyGeometryLines(primary),
    ...journeyGeometryLines(supplemental)
  ]);
}
function buildJourneyGeometryRequests(sections) {
  return sections.filter((section) => section.kind === "ride" && section.routeId).map((section) => ({
    routeId: section.routeId,
    routeRef: section.routeRef || section.routeId,
    stops: section.stops.filter(validJourneyCoordinate).map(normalizeJourneyMapStop)
  })).filter((request) => request.stops.length >= 2);
}
function journeyGeometryRequestKey(requests) {
  if (requests.length === 0) return "journey-geometry:none";
  return JSON.stringify(requests.map((request) => [
    request.routeId,
    request.routeRef,
    request.stops.map((stop) => [stop.node_id, stop.node_order, stop.latitude, stop.longitude])
  ]));
}
function requestJourneyGeometry(requestKey, requests) {
  if (JOURNEY_GEOMETRY_REQUEST_CACHE.has(requestKey)) return JOURNEY_GEOMETRY_REQUEST_CACHE.get(requestKey);
  if (JOURNEY_GEOMETRY_REQUEST_CACHE.size >= MAX_JOURNEY_GEOMETRY_CACHE) {
    JOURNEY_GEOMETRY_REQUEST_CACHE.delete(JOURNEY_GEOMETRY_REQUEST_CACHE.keys().next().value);
  }
  const request = Promise.allSettled(requests.map((item) => BusroApi.routeGeometry(item.routeRef, item.stops))).then((outcomes) => {
    const resolved = [];
    const fallbackRoutes = [];
    const lines = [];
    outcomes.forEach((outcome, index) => {
      const requestItem = requests[index];
      const payload = outcome.status === "fulfilled" ? outcome.value : null;
      const source2 = String(payload?.geometry_source || "");
      const geometry2 = ["osm_bus_relation", "osm_road_route_estimate"].includes(source2) ? normalizeJourneyGeometry(payload?.geometry) : null;
      if (geometry2) {
        lines.push(...journeyGeometryLines(geometry2));
        resolved.push({ routeRef: requestItem.routeRef, source: source2, precision: String(payload?.precision || "") });
        return;
      }
      const fallbackLine = requestItem.stops.filter(validJourneyCoordinate).map((stop) => [Number(stop.longitude), Number(stop.latitude)]);
      if (fallbackLine.length >= 2) {
        lines.push(fallbackLine);
        fallbackRoutes.push(requestItem.routeRef);
      }
    });
    if (lines.length === 0) return { status: "gap" };
    const geometry = lines.length === 1 ? { type: "LineString", coordinates: lines[0] } : { type: "MultiLineString", coordinates: lines };
    const sources = resolved.map((item) => item.source);
    const source = fallbackRoutes.length > 0 && resolved.length > 0 ? "partial_osm_geometry" : fallbackRoutes.length > 0 ? "ordered_stop_fallback" : sources.every((item) => item === "osm_bus_relation") ? "osm_bus_relation" : sources.every((item) => item === "osm_road_route_estimate") ? "osm_road_route_estimate" : "mixed_osm_geometry";
    return {
      status: "ready",
      geometry,
      source,
      precision: [...new Set(resolved.map((item) => item.precision).filter(Boolean))].join(","),
      resolvedRoutes: resolved.map((item) => item.routeRef),
      fallbackRoutes,
      retryable: fallbackRoutes.length > 0
    };
  }).catch(() => ({ status: "gap" })).then((result) => {
    if (result.status !== "ready" || result.retryable) JOURNEY_GEOMETRY_REQUEST_CACHE.delete(requestKey);
    return result;
  });
  JOURNEY_GEOMETRY_REQUEST_CACHE.set(requestKey, request);
  return request;
}
function formatJourneyDistance(value) {
  const distance = Number(value);
  if (!Number.isFinite(distance)) return "\uAC70\uB9AC DATA_GAP";
  return distance >= 1e3 ? `${(distance / 1e3).toFixed(1)}km` : `${Math.round(distance)}m`;
}
function safeJourneySourceUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}
function parseJourneySource(value) {
  const raw = String(value || "").trim();
  let source = null;
  if (raw.startsWith("{")) {
    try {
      source = JSON.parse(raw);
    } catch {
      source = null;
    }
  }
  if (!source || Array.isArray(source) || typeof source !== "object") {
    return { key: raw || "unknown", label: raw || "\uCD9C\uCC98 DATA_GAP", type: "\uACBD\uC720 \uC21C\uC11C \uADFC\uAC70", date: "", hash: "", url: "" };
  }
  const kind = String(source.kind || "");
  const dataset = String(source.dataset || source.name || "\uACF5\uC2DD \uAD50\uD1B5 \uB370\uC774\uD130").replace(/_\d{8}$/, "");
  return {
    key: raw,
    label: dataset,
    type: kind === "OFFICIAL_MUNICIPAL_ROUTE_STOP_CSV" ? "\uC9C0\uC790\uCCB4 \uACF5\uC2DD \uACBD\uC720 \uC21C\uC11C" : kind || "\uACF5\uC2DD \uACBD\uB85C \uADFC\uAC70",
    date: String(source.route_date || source.source_date || ""),
    capturedAt: String(source.captured_at || ""),
    hash: String(source.file_sha256 || source.sha256 || ""),
    url: safeJourneySourceUrl(source.page || source.download)
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
  return {
    VERIFIED_TIMETABLE_REQUIRED: "\uAC80\uC99D\uB41C \uC2DC\uAC04\uD45C \uC5C6\uC74C",
    PASSAGE_HISTORY_REQUIRED: "\uC2E4\uC81C \uD1B5\uACFC \uC774\uB825 \uBD80\uC871",
    HISTORICAL_GTFS_PRIOR_ONLY: "\uACFC\uAC70 GTFS \xB7 \uBAA8\uB378 \uADFC\uAC70 \uC804\uC6A9"
  }[reason] || reason;
}
function journeyMapPresentation(state, stopCount) {
  if (state.source === "osm_bus_relation") return {
    title: "OSM \uBC84\uC2A4 \uAD00\uACC4 \uD615\uC0C1",
    badge: "OSM route=bus",
    icon: "path",
    tone: "relation",
    detail: `OSM \uBC84\uC2A4 \uAD00\uACC4\uC640 \uACF5\uC2DD \uC815\uB958\uC7A5 ${stopCount}\uAC1C\uB97C \uD568\uAED8 \uD45C\uC2DC\uD569\uB2C8\uB2E4. \uC2E4\uC81C \uCC28\uB7C9 GPS \uADA4\uC801\uC740 \uC544\uB2D9\uB2C8\uB2E4.`
  };
  if (state.source === "osm_road_route_estimate") return {
    title: "OSM/OSRM \uB3C4\uB85C \uCD94\uC815\uC120",
    badge: "\uC815\uB958\uC7A5 \uC21C\uC11C \uAE30\uBC18",
    icon: "road-horizon",
    tone: "estimate",
    detail: `\uACF5\uC2DD \uC815\uB958\uC7A5 ${stopCount}\uAC1C\uC758 \uC6B4\uD589 \uC21C\uC11C\uB97C \uB530\uB77C \uB3C4\uB85C\uB9DD\uC73C\uB85C \uCD94\uC815\uD588\uC2B5\uB2C8\uB2E4. \uC2E4\uC81C \uCC28\uB7C9 GPS \uADA4\uC801\uC740 \uC544\uB2D9\uB2C8\uB2E4.`
  };
  if (state.source === "mixed_osm_geometry") return {
    title: "OSM \uAD00\uACC4\xB7\uB3C4\uB85C \uCD94\uC815 \uD63C\uD569",
    badge: "\uAD6C\uAC04\uBCC4 \uD615\uC0C1",
    icon: "map-trifold",
    tone: "mixed",
    detail: `\uB178\uC120\uBCC4 OSM \uAD00\uACC4 \uB610\uB294 \uB3C4\uB85C \uCD94\uC815 \uD615\uC0C1\uC744 \uC774\uC5B4 \uD45C\uC2DC\uD569\uB2C8\uB2E4. \uC2E4\uC81C \uCC28\uB7C9 GPS \uADA4\uC801\uC740 \uC544\uB2D9\uB2C8\uB2E4.`
  };
  if (state.source === "partial_osm_geometry") return {
    title: "OSM \uB178\uC120 \uD615\uC0C1 \xB7 \uC815\uB958\uC7A5 \uC21C\uC11C \uBCF4\uC644",
    badge: "\uAD6C\uAC04\uBCC4 \uAC80\uC99D",
    icon: "map-trifold",
    tone: "mixed",
    detail: `OSM \uD615\uC0C1 ${state.resolvedRoutes?.length || 0}\uAC1C \uAD6C\uAC04\uACFC \uACF5\uC2DD \uC815\uB958\uC7A5 \uC21C\uC11C \uBCF4\uC644 ${state.fallbackRoutes?.length || 0}\uAC1C \uAD6C\uAC04\uC744 \uD568\uAED8 \uD45C\uC2DC\uD569\uB2C8\uB2E4. \uC2E4\uC81C \uCC28\uB7C9 GPS \uADA4\uC801\uC740 \uC544\uB2D9\uB2C8\uB2E4.`
  };
  if (state.source === "ordered_stop_fallback") return {
    title: "\uACF5\uC2DD \uC815\uB958\uC7A5 \uC21C\uC11C \uACBD\uB85C",
    badge: "OSM \uD615\uC0C1 \uC7AC\uC2DC\uB3C4 \uAC00\uB2A5",
    icon: "path",
    tone: "estimate",
    detail: `\uACF5\uC2DD \uACBD\uC720 \uC815\uB958\uC7A5 ${stopCount}\uAC1C\uC758 \uC21C\uC11C\uB97C \uC5F0\uACB0\uD588\uC2B5\uB2C8\uB2E4. \uB3C4\uB85C \uD615\uC0C1\uC774\uB098 \uC2E4\uC81C \uCC28\uB7C9 GPS \uADA4\uC801\uC740 \uC544\uB2D9\uB2C8\uB2E4.`
  };
  return {
    title: "\uBC84\uC2A4 \uC774\uB3D9 \uACBD\uB85C",
    badge: state.status === "loading" ? "\uB3C4\uB85C \uACBD\uB85C \uBD88\uB7EC\uC624\uB294 \uC911" : "\uB300\uB7B5\uC801\uC778 \uACBD\uB85C",
    icon: state.status === "loading" ? "spinner-gap" : "path",
    tone: state.status === "loading" ? "loading" : "gap",
    detail: state.status === "loading" ? "\uB3C4\uB85C\uB97C \uB530\uB77C\uAC00\uB294 \uACBD\uB85C\uB97C \uBD88\uB7EC\uC624\uACE0 \uC788\uC5B4\uC694." : `${stopCount}\uAC1C \uC815\uB958\uC7A5\uC744 \uC9C0\uB098\uB294 \uB300\uB7B5\uC801\uC778 \uC774\uB3D9 \uACBD\uB85C\uC608\uC694.`
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
      return () => {
        active = false;
      };
    }
    setResolvedGeometry({ key: requestKey, status: "loading", geometry: null, source: "" });
    requestJourneyGeometry(requestKey, geometryRequests).then((result) => {
      if (!active) return;
      setResolvedGeometry({ key: requestKey, ...result, geometry: result.geometry || null, source: result.source || "" });
    });
    return () => {
      active = false;
    };
  }, [requestKey]);
  const geometryState = resolvedGeometry.key === requestKey ? resolvedGeometry : { key: requestKey, status: geometryRequests.length ? "loading" : "gap", geometry: null, source: "" };
  const displayedGeometry = geometryState.status === "ready" && geometryState.geometry ? mergeJourneyGeometry(geometryState.geometry, mapPayload.walkingGeometry) : mapPayload.geometry;
  const routeStopCount = sections.filter((section) => section.kind === "ride").reduce((sum, section) => sum + Math.max(2, Number(section.stopCount) || section.stops.length), 0);
  const presentation = journeyMapPresentation(geometryState, routeStopCount || mapPayload.stops.length);
  if (!mapPayload.geometry) {
    return /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "map-trifold", title: "\uC9C0\uB3C4\uB97C \uD45C\uC2DC\uD560 \uC218 \uC5C6\uC5B4\uC694" }, "\uC774 \uACBD\uB85C\uC758 \uC815\uB958\uC7A5 \uC704\uCE58\uB97C \uB2E4\uC2DC \uD655\uC778\uD574 \uC8FC\uC138\uC694.");
  }
  return /* @__PURE__ */ React.createElement("section", { className: `journey-route-map ${presentation.tone}`, "aria-labelledby": "journey-map-title" }, /* @__PURE__ */ React.createElement(
    OSMRouteMap,
    {
      geometry: displayedGeometry,
      stops: mapPayload.stops,
      positions: [],
      loading: false,
      ariaLabel: `${fromName}\uC5D0\uC11C ${toName}\uAE4C\uC9C0 ${presentation.title} \uC9C0\uB3C4`,
      badgeLabel: "OpenStreetMap"
    }
  ), /* @__PURE__ */ React.createElement("div", { className: "journey-map-caption" }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: presentation.icon })), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "journey-map-title-row" }, /* @__PURE__ */ React.createElement("strong", { id: "journey-map-title" }, presentation.title), /* @__PURE__ */ React.createElement("em", null, presentation.badge)), /* @__PURE__ */ React.createElement("small", null, presentation.detail))));
}
function JourneyScreen({ journey, connection, onExplore }) {
  const selectedFetchedWindows = useCandidateRouteWindows(journey ? [journey] : []);
  if (!journey) {
    return /* @__PURE__ */ React.createElement("main", { className: "screen content-screen journey-screen" }, /* @__PURE__ */ React.createElement(ScreenHeading, { eyebrow: "\uB0B4 \uACBD\uB85C", title: "\uC544\uC9C1 \uACE0\uB978 \uACBD\uB85C\uAC00 \uC5C6\uC5B4\uC694", detail: "\uD648\uC5D0\uC11C \uCD9C\uBC1C\xB7\uB3C4\uCC29 \uC815\uB958\uC7A5\uC744 \uACE0\uB974\uACE0 \uC5EC\uD589 \uACBD\uB85C\uB97C \uC120\uD0DD\uD558\uC138\uC694." }), /* @__PURE__ */ React.createElement(GlassCard, { className: "ticket-card" }, /* @__PURE__ */ React.createElement(InlineNotice, { tone: "neutral", icon: "map-trifold", title: "\uC5EC\uD589 \uACBD\uB85C \uCC3E\uAE30" }, "\uC804\uAD6D \uC815\uB958\uC7A5\uC744 \uAC80\uC0C9\uD574 \uC2DC\uB0B4\uBC84\uC2A4\uB97C \uC774\uC5B4 \uBCF4\uC138\uC694.")), /* @__PURE__ */ React.createElement("button", { className: "liquid-button sticky-action", type: "button", onClick: onExplore }, "\uACBD\uB85C \uCC3E\uC73C\uB7EC \uAC00\uAE30 ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" })));
  }
  const fromStop = journey.from_stop || journey.from || {};
  const toStop = journey.to_stop || journey.to || {};
  const fromName = fromStop.node_name || fromStop.stop_name || fromStop.node_id || "\uCD9C\uBC1C \uC815\uB958\uC7A5";
  const toName = toStop.node_name || toStop.stop_name || toStop.node_id || "\uB3C4\uCC29 \uC815\uB958\uC7A5";
  const routeIds = Array.isArray(journey.route_ids) ? journey.route_ids.filter(Boolean) : [];
  const routeLabels = new Map((Array.isArray(journey.routes) ? journey.routes : []).map((item) => [String(item?.route_id || item?.routeId || ""), String(item?.route_no || item?.routeNo || item?.route_id || item?.routeId || "\uBC84\uC2A4")]));
  const steps = Array.isArray(journey.steps) ? journey.steps : [];
  const sections = summarizeJourneySections(journey);
  const reasons = Array.isArray(journey.reasons) ? journey.reasons.filter(Boolean) : [];
  const status = journey.status || "DATA_GAP";
  const successProbability = verifiedSuccessProbability(journey);
  const coverage = journey.coverage && typeof journey.coverage === "object" ? journey.coverage : {};
  const evidence = journey.evidence && typeof journey.evidence === "object" ? journey.evidence : {};
  const sources = collectJourneySources(journey);
  const schedule = normalizeSchedule({ schedule: journey.schedule || {} });
  const provenance = scheduleEvidence(schedule, journey);
  const departureTime = schedule.ready ? formatGtfsClock(journey.departure_time, journey.departure_seconds) : null;
  const arrivalTime = schedule.ready ? formatGtfsClock(journey.arrival_time, journey.arrival_seconds) : null;
  const timeSummary = [departureTime ? `\uCD9C\uBC1C ${departureTime}` : "", arrivalTime ? `\uB3C4\uCC29 ${arrivalTime}` : ""].filter(Boolean);
  const topologyReady = sections.some((section) => section.kind === "ride");
  return /* @__PURE__ */ React.createElement("main", { className: "screen content-screen journey-screen" }, /* @__PURE__ */ React.createElement(ScreenHeading, { eyebrow: "\uB0B4 \uACBD\uB85C", title: `${fromName} \u2192 ${toName}`, detail: `${journey.transfers || 0}\uBC88 \uD658\uC2B9\uD558\uB294 \uC2DC\uB0B4\uBC84\uC2A4 \uC5EC\uD589 \uACBD\uB85C\uC608\uC694.` }), /* @__PURE__ */ React.createElement(JourneyRouteMap, { sections, fromName, toName }), /* @__PURE__ */ React.createElement(GlassCard, { className: "ticket-card" }, /* @__PURE__ */ React.createElement("div", { className: "ticket-route" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "\uCD9C\uBC1C"), /* @__PURE__ */ React.createElement("strong", null, fromName)), /* @__PURE__ */ React.createElement("div", { className: "ticket-line" }, /* @__PURE__ */ React.createElement("span", null), /* @__PURE__ */ React.createElement(Icon, { name: "bus" }), /* @__PURE__ */ React.createElement("span", null)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "\uB3C4\uCC29"), /* @__PURE__ */ React.createElement("strong", null, toName))), /* @__PURE__ */ React.createElement("div", { className: "ticket-meta" }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "bus" }), " \uBC84\uC2A4 ", routeIds.length || 0, "\uB300"), typeof journey.transfers === "number" && /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "arrows-left-right" }), " ", journey.transfers, "\uD68C \uD658\uC2B9"), typeof journey.walking_m === "number" && /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "person-simple-walk" }), " ", Math.round(journey.walking_m), "m")), routeIds.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "ticket-meta" }, routeIds.map((routeId) => /* @__PURE__ */ React.createElement("span", { key: routeId }, /* @__PURE__ */ React.createElement(Icon, { name: "bus" }), " ", routeLabels.get(routeId) || routeId)))), /* @__PURE__ */ React.createElement("section", { className: "leg-timeline" }, sections.map((section, index) => {
    const stepFrom = section.from || {};
    const stepTo = section.to || {};
    const isTransfer = section.kind === "transfer";
    const isAccess = section.kind === "access";
    const isEgress = section.kind === "egress";
    const isWalk = section.kind !== "ride";
    const stopCount = Number(section.stopCount) || section.edgeCount + 1;
    const intermediateCount = Math.max(0, stopCount - 2);
    const stepLabel = isAccess ? "\uCD9C\uBC1C \uC811\uADFC" : isEgress ? "\uB3C4\uCC29 \uC774\uD0C8" : isTransfer ? "\uD658\uC2B9" : isWalk ? "\uB3C4\uBCF4" : routeLabels.get(section.routeId) || section.routeId || "\uBC84\uC2A4";
    const movementLabel = isAccess ? "\uCCAB \uC2B9\uCC28 \uC815\uB958\uC7A5\uAE4C\uC9C0 \uAC77\uAE30" : isEgress ? "\uD558\uCC28 \uD6C4 \uB3C4\uCC29 \uC815\uB958\uC7A5\uAE4C\uC9C0 \uAC77\uAE30" : isTransfer ? "\uAC78\uC5B4\uC11C \uD658\uC2B9" : isWalk ? "\uAC78\uC5B4\uC11C \uC774\uB3D9" : "\uBC84\uC2A4 \uC774\uB3D9";
    const fromFallback = isAccess ? "\uC120\uD0DD \uCD9C\uBC1C \uC815\uB958\uC7A5" : isEgress ? "\uB9C8\uC9C0\uB9C9 \uD558\uCC28 \uC815\uB958\uC7A5" : "\uC2B9\uCC28 \uC815\uB958\uC7A5";
    const toFallback = isAccess ? "\uCCAB \uC2B9\uCC28 \uC815\uB958\uC7A5" : isEgress ? "\uC120\uD0DD \uB3C4\uCC29 \uC815\uB958\uC7A5" : "\uD558\uCC28 \uC815\uB958\uC7A5";
    return /* @__PURE__ */ React.createElement("article", { key: `${section.kind || "step"}-${section.routeId || "none"}-${index}`, className: index === 0 ? "current" : "" }, /* @__PURE__ */ React.createElement("div", { className: "leg-rail" }, /* @__PURE__ */ React.createElement("span", null, index + 1), /* @__PURE__ */ React.createElement("i", null)), /* @__PURE__ */ React.createElement("div", { className: "leg-card" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("span", { className: "line-chip blue" }, stepLabel), movementLabel), /* @__PURE__ */ React.createElement("h3", null, stepFrom.node_name || stepFrom.node_id || fromFallback, " \u2192 ", stepTo.node_name || stepTo.node_id || toFallback), /* @__PURE__ */ React.createElement("small", null, isWalk ? `\uB3C4\uBCF4 ${formatJourneyDistance(section.distanceM)}` : `${stopCount}\uAC1C \uC815\uB958\uC7A5 \xB7 \uC911\uAC04 \uC815\uB958\uC7A5 ${intermediateCount}\uAC1C`))));
  })), steps.length === 0 && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "warning-circle", title: "\uC0C1\uC138 \uACBD\uB85C\uB97C \uBD88\uB7EC\uC624\uC9C0 \uBABB\uD588\uC5B4\uC694" }, "\uB2E4\uB978 \uACBD\uB85C\uB97C \uC120\uD0DD\uD574 \uC8FC\uC138\uC694."), sources.length > 0 && /* @__PURE__ */ React.createElement("details", { className: "journey-evidence" }, /* @__PURE__ */ React.createElement("summary", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "seal-check" }), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("strong", null, "\uB370\uC774\uD130 \uCD9C\uCC98"), /* @__PURE__ */ React.createElement("small", null, "\uB178\uC120 \uC815\uBCF4\uAC00 \uC5B4\uB514\uC5D0\uC11C \uC654\uB294\uC9C0 \uD655\uC778\uD560 \uC218 \uC788\uC5B4\uC694."))), /* @__PURE__ */ React.createElement(Icon, { name: "caret-down" })), /* @__PURE__ */ React.createElement("div", { className: "journey-evidence-list" }, sources.map((source) => /* @__PURE__ */ React.createElement("article", { key: source.key }, /* @__PURE__ */ React.createElement("span", null, source.type), /* @__PURE__ */ React.createElement("strong", null, source.label), source.date && /* @__PURE__ */ React.createElement("small", null, "\uAE30\uC900\uC77C ", source.date), source.url && /* @__PURE__ */ React.createElement("a", { href: source.url, target: "_blank", rel: "noreferrer" }, "\uACF5\uC2DD \uC6D0\uBB38 \uBCF4\uAE30 ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-square-out" })))))), /* @__PURE__ */ React.createElement("button", { className: "liquid-button sticky-action", type: "button", onClick: onExplore }, "\uB2E4\uB978 \uACBD\uB85C \uCC3E\uAE30 ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" })));
}
function SettingsSheet({ open, onClose, apiBase, setApiBase, connection, journey, mappings, legs, mappingSummary, settingsError, onMappingChange, onVerifyMapping, onReconnect }) {
  if (!open) return null;
  return /* @__PURE__ */ React.createElement("div", { className: "sheet-layer", role: "presentation", onMouseDown: (event) => event.target === event.currentTarget && onClose() }, /* @__PURE__ */ React.createElement("section", { className: "settings-sheet", role: "dialog", "aria-modal": "true", "aria-labelledby": "settings-title" }, /* @__PURE__ */ React.createElement("div", { className: "sheet-grabber" }), /* @__PURE__ */ React.createElement("div", { className: "sheet-title" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uB370\uC774\uD130 \uC5F0\uACB0"), /* @__PURE__ */ React.createElement("h2", { id: "settings-title" }, "\uACF5\uC2DD \uAD50\uD1B5 \uB370\uC774\uD130")), /* @__PURE__ */ React.createElement("button", { type: "button", onClick: onClose, "aria-label": "\uB2EB\uAE30" }, /* @__PURE__ */ React.createElement(Icon, { name: "x" }))), /* @__PURE__ */ React.createElement(InlineNotice, { tone: connection.mode === "live" ? "success" : "warning", icon: connection.mode === "live" ? "cloud-check" : "key", title: connection.label }, connection.message), /* @__PURE__ */ React.createElement("label", { className: "api-field" }, /* @__PURE__ */ React.createElement("span", null, "\uB85C\uCEEC \uB370\uC774\uD130 \uC11C\uBE44\uC2A4 \uC8FC\uC18C"), /* @__PURE__ */ React.createElement("input", { value: apiBase, onChange: (event) => setApiBase(event.target.value), placeholder: "http://127.0.0.1:8791/api" })), /* @__PURE__ */ React.createElement("p", { className: "privacy-note" }, /* @__PURE__ */ React.createElement(Icon, { name: "shield-check" }), " TAGO \uC11C\uBE44\uC2A4 \uD0A4\uB294 \uBE0C\uB77C\uC6B0\uC800\uC5D0 \uC785\uB825\uD558\uAC70\uB098 \uC800\uC7A5\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4. \uB85C\uCEEC \uB370\uC774\uD130 \uC11C\uBE44\uC2A4 \uB610\uB294 \uC5F0\uACB0\uB41C upstream\uC5D0\uC11C\uB9CC \uAD00\uB9AC\uD569\uB2C8\uB2E4."), !journey || legs.length === 0 ? /* @__PURE__ */ React.createElement("section", { className: "mapping-settings", "aria-labelledby": "mapping-title" }, /* @__PURE__ */ React.createElement("div", { className: "mapping-settings-head" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uACF5\uC2DD \uC2DD\uBCC4\uC790"), /* @__PURE__ */ React.createElement("h3", { id: "mapping-title" }, "\uB178\uC120 \uB9E4\uD551")), /* @__PURE__ */ React.createElement("strong", null, "0/0")), /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "map-trifold", title: "\uC804\uAD6D \uC5EC\uD589 \uD6C4\uBCF4 \uBA3C\uC800 \uC120\uD0DD" }, "\uC120\uD0DD\uD55C \uD6C4\uBCF4\uC758 \uC5F0\uC18D \uBC84\uC2A4 \uC774\uB3D9 \uAD6C\uAC04\uB9CC cityCode \xB7 nodeId \xB7 routeId \uAC80\uC99D \uB300\uC0C1\uC73C\uB85C \uD45C\uC2DC\uD569\uB2C8\uB2E4. \uAE30\uC874 \uACE0\uC815 \uAD6C\uAC04\uC740 \uC0AC\uC6A9\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.")) : /* @__PURE__ */ React.createElement("section", { className: "mapping-settings", "aria-labelledby": "mapping-title" }, /* @__PURE__ */ React.createElement("div", { className: "mapping-settings-head" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uACF5\uC2DD \uC2DD\uBCC4\uC790"), /* @__PURE__ */ React.createElement("h3", { id: "mapping-title" }, "\uB178\uC120 \uB9E4\uD551")), /* @__PURE__ */ React.createElement("strong", null, mappingSummary.verified, "/", mappingSummary.total)), /* @__PURE__ */ React.createElement("p", { className: "mapping-help" }, "\uC2E4\uC2DC\uAC04 \uC2B9\uCC28 \uC815\uB958\uC7A5\uACFC \uD658\uC2B9 \uC7AC\uC0DD \uCCB4\uD06C\uD3EC\uC778\uD2B8\uB97C \uAD6C\uBD84\uD574 \uAC80\uC99D\uD569\uB2C8\uB2E4. cityCode \xB7 nodeId \xB7 routeId\uB9CC \uB85C\uCEEC\uC5D0 \uC800\uC7A5\uD558\uBA70, \uC11C\uBC84\uAC00 \uACF5\uC2DD \uACBD\uC720 \uC815\uB958\uC7A5\uC73C\uB85C \uD655\uC778\uD558\uC9C0 \uBABB\uD558\uBA74 DATA_GAP\uC785\uB2C8\uB2E4."), /* @__PURE__ */ React.createElement("div", { className: "mapping-leg-list" }, legs.map((leg, index) => {
    const mapping = mappings[leg.id] || {};
    const complete = Boolean(mapping.cityCode && mapping.nodeId && mapping.routeId);
    return /* @__PURE__ */ React.createElement("article", { className: "mapping-leg", key: leg.id }, /* @__PURE__ */ React.createElement("div", { className: "mapping-leg-title" }, /* @__PURE__ */ React.createElement("span", null, index + 1), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("strong", null, leg.city, " ", leg.routeNo), /* @__PURE__ */ React.createElement("small", null, leg.transferCheckpoint ? "\uD658\uC2B9 \uC7AC\uC0DD \uCCB4\uD06C\uD3EC\uC778\uD2B8" : "\uC2E4\uC2DC\uAC04 \uC2B9\uCC28", " \xB7 ", leg.board, " \u2192 ", leg.alight, " \xB7 \uC21C\uBC88 ", Number.isInteger(leg.nodeOrder) ? leg.nodeOrder : "DATA_GAP")), /* @__PURE__ */ React.createElement(MappingBadge, { state: mapping.state })), /* @__PURE__ */ React.createElement("div", { className: "mapping-fields" }, /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, "cityCode"), /* @__PURE__ */ React.createElement("input", { value: mapping.cityCode || "", onChange: (event) => onMappingChange(leg.id, "cityCode", event.target.value), inputMode: "numeric", autoComplete: "off", "aria-label": `${leg.city} ${leg.routeNo} cityCode` })), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, "nodeId"), /* @__PURE__ */ React.createElement("input", { value: mapping.nodeId || "", onChange: (event) => onMappingChange(leg.id, "nodeId", event.target.value), autoCapitalize: "characters", autoComplete: "off", "aria-label": `${leg.city} ${leg.routeNo} nodeId` })), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, "routeId"), /* @__PURE__ */ React.createElement("input", { value: mapping.routeId || "", onChange: (event) => onMappingChange(leg.id, "routeId", event.target.value), autoCapitalize: "characters", autoComplete: "off", "aria-label": `${leg.city} ${leg.routeNo} routeId` }))), /* @__PURE__ */ React.createElement("div", { className: "mapping-leg-foot" }, /* @__PURE__ */ React.createElement("p", null, mapping.note || "\uC11C\uBC84 \uAC80\uC99D \uC804"), /* @__PURE__ */ React.createElement("button", { type: "button", disabled: !complete || mapping.state === "checking", onClick: () => onVerifyMapping(leg.id) }, mapping.state === "checking" ? "\uAC80\uC99D\uC911" : "\uC11C\uBC84 \uAC80\uC99D", /* @__PURE__ */ React.createElement(Icon, { name: mapping.state === "checking" ? "spinner-gap" : "arrow-right" }))));
  }))), settingsError && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "danger", icon: "warning-circle", title: "\uC800\uC7A5\uD560 \uC218 \uC5C6\uC74C" }, settingsError), /* @__PURE__ */ React.createElement("button", { className: "liquid-button settings-save", type: "button", onClick: onReconnect }, "\uC8FC\uC18C\xB7\uC2DD\uBCC4\uC790 \uC800\uC7A5 \uD6C4 \uC5F0\uACB0 \uD655\uC778 ", /* @__PURE__ */ React.createElement(Icon, { name: "plug" }))));
}
