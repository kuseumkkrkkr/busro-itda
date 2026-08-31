const { useEffect, useMemo, useState } = React;

function Icon({ name, label }) {
  return <i className={`ph ph-${name}`} aria-hidden={label ? undefined : "true"} title={label || undefined} />;
}

function SourceBadge({ mode = "offline", label }) {
  const text = label || ({ live: "TAGO LIVE", fixture: "FIXTURE", offline: "연결 대기" }[mode] || mode);
  return <span className={`source-badge ${mode}`}><span className="source-dot" />{text}</span>;
}

function MappingBadge({ state = "unmapped" }) {
  const content = {
    verified: ["check-circle", "검증됨"],
    checking: ["spinner-gap", "검증중"],
    unmapped: ["warning-circle", "미매핑"],
  }[state] || ["warning-circle", "미매핑"];
  return <span className={`mapping-badge ${state}`}><Icon name={content[0]} />{content[1]}</span>;
}

function CoverageStrip({ mappingSummary, coverage, compact = false }) {
  const mapped = mappingSummary?.verified || 0;
  const total = Number.isFinite(mappingSummary?.total) ? mappingSummary.total : 0;
  const passageReady = coverage?.supported && !coverage?.dataGap && coverage?.count > 0;
  return (
    <div className={`coverage-strip ${compact ? "compact" : ""}`} aria-label="공식 데이터 준비 상태">
      <div><span><Icon name="map-pin-line" /> 공식 매핑</span><strong>{mapped}/{total}</strong></div>
      <div><span><Icon name="path" /> 통과 이력</span><strong>{passageReady ? `${coverage.count}건 · ${coverage.eligibleDays}일` : "DATA_GAP"}</strong></div>
    </div>
  );
}

function GlassCard({ className = "", children, as = "section", ...props }) {
  const Element = as;
  return <Element className={`glass-card ${className}`.trim()} {...props}>{children}</Element>;
}

function AppHeader({ connection, onSettings }) {
  return (
    <header className="app-header">
      <div className="brand-lockup">
        <span className="brand-mark"><Icon name="bus" /></span>
        <div><p>시내버스로 이어가는 여행</p><strong>버스로 잇다</strong></div>
      </div>
      {onSettings && <button className="glass-icon-button" type="button" onClick={onSettings} aria-label="운영 설정">
        <Icon name="sliders-horizontal" />
      </button>}
    </header>
  );
}

function BottomDock({ tab, onChange }) {
  const items = [
    ["explore", "house", "홈"],
    ["live", "broadcast", "여행 중"],
    ["simulation", "chart-line-up", "운행 기록"],
    ["journey", "ticket", "내 경로"],
  ];
  return (
    <nav className="bottom-dock" aria-label="주요 메뉴">
      {items.map(([key, icon, label]) => (
        <button key={key} type="button" className={tab === key ? "active" : ""} onClick={() => onChange(key)} aria-current={tab === key ? "page" : undefined}>
          <Icon name={icon} /><span>{label}</span>
        </button>
      ))}
    </nav>
  );
}

function Segmented({ value, options, onChange, label }) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((option) => <button key={option.value} type="button" className={value === option.value ? "active" : ""} onClick={() => onChange(option.value)}>{option.label}</button>)}
    </div>
  );
}

function ProbabilityRing({ value, size = "large", label = "관측 성공률" }) {
  const available = Number.isFinite(value);
  const safeValue = available ? Math.max(0, Math.min(100, value)) : 0;
  return (
    <div className={`probability-ring ${size} ${available ? "" : "unavailable"}`} style={{ "--probability": `${safeValue * 3.6}deg` }} aria-label={available ? `${label} ${value}%` : "관측 데이터 부족"}>
      <div><strong>{available ? value : "—"}</strong>{available && <span>%</span>}<small>{available ? label : "DATA GAP"}</small></div>
    </div>
  );
}

function ScreenHeading({ eyebrow, title, detail, action }) {
  return (
    <div className="screen-heading">
      <div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1>{detail && <p className="screen-detail">{detail}</p>}</div>
      {action}
    </div>
  );
}

function InlineNotice({ tone = "neutral", icon = "info", title, children }) {
  return <div className={`inline-notice ${tone}`}><span><Icon name={icon} /></span><div><strong>{title}</strong><p>{children}</p></div></div>;
}

function LoadingRows({ count = 2 }) {
  return <div className="loading-rows" aria-label="불러오는 중">{Array.from({ length: count }, (_, index) => <div className="loading-row" key={index}><span /><div><i /><i /></div></div>)}</div>;
}
