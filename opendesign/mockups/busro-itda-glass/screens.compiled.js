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
  for (const step of Array.isArray(journey?.steps) ? journey.steps : []) {
    const from = step?.from || {};
    const to = step?.to || {};
    const distance = Number(step?.distance_m);
    if (step?.kind === "ride" && step.route_id) {
      const routeId = String(step.route_id);
      const continues = currentRide && currentRide.routeId === routeId && journeyStopsMatch(currentRide.to, from);
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
          stops: [from, to]
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
      stops: [from, to]
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
  const geometry = lines.length === 1 ? { type: "LineString", coordinates: lines[0] } : lines.length > 1 ? { type: "MultiLineString", coordinates: lines } : null;
  return { geometry, stops };
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
function buildJourneyGeometryRequests(sections) {
  return sections.filter((section) => section.kind === "ride" && section.routeId).map((section) => ({
    routeId: section.routeId,
    stops: section.stops.filter(validJourneyCoordinate).map(normalizeJourneyMapStop)
  })).filter((request) => request.stops.length >= 2);
}
function journeyGeometryRequestKey(requests) {
  if (requests.length === 0) return "journey-geometry:none";
  return JSON.stringify(requests.map((request) => [
    request.routeId,
    request.stops.map((stop) => [stop.node_id, stop.node_order, stop.latitude, stop.longitude])
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
    if (sources.some((source2) => !["osm_bus_relation", "osm_road_route_estimate"].includes(source2))) return { status: "gap" };
    const geometries = payloads.map((payload) => normalizeJourneyGeometry(payload?.geometry));
    if (geometries.some((geometry2) => !geometry2)) return { status: "gap" };
    const lines = geometries.flatMap(journeyGeometryLines);
    if (lines.length === 0) return { status: "gap" };
    const geometry = lines.length === 1 ? { type: "LineString", coordinates: lines[0] } : { type: "MultiLineString", coordinates: lines };
    const source = sources.every((item) => item === "osm_bus_relation") ? "osm_bus_relation" : sources.every((item) => item === "osm_road_route_estimate") ? "osm_road_route_estimate" : "mixed_osm_geometry";
    return {
      status: "ready",
      geometry,
      source,
      precision: [...new Set(payloads.map((payload) => String(payload?.precision || "")).filter(Boolean))].join(",")
    };
  }).catch(() => ({ status: "gap" }));
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
  return {
    title: "\uACF5\uC2DD \uC815\uB958\uC7A5 \uC5F0\uACB0\uC120",
    badge: state.status === "loading" ? "\uB3C4\uB85C \uD615\uC0C1 \uD655\uC778 \uC911" : "\uB3C4\uB85C \uD615\uC0C1 DATA_GAP",
    icon: state.status === "loading" ? "spinner-gap" : "path",
    tone: state.status === "loading" ? "loading" : "gap",
    detail: `${state.status === "loading" ? "\uD604\uC7AC\uB294" : "\uACF5\uAC1C \uB3C4\uB85C \uD615\uC0C1\uC744 \uAC00\uC838\uC624\uC9C0 \uBABB\uD574"} \uACF5\uC2DD \uACBD\uC720 \uC815\uB958\uC7A5 \uC88C\uD45C ${stopCount}\uAC1C\uB97C \uC6B4\uD589 \uC21C\uC11C\uB300\uB85C \uC5F0\uACB0\uD588\uC2B5\uB2C8\uB2E4. \uB3C4\uB85C \uC8FC\uD589\uADA4\uC801\uC740 \uC544\uB2D9\uB2C8\uB2E4.`
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
  const displayedGeometry = geometryState.status === "ready" && geometryState.geometry ? geometryState.geometry : mapPayload.geometry;
  const presentation = journeyMapPresentation(geometryState, mapPayload.stops.length);
  if (!mapPayload.geometry) {
    return /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "map-trifold", title: "\uC9C0\uB3C4 DATA_GAP" }, "\uC120\uD0DD \uACBD\uB85C\uC758 \uACF5\uC2DD \uC815\uB958\uC7A5 \uC88C\uD45C\uAC00 \uC5C6\uC5B4 \uC774\uB3D9\uC120\uC744 \uD45C\uC2DC\uD560 \uC218 \uC5C6\uC2B5\uB2C8\uB2E4.");
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
    return /* @__PURE__ */ React.createElement("main", { className: "screen content-screen journey-screen" }, /* @__PURE__ */ React.createElement(ScreenHeading, { eyebrow: "\uC120\uD0DD\uD55C \uC5EC\uC815", title: "\uC120\uD0DD\uB41C \uBC84\uC2A4 \uC5EC\uD589\uC774 \uC5C6\uC2B5\uB2C8\uB2E4", detail: "\uC804\uAD6D \uD0D0\uC0C9\uC5D0\uC11C \uCD9C\uBC1C\xB7\uB3C4\uCC29 \uC815\uB958\uC7A5\uC744 \uACE0\uB974\uACE0 \uC0DD\uC131\uB41C \uD6C4\uBCF4\uB97C \uC120\uD0DD\uD558\uC138\uC694." }), /* @__PURE__ */ React.createElement(GlassCard, { className: "ticket-card" }, /* @__PURE__ */ React.createElement(InlineNotice, { tone: "neutral", icon: "map-trifold", title: "\uC804\uAD6D \uACBD\uB85C \uD0D0\uC0C9" }, "\uACF5\uC2DD \uC815\uB958\uC7A5 \uC21C\uC11C\uAC00 \uC801\uC7AC\uB41C \uB178\uC120\uB9CC \uC5EC\uD589 \uD6C4\uBCF4\uB85C \uC0AC\uC6A9\uD569\uB2C8\uB2E4.")), /* @__PURE__ */ React.createElement("button", { className: "liquid-button sticky-action", type: "button", onClick: onExplore }, "\uC804\uAD6D \uD0D0\uC0C9\uC73C\uB85C \uAC00\uAE30 ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" })));
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
  return /* @__PURE__ */ React.createElement("main", { className: "screen content-screen journey-screen" }, /* @__PURE__ */ React.createElement(ScreenHeading, { eyebrow: "\uC120\uD0DD\uD55C \uC5EC\uC815", title: `${fromName} \u2192 ${toName}`, detail: "\uD604\uC7AC TAGO \uACBD\uC720 \uC815\uB958\uC7A5\uC73C\uB85C \uC0DD\uC131\uD55C \uACBD\uB85C\uC785\uB2C8\uB2E4. \uC2DC\uAC04\uD45C\xB7\uC2E4\uC2DC\uAC04\xB7\uACFC\uAC70 \uBAA8\uB378 \uADFC\uAC70\uB97C \uAD6C\uBD84\uD574 \uD45C\uC2DC\uD569\uB2C8\uB2E4." }), /* @__PURE__ */ React.createElement(JourneyRouteMap, { sections, fromName, toName }), /* @__PURE__ */ React.createElement(GlassCard, { className: "ticket-card" }, /* @__PURE__ */ React.createElement("div", { className: "ticket-route" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "\uCD9C\uBC1C \uC815\uB958\uC7A5"), /* @__PURE__ */ React.createElement("strong", null, fromName), /* @__PURE__ */ React.createElement("span", null, fromStop.node_id || "ID DATA_GAP")), /* @__PURE__ */ React.createElement("div", { className: "ticket-line" }, /* @__PURE__ */ React.createElement("span", null), /* @__PURE__ */ React.createElement(Icon, { name: "bus" }), /* @__PURE__ */ React.createElement("span", null)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("small", null, "\uB3C4\uCC29 \uC815\uB958\uC7A5"), /* @__PURE__ */ React.createElement("strong", null, toName), /* @__PURE__ */ React.createElement("span", null, toStop.node_id || "ID DATA_GAP"))), /* @__PURE__ */ React.createElement("div", { className: "ticket-meta" }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "bus" }), " \uB178\uC120 ", routeIds.length || "DATA_GAP"), typeof journey.transfers === "number" && /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "arrows-left-right" }), " ", journey.transfers, "\uD68C \uD658\uC2B9"), typeof journey.walking_m === "number" && /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "person-simple-walk" }), " ", Math.round(journey.walking_m), "m")), routeIds.length > 0 && /* @__PURE__ */ React.createElement("div", { className: "ticket-meta" }, routeIds.map((routeId) => /* @__PURE__ */ React.createElement("span", { key: routeId }, /* @__PURE__ */ React.createElement(Icon, { name: "path" }), " ", routeId)))), /* @__PURE__ */ React.createElement(JourneyEvidenceStack, { candidate: journey, context: journey, schedule, provenance, timeSummary, connection, fetchedWindows: selectedFetchedWindows }), /* @__PURE__ */ React.createElement("section", { className: "leg-timeline" }, sections.map((section, index) => {
    const stepFrom = section.from || {};
    const stepTo = section.to || {};
    const isTransfer = section.kind === "transfer";
    const stopCount = section.edgeCount + 1;
    const intermediateCount = Math.max(0, stopCount - 2);
    const stepLabel = isTransfer ? "\uD658\uC2B9" : section.routeId || "\uB178\uC120 DATA_GAP";
    return /* @__PURE__ */ React.createElement("article", { key: `${section.kind || "step"}-${section.routeId || "none"}-${index}`, className: index === 0 ? "current" : "" }, /* @__PURE__ */ React.createElement("div", { className: "leg-rail" }, /* @__PURE__ */ React.createElement("span", null, index + 1), /* @__PURE__ */ React.createElement("i", null)), /* @__PURE__ */ React.createElement("div", { className: "leg-card" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", null, /* @__PURE__ */ React.createElement("span", { className: "line-chip blue" }, stepLabel), isTransfer ? "\uC815\uB958\uC7A5 \uAC04 \uC774\uB3D9" : "\uBC84\uC2A4 \uC2B9\uCC28 \uAD6C\uAC04"), /* @__PURE__ */ React.createElement("h3", null, stepFrom.node_name || stepFrom.node_id || "DATA_GAP", " \u2192 ", stepTo.node_name || stepTo.node_id || "DATA_GAP"), /* @__PURE__ */ React.createElement("small", null, isTransfer ? `\uB3C4\uBCF4 \uC5F0\uACB0 \xB7 ${formatJourneyDistance(section.distanceM)}` : `\uCD1D ${stopCount}\uAC1C \uC815\uB958\uC7A5 \xB7 \uC911\uAC04 \uACBD\uC720 ${intermediateCount}\uAC1C \xB7 \uC88C\uD45C \uAC04 ${formatJourneyDistance(section.distanceM)}`))));
  })), steps.length === 0 && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "warning-circle", title: "DATA_GAP" }, "\uC774 \uD6C4\uBCF4\uC5D0 \uD45C\uC2DC\uD560 \uACBD\uB85C \uB2E8\uACC4\uAC00 \uC5C6\uC2B5\uB2C8\uB2E4."), sources.length > 0 && /* @__PURE__ */ React.createElement("details", { className: "journey-evidence" }, /* @__PURE__ */ React.createElement("summary", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "seal-check" }), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("strong", null, "\uACF5\uC2DD \uACBD\uB85C \uADFC\uAC70"), /* @__PURE__ */ React.createElement("small", null, sources.length, "\uAC1C \uCD9C\uCC98 \xB7 \uC6D0\uBB38 \uC815\uBCF4\uB294 \uC5EC\uAE30\uC11C \uD55C \uBC88\uB9CC \uD45C\uC2DC\uD569\uB2C8\uB2E4."))), /* @__PURE__ */ React.createElement(Icon, { name: "caret-down" })), /* @__PURE__ */ React.createElement("div", { className: "journey-evidence-list" }, sources.map((source) => /* @__PURE__ */ React.createElement("article", { key: source.key }, /* @__PURE__ */ React.createElement("span", null, source.type), /* @__PURE__ */ React.createElement("strong", null, source.label), /* @__PURE__ */ React.createElement("small", null, source.date ? `\uAE30\uC900\uC77C ${source.date}` : "\uAE30\uC900\uC77C DATA_GAP", source.hash ? ` \xB7 SHA-256 ${source.hash.slice(0, 12)}\u2026` : ""), source.url && /* @__PURE__ */ React.createElement("a", { href: source.url, target: "_blank", rel: "noreferrer" }, "\uACF5\uC2DD \uC6D0\uBB38 \uBCF4\uAE30 ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-square-out" })))))), /* @__PURE__ */ React.createElement(InlineNotice, { tone: topologyReady ? "success" : "warning", icon: topologyReady ? "path" : "warning-circle", title: topologyReady ? "\uD604\uC7AC TAGO \uACBD\uB85C" : status }, reasons.length > 0 ? reasons.map(journeyReasonLabel).join(" \xB7 ") : "\uCD94\uAC00 \uACB0\uCE21 \uC0AC\uC720 \uC5C6\uC74C", ` \xB7 ${successProbability === null ? "\uC131\uACF5\uB960 \uBBF8\uC0B0\uCD9C" : `\uAD00\uCE21 \uC131\uACF5\uB960 ${Math.round(successProbability * 100)}%`}`, typeof coverage.schedule_routes === "number" && typeof coverage.total_routes === "number" ? ` \xB7 \uD604\uC7AC \uC2DC\uAC04\uD45C \uADFC\uAC70 ${coverage.schedule_routes}/${coverage.total_routes}` : "", typeof coverage.passage_routes === "number" && typeof coverage.total_routes === "number" ? ` \xB7 \uC2E4\uC81C \uD1B5\uACFC \uC774\uB825 ${coverage.passage_routes}/${coverage.total_routes}` : "", evidence.topology ? " \xB7 \uAC80\uC99D\uB41C \uB2E8\uBC29\uD5A5 \uACBD\uC720 \uC21C\uC11C" : ""), /* @__PURE__ */ React.createElement("button", { className: "liquid-button sticky-action", type: "button", onClick: onExplore }, "\uB2E4\uB978 \uC804\uAD6D \uC5EC\uD589 \uCC3E\uAE30 ", /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right" })));
}
function SettingsSheet({ open, onClose, apiBase, setApiBase, connection, journey, mappings, legs, mappingSummary, settingsError, onMappingChange, onVerifyMapping, onReconnect }) {
  if (!open) return null;
  return /* @__PURE__ */ React.createElement("div", { className: "sheet-layer", role: "presentation", onMouseDown: (event) => event.target === event.currentTarget && onClose() }, /* @__PURE__ */ React.createElement("section", { className: "settings-sheet", role: "dialog", "aria-modal": "true", "aria-labelledby": "settings-title" }, /* @__PURE__ */ React.createElement("div", { className: "sheet-grabber" }), /* @__PURE__ */ React.createElement("div", { className: "sheet-title" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uB370\uC774\uD130 \uC5F0\uACB0"), /* @__PURE__ */ React.createElement("h2", { id: "settings-title" }, "\uACF5\uC2DD \uAD50\uD1B5 \uB370\uC774\uD130")), /* @__PURE__ */ React.createElement("button", { type: "button", onClick: onClose, "aria-label": "\uB2EB\uAE30" }, /* @__PURE__ */ React.createElement(Icon, { name: "x" }))), /* @__PURE__ */ React.createElement(InlineNotice, { tone: connection.mode === "live" ? "success" : "warning", icon: connection.mode === "live" ? "cloud-check" : "key", title: connection.label }, connection.message), /* @__PURE__ */ React.createElement("label", { className: "api-field" }, /* @__PURE__ */ React.createElement("span", null, "\uB85C\uCEEC \uB370\uC774\uD130 \uC11C\uBE44\uC2A4 \uC8FC\uC18C"), /* @__PURE__ */ React.createElement("input", { value: apiBase, onChange: (event) => setApiBase(event.target.value), placeholder: "http://127.0.0.1:8791/api" })), /* @__PURE__ */ React.createElement("p", { className: "privacy-note" }, /* @__PURE__ */ React.createElement(Icon, { name: "shield-check" }), " TAGO \uC11C\uBE44\uC2A4 \uD0A4\uB294 \uBE0C\uB77C\uC6B0\uC800\uC5D0 \uC785\uB825\uD558\uAC70\uB098 \uC800\uC7A5\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4. \uB85C\uCEEC \uB370\uC774\uD130 \uC11C\uBE44\uC2A4 \uB610\uB294 \uC5F0\uACB0\uB41C upstream\uC5D0\uC11C\uB9CC \uAD00\uB9AC\uD569\uB2C8\uB2E4."), !journey || legs.length === 0 ? /* @__PURE__ */ React.createElement("section", { className: "mapping-settings", "aria-labelledby": "mapping-title" }, /* @__PURE__ */ React.createElement("div", { className: "mapping-settings-head" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uACF5\uC2DD \uC2DD\uBCC4\uC790"), /* @__PURE__ */ React.createElement("h3", { id: "mapping-title" }, "\uB178\uC120 \uB9E4\uD551")), /* @__PURE__ */ React.createElement("strong", null, "0/0")), /* @__PURE__ */ React.createElement(InlineNotice, { tone: "warning", icon: "map-trifold", title: "\uC804\uAD6D \uC5EC\uD589 \uD6C4\uBCF4 \uBA3C\uC800 \uC120\uD0DD" }, "\uC120\uD0DD\uD55C \uD6C4\uBCF4\uC758 \uC5F0\uC18D \uBC84\uC2A4 \uC774\uB3D9 \uAD6C\uAC04\uB9CC cityCode \xB7 nodeId \xB7 routeId \uAC80\uC99D \uB300\uC0C1\uC73C\uB85C \uD45C\uC2DC\uD569\uB2C8\uB2E4. \uAE30\uC874 \uACE0\uC815 \uAD6C\uAC04\uC740 \uC0AC\uC6A9\uD558\uC9C0 \uC54A\uC2B5\uB2C8\uB2E4.")) : /* @__PURE__ */ React.createElement("section", { className: "mapping-settings", "aria-labelledby": "mapping-title" }, /* @__PURE__ */ React.createElement("div", { className: "mapping-settings-head" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, "\uACF5\uC2DD \uC2DD\uBCC4\uC790"), /* @__PURE__ */ React.createElement("h3", { id: "mapping-title" }, "\uB178\uC120 \uB9E4\uD551")), /* @__PURE__ */ React.createElement("strong", null, mappingSummary.verified, "/", mappingSummary.total)), /* @__PURE__ */ React.createElement("p", { className: "mapping-help" }, "\uC2E4\uC2DC\uAC04 \uC2B9\uCC28 \uC815\uB958\uC7A5\uACFC \uD658\uC2B9 \uC7AC\uC0DD \uCCB4\uD06C\uD3EC\uC778\uD2B8\uB97C \uAD6C\uBD84\uD574 \uAC80\uC99D\uD569\uB2C8\uB2E4. cityCode \xB7 nodeId \xB7 routeId\uB9CC \uB85C\uCEEC\uC5D0 \uC800\uC7A5\uD558\uBA70, \uC11C\uBC84\uAC00 \uACF5\uC2DD \uACBD\uC720 \uC815\uB958\uC7A5\uC73C\uB85C \uD655\uC778\uD558\uC9C0 \uBABB\uD558\uBA74 DATA_GAP\uC785\uB2C8\uB2E4."), /* @__PURE__ */ React.createElement("div", { className: "mapping-leg-list" }, legs.map((leg, index) => {
    const mapping = mappings[leg.id] || {};
    const complete = Boolean(mapping.cityCode && mapping.nodeId && mapping.routeId);
    return /* @__PURE__ */ React.createElement("article", { className: "mapping-leg", key: leg.id }, /* @__PURE__ */ React.createElement("div", { className: "mapping-leg-title" }, /* @__PURE__ */ React.createElement("span", null, index + 1), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("strong", null, leg.city, " ", leg.routeNo), /* @__PURE__ */ React.createElement("small", null, leg.transferCheckpoint ? "\uD658\uC2B9 \uC7AC\uC0DD \uCCB4\uD06C\uD3EC\uC778\uD2B8" : "\uC2E4\uC2DC\uAC04 \uC2B9\uCC28", " \xB7 ", leg.board, " \u2192 ", leg.alight, " \xB7 \uC21C\uBC88 ", Number.isInteger(leg.nodeOrder) ? leg.nodeOrder : "DATA_GAP")), /* @__PURE__ */ React.createElement(MappingBadge, { state: mapping.state })), /* @__PURE__ */ React.createElement("div", { className: "mapping-fields" }, /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, "cityCode"), /* @__PURE__ */ React.createElement("input", { value: mapping.cityCode || "", onChange: (event) => onMappingChange(leg.id, "cityCode", event.target.value), inputMode: "numeric", autoComplete: "off", "aria-label": `${leg.city} ${leg.routeNo} cityCode` })), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, "nodeId"), /* @__PURE__ */ React.createElement("input", { value: mapping.nodeId || "", onChange: (event) => onMappingChange(leg.id, "nodeId", event.target.value), autoCapitalize: "characters", autoComplete: "off", "aria-label": `${leg.city} ${leg.routeNo} nodeId` })), /* @__PURE__ */ React.createElement("label", null, /* @__PURE__ */ React.createElement("span", null, "routeId"), /* @__PURE__ */ React.createElement("input", { value: mapping.routeId || "", onChange: (event) => onMappingChange(leg.id, "routeId", event.target.value), autoCapitalize: "characters", autoComplete: "off", "aria-label": `${leg.city} ${leg.routeNo} routeId` }))), /* @__PURE__ */ React.createElement("div", { className: "mapping-leg-foot" }, /* @__PURE__ */ React.createElement("p", null, mapping.note || "\uC11C\uBC84 \uAC80\uC99D \uC804"), /* @__PURE__ */ React.createElement("button", { type: "button", disabled: !complete || mapping.state === "checking", onClick: () => onVerifyMapping(leg.id) }, mapping.state === "checking" ? "\uAC80\uC99D\uC911" : "\uC11C\uBC84 \uAC80\uC99D", /* @__PURE__ */ React.createElement(Icon, { name: mapping.state === "checking" ? "spinner-gap" : "arrow-right" }))));
  }))), settingsError && /* @__PURE__ */ React.createElement(InlineNotice, { tone: "danger", icon: "warning-circle", title: "\uC800\uC7A5\uD560 \uC218 \uC5C6\uC74C" }, settingsError), /* @__PURE__ */ React.createElement("button", { className: "liquid-button settings-save", type: "button", onClick: onReconnect }, "\uC8FC\uC18C\xB7\uC2DD\uBCC4\uC790 \uC800\uC7A5 \uD6C4 \uC5F0\uACB0 \uD655\uC778 ", /* @__PURE__ */ React.createElement(Icon, { name: "plug" }))));
}
