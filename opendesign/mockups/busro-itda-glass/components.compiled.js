const { useEffect, useMemo, useState } = React;
function Icon({ name, label }) {
  return /* @__PURE__ */ React.createElement("i", { className: `ph ph-${name}`, "aria-hidden": label ? void 0 : "true", title: label || void 0 });
}
function SourceBadge({ mode = "offline", label }) {
  const text = label || ({ live: "TAGO LIVE", fixture: "FIXTURE", offline: "\uC5F0\uACB0 \uB300\uAE30" }[mode] || mode);
  return /* @__PURE__ */ React.createElement("span", { className: `source-badge ${mode}` }, /* @__PURE__ */ React.createElement("span", { className: "source-dot" }), text);
}
function MappingBadge({ state = "unmapped" }) {
  const content = {
    verified: ["check-circle", "\uAC80\uC99D\uB428"],
    checking: ["spinner-gap", "\uAC80\uC99D\uC911"],
    unmapped: ["warning-circle", "\uBBF8\uB9E4\uD551"]
  }[state] || ["warning-circle", "\uBBF8\uB9E4\uD551"];
  return /* @__PURE__ */ React.createElement("span", { className: `mapping-badge ${state}` }, /* @__PURE__ */ React.createElement(Icon, { name: content[0] }), content[1]);
}
function CoverageStrip({ mappingSummary, coverage, compact = false }) {
  const mapped = mappingSummary?.verified || 0;
  const total = Number.isFinite(mappingSummary?.total) ? mappingSummary.total : 0;
  const passageReady = coverage?.supported && !coverage?.dataGap && coverage?.count > 0;
  return /* @__PURE__ */ React.createElement("div", { className: `coverage-strip ${compact ? "compact" : ""}`, "aria-label": "\uACF5\uC2DD \uB370\uC774\uD130 \uC900\uBE44 \uC0C1\uD0DC" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "map-pin-line" }), " \uACF5\uC2DD \uB9E4\uD551"), /* @__PURE__ */ React.createElement("strong", null, mapped, "/", total)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: "path" }), " \uD1B5\uACFC \uC774\uB825"), /* @__PURE__ */ React.createElement("strong", null, passageReady ? `${coverage.count}\uAC74 \xB7 ${coverage.eligibleDays}\uC77C` : "DATA_GAP")));
}
function GlassCard({ className = "", children, as = "section", ...props }) {
  const Element = as;
  return /* @__PURE__ */ React.createElement(Element, { className: `glass-card ${className}`.trim(), ...props }, children);
}
function AppHeader({ connection, onSettings }) {
  return /* @__PURE__ */ React.createElement("header", { className: "app-header" }, /* @__PURE__ */ React.createElement("div", { className: "brand-lockup" }, /* @__PURE__ */ React.createElement("span", { className: "brand-mark" }, /* @__PURE__ */ React.createElement(Icon, { name: "bus" })), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", null, "\uC2DC\uB0B4\uBC84\uC2A4\uB85C \uC774\uC5B4\uAC00\uB294 \uC5EC\uD589"), /* @__PURE__ */ React.createElement("strong", null, "\uBC84\uC2A4\uB85C \uC787\uB2E4"))), onSettings && /* @__PURE__ */ React.createElement("button", { className: "glass-icon-button", type: "button", onClick: onSettings, "aria-label": "\uC6B4\uC601 \uC124\uC815" }, /* @__PURE__ */ React.createElement(Icon, { name: "sliders-horizontal" })));
}
function BottomDock({ tab, onChange }) {
  const items = [
    ["explore", "house", "\uD648"],
    ["live", "broadcast", "\uC5EC\uD589 \uC911"],
    ["simulation", "chart-line-up", "\uC6B4\uD589 \uAE30\uB85D"],
    ["journey", "ticket", "\uB0B4 \uACBD\uB85C"]
  ];
  return /* @__PURE__ */ React.createElement("nav", { className: "bottom-dock", "aria-label": "\uC8FC\uC694 \uBA54\uB274" }, items.map(([key, icon, label]) => /* @__PURE__ */ React.createElement("button", { key, type: "button", className: tab === key ? "active" : "", onClick: () => onChange(key), "aria-current": tab === key ? "page" : void 0 }, /* @__PURE__ */ React.createElement(Icon, { name: icon }), /* @__PURE__ */ React.createElement("span", null, label))));
}
function Segmented({ value, options, onChange, label }) {
  return /* @__PURE__ */ React.createElement("div", { className: "segmented", role: "group", "aria-label": label }, options.map((option) => /* @__PURE__ */ React.createElement("button", { key: option.value, type: "button", className: value === option.value ? "active" : "", onClick: () => onChange(option.value) }, option.label)));
}
function ProbabilityRing({ value, size = "large", label = "\uAD00\uCE21 \uC131\uACF5\uB960" }) {
  const available = Number.isFinite(value);
  const safeValue = available ? Math.max(0, Math.min(100, value)) : 0;
  return /* @__PURE__ */ React.createElement("div", { className: `probability-ring ${size} ${available ? "" : "unavailable"}`, style: { "--probability": `${safeValue * 3.6}deg` }, "aria-label": available ? `${label} ${value}%` : "\uAD00\uCE21 \uB370\uC774\uD130 \uBD80\uC871" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("strong", null, available ? value : "\u2014"), available && /* @__PURE__ */ React.createElement("span", null, "%"), /* @__PURE__ */ React.createElement("small", null, available ? label : "DATA GAP")));
}
function ScreenHeading({ eyebrow, title, detail, action }) {
  return /* @__PURE__ */ React.createElement("div", { className: "screen-heading" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("p", { className: "eyebrow" }, eyebrow), /* @__PURE__ */ React.createElement("h1", null, title), detail && /* @__PURE__ */ React.createElement("p", { className: "screen-detail" }, detail)), action);
}
function InlineNotice({ tone = "neutral", icon = "info", title, children }) {
  return /* @__PURE__ */ React.createElement("div", { className: `inline-notice ${tone}` }, /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement(Icon, { name: icon })), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("strong", null, title), /* @__PURE__ */ React.createElement("p", null, children)));
}
function LoadingRows({ count = 2 }) {
  return /* @__PURE__ */ React.createElement("div", { className: "loading-rows", "aria-label": "\uBD88\uB7EC\uC624\uB294 \uC911" }, Array.from({ length: count }, (_, index) => /* @__PURE__ */ React.createElement("div", { className: "loading-row", key: index }, /* @__PURE__ */ React.createElement("span", null), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("i", null), /* @__PURE__ */ React.createElement("i", null)))));
}
