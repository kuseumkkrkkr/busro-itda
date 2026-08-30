const { useEffect, useMemo, useState } = React;

const STORAGE_KEY = "busro-itda-demo-v1";

const PRESETS = {
  document: {
    from: "조치원버스터미널",
    to: "부산 구포역",
    date: "2026-09-01",
    time: "07:00",
    journeyType: "auto",
    walkingLimit: 800,
    priority: "safe",
  },
};

const ROUTES = [
  { id: "B", name: "안전 우선", duration: "17시간 55분", rides: "18회", walk: "0.8km", walkMeters: 800, trust: "B", precision: "S2", overnight: "당일", basis: "2026.08.27", warning: "영천 환승은 14분 여유", vulnerable: "취약 환승 1회 · 영천 14분", tag: "추천" },
  { id: "C", name: "최단·도전", duration: "17시간 32분", rides: "19회", walk: "2.8km", walkMeters: 2800, trust: "C", precision: "S1", overnight: "당일", basis: "2026.08.27", warning: "일반 도보 한도 초과, 출발 전 공식 BIS 확인", vulnerable: "취약 환승 2회 · 긴 보행", tag: "도전" },
  { id: "A", name: "최소 도보", duration: "1박 2일", rides: "16회", walk: "0.4km", walkMeters: 400, trust: "A", precision: "S3", overnight: "1박", basis: "2026.08.27", warning: "숙박 거점과 다음 날 첫차를 재확인", vulnerable: "취약 환승 없음 · 숙박 전환", tag: "추천" },
];

const BASE_LEGS = [
  { no: "세종 601", at: "07:00", role: "출발 연결", direction: "세종시청 방면", board: "조치원버스터미널", alight: "세종시청", wait: "9분", walk: "180m", buffer: "18분", remaining: 2 },
  { no: "대전 607", at: "09:10", role: "광역 연결", direction: "대전역 방면", board: "세종시청", alight: "대전역 동광장", wait: "11분", walk: "260m", buffer: "20분", remaining: 3 },
  { no: "김천 11-6", at: "13:50", role: "중간 도시 연결", direction: "김천터미널 방면", board: "DEMO 환승 정류장", alight: "김천터미널", wait: "17분", walk: "90m", buffer: "22분", remaining: 2 },
  { no: "영천 55", at: "19:05", role: "취약 경계 연결", direction: "영천시외버스정류장 방면", board: "DEMO 기점", alight: "영천시외버스정류장", wait: "13분", walk: "70m", buffer: "14분", remaining: 2 },
  { no: "울산 1224", at: "23:20", role: "도착 연결", direction: "구포역 방면", board: "DEMO 환승 정류장", alight: "부산 구포역", wait: "8분", walk: "200m", buffer: "16분", remaining: 3 },
];

const ROUTE_DETAILS = {
  B: {
    range: "07:00 → 00:35", trust: "B", precision: "S2", note: "문서 고정 안전 경로 DEMO", risk: "영천 55 환승 여유 14분. 3분 이상 지연 시 B 복구안 검토.", prepare: "영천 취약 환승과 00:35 도착 시연값을 공식 채널에서 재확인", safeEnd: "대전역 광장 안내소",
    legs: BASE_LEGS.map((leg, index) => ({ ...leg, source: index === 3 ? "S3 · 핵심 경계 회차별 확정 DEMO" : "S2 · 구간추정" })),
  },
  C: {
    range: "07:00 → 00:32", trust: "C", precision: "S1", note: "최단·도전 변형 DEMO", risk: "총 도보 2.8km와 두 번의 취약 환승. 일반 추천에서 제외.", prepare: "긴 보행·C등급·공식 BIS 재확인을 모두 수용할 때만 시작", safeEnd: "김천역 공공 대기공간",
    legs: BASE_LEGS.map((leg, index) => ({ ...leg, at: ["07:00", "08:58", "13:32", "18:48", "23:05"][index], walk: ["420m", "680m", "750m", "510m", "440m"][index], buffer: ["10분", "8분", "11분", "7분", "9분"][index], source: "S1 · 배차추정" })),
  },
  A: {
    range: "07:00 → 다음 날 11:10", trust: "A", precision: "S3", note: "최소도보·1박 변형 DEMO", risk: "영천 숙박 전환 후 다음 날 첫차 이용. 숙박 거점 운영 여부 확인.", prepare: "1박 거점·다음 날 첫차·야간 보행 0.4km 조건 재확인", safeEnd: "영천 공공 숙박 거점",
    legs: BASE_LEGS.map((leg, index) => ({ ...leg, at: ["07:00", "09:25", "14:20", "20:10", "다음 날 08:30"][index], walk: ["80m", "90m", "70m", "60m", "100m"][index], buffer: ["24분", "31분", "28분", "숙박", "35분"][index], source: "S3 · 회차별 확정 DEMO" })),
  },
};

function routeAvailability(route, form) {
  const reasons = [];
  if (route.walkMeters > form.walkingLimit) reasons.push(`도보 한도 ${form.walkingLimit}m 초과`);
  if (form.journeyType === "day" && route.overnight !== "당일") reasons.push("당일 조건과 불일치");
  if (["overnight", "twoDays"].includes(form.journeyType) && route.overnight !== "1박") reasons.push("다일 여정 조건과 불일치");
  if (route.id === "C" && form.priority !== "challenge") reasons.push("도전 우선 동의 필요");
  return { allowed: reasons.length === 0, reasons };
}

const SCREEN_WHITELIST = new Set(["home", "loading", "results", "detail", "risk", "prepare", "ride", "delay", "alight", "complete", "safeEnd", "noPath", "dataGap", "journey", "saved", "records", "settings"]);
const TAB_WHITELIST = new Set(["explore", "journey", "saved", "records", "settings"]);
const DEFAULT_APP = {
  screen: "home",
  tab: "explore",
  form: { ...PRESETS.document },
  preset: "document",
  demoState: "normal",
  selectedRoute: "B",
  savedRoutes: [],
  records: [],
  detailView: "schedule",
  ackOfficial: false,
  ackRisk: false,
  ackChallenge: false,
  prep: { battery: false, contact: false, official: false },
  journeyLeg: 2,
  remainingStops: BASE_LEGS[2].remaining,
  recovery: null,
  journeyStarted: false,
  activeRouteNo: null,
  activeDirection: null,
  activeBoard: null,
  activeAlight: null,
  activeBuffer: null,
  expectedArrival: null,
  gapExpanded: false,
  trustExpanded: false,
  shareStatus: "",
  guardMessage: "",
  resetReady: false,
  settings: {
    walkingDefault: 800,
    delayAlerts: true,
    offlineSchedule: true,
    dataSaver: false,
  },
};

function loadInitialState() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return DEFAULT_APP;
    const selectedRoute = ROUTE_DETAILS[saved.selectedRoute] ? saved.selectedRoute : "B";
    const maxLeg = ROUTE_DETAILS[selectedRoute].legs.length - 1;
    const savedForm = saved.form && typeof saved.form === "object" ? saved.form : {};
    const safeForm = {
      ...DEFAULT_APP.form,
      ...savedForm,
      from: typeof savedForm.from === "string" ? savedForm.from : DEFAULT_APP.form.from,
      to: typeof savedForm.to === "string" ? savedForm.to : DEFAULT_APP.form.to,
      date: typeof savedForm.date === "string" ? savedForm.date : DEFAULT_APP.form.date,
      time: typeof savedForm.time === "string" ? savedForm.time : DEFAULT_APP.form.time,
      journeyType: ["day", "overnight", "twoDays", "auto"].includes(savedForm.journeyType) ? savedForm.journeyType : "auto",
      priority: ["safe", "fast", "walk", "transfer", "challenge"].includes(savedForm.priority) ? savedForm.priority : "safe",
      walkingLimit: Math.max(200, Math.min(3000, Number(savedForm.walkingLimit) || 800)),
    };
    return {
      ...DEFAULT_APP,
      ...saved,
      screen: SCREEN_WHITELIST.has(saved.screen) ? saved.screen : "home",
      tab: TAB_WHITELIST.has(saved.tab) ? saved.tab : "explore",
      selectedRoute,
      journeyLeg: Math.max(0, Math.min(Number(saved.journeyLeg) || 0, maxLeg)),
      ackOfficial: saved.ackOfficial === true,
      ackRisk: saved.ackRisk === true,
      ackChallenge: saved.ackChallenge === true,
      journeyStarted: saved.journeyStarted === true,
      activeRouteNo: typeof saved.activeRouteNo === "string" ? saved.activeRouteNo : null,
      activeDirection: typeof saved.activeDirection === "string" ? saved.activeDirection : null,
      activeBoard: typeof saved.activeBoard === "string" ? saved.activeBoard : null,
      activeAlight: typeof saved.activeAlight === "string" ? saved.activeAlight : null,
      activeBuffer: typeof saved.activeBuffer === "string" ? saved.activeBuffer : null,
      expectedArrival: typeof saved.expectedArrival === "string" ? saved.expectedArrival : null,
      recovery: ["A", "B", "C"].includes(saved.recovery) ? saved.recovery : null,
      detailView: ["schedule", "map"].includes(saved.detailView) ? saved.detailView : "schedule",
      demoState: ["normal", "noPath", "dataGap"].includes(saved.demoState) ? saved.demoState : "normal",
      gapExpanded: saved.gapExpanded === true,
      trustExpanded: saved.trustExpanded === true,
      resetReady: saved.resetReady === true,
      shareStatus: typeof saved.shareStatus === "string" ? saved.shareStatus : "",
      guardMessage: typeof saved.guardMessage === "string" ? saved.guardMessage : "",
      remainingStops: Math.max(0, Math.min(99, Number.isFinite(Number(saved.remainingStops)) ? Number(saved.remainingStops) : DEFAULT_APP.remainingStops)),
      form: safeForm,
      savedRoutes: Array.isArray(saved.savedRoutes) ? saved.savedRoutes.filter((id) => ROUTE_DETAILS[id]) : [],
      records: Array.isArray(saved.records) ? saved.records.filter((record) => record && typeof record === "object").slice(0, 20).map((record, index) => ({
        id: typeof record.id === "number" || typeof record.id === "string" ? record.id : `restored-${index}`,
        outcome: typeof record.outcome === "string" ? record.outcome : "기록",
        route: typeof record.route === "string" ? record.route : "DEMO 여정",
        when: typeof record.when === "string" ? record.when : "날짜 미확인",
        actual: typeof record.actual === "string" ? record.actual : "--:--",
        delay: typeof record.delay === "string" ? record.delay : "확인 안 됨",
        status: typeof record.status === "string" ? record.status : "복원됨",
      })) : [],
      prep: saved.prep && typeof saved.prep === "object" ? {
        battery: saved.prep.battery === true,
        contact: saved.prep.contact === true,
        official: saved.prep.official === true,
      } : { ...DEFAULT_APP.prep },
      settings: saved.settings && typeof saved.settings === "object" ? {
        walkingDefault: Math.max(200, Math.min(3000, Number(saved.settings.walkingDefault) || 800)),
        delayAlerts: saved.settings.delayAlerts !== false,
        offlineSchedule: saved.settings.offlineSchedule !== false,
        dataSaver: saved.settings.dataSaver === true,
      } : { ...DEFAULT_APP.settings },
    };
  } catch {
    return DEFAULT_APP;
  }
}

function DataTag({ kind = "confirmed", children }) {
  return <span className={`tag ${kind}`}>{children}</span>;
}

function ScreenTitle({ eyebrow, title, lede, onBack }) {
  return (
    <div className="section-head">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h2>{title}</h2>
        {lede && <p className="lede">{lede}</p>}
      </div>
      {onBack && <button className="icon-button" type="button" onClick={onBack} aria-label="이전 화면">←</button>}
    </div>
  );
}

function SwitchRow({ title, detail, checked, onChange }) {
  return (
    <div className="toggle-row">
      <div className="toggle-copy">
        <strong>{title}</strong>
        <small>{detail}</small>
      </div>
      <button className={`switch ${checked ? "on" : ""}`} type="button" role="switch" aria-checked={checked} onClick={() => onChange(!checked)} aria-label={`${title} ${checked ? "끄기" : "켜기"}`} />
    </div>
  );
}

function HomeScreen({ app, setApp, search }) {
  const setForm = (patch) => setApp((state) => ({ ...state, guardMessage: "", form: { ...state.form, ...patch } }));
  const invalid = !app.form.from.trim() || !app.form.to.trim() || !app.form.date || !app.form.time;
  const journeyLabels = { day: "당일", overnight: "1박", twoDays: "2일", auto: "자동" };
  const priorityLabels = { safe: "안전", fast: "최단", walk: "최소도보", transfer: "최소환승", challenge: "도전" };

  return (
    <main className="screen">
      <p className="eyebrow">City bus field journal</p>
      <h1>버스만으로<br />어디까지 이어질까요?</h1>
      <p className="lede">빠른 길보다, 놓치지 않을 수 있는 길을 먼저 찾습니다.</p>

      <div className="notice-box">
        <h3>문서 고정 검증 샘플</h3>
        <p>조치원버스터미널 → 부산 구포역 · 2026-09-01 평일 07:00 · 전 항목 DEMO</p>
      </div>

      <section className="search-panel" aria-label="여정 검색 조건">
        <div className="field-grid">
          <label className="field"><span>출발</span><input value={app.form.from} onChange={(event) => setForm({ from: event.target.value })} placeholder="출발 정류장" /></label>
          <label className="field"><span>도착</span><input value={app.form.to} onChange={(event) => setForm({ to: event.target.value })} placeholder="도착 정류장" /></label>
          <div className="field-row">
            <label className="field"><span>날짜 · 평일 샘플</span><input type="date" value={app.form.date} onChange={(event) => setForm({ date: event.target.value })} /></label>
            <label className="field"><span>출발 시각</span><input type="time" value={app.form.time} onChange={(event) => setForm({ time: event.target.value })} /></label>
          </div>
          <div className="field-row">
            <label className="field">
              <span>여정 유형</span>
              <select value={app.form.journeyType} onChange={(event) => setForm({ journeyType: event.target.value })}>
                <option value="day">당일</option><option value="overnight">1박</option><option value="twoDays">2일</option><option value="auto">자동</option>
              </select>
            </label>
            <label className="field">
              <span>우선순위</span>
              <select value={app.form.priority} onChange={(event) => setForm({ priority: event.target.value })}>
                <option value="safe">안전 우선</option><option value="fast">최단</option><option value="walk">최소도보</option><option value="transfer">최소환승</option><option value="challenge">도전</option>
              </select>
            </label>
          </div>
          <label className="field">
            <span>구간별 도보 한도 · {app.form.walkingLimit}m</span>
            <input type="range" min="200" max="3000" step="100" value={app.form.walkingLimit} onChange={(event) => setForm({ walkingLimit: Number(event.target.value) })} />
          </label>
        </div>
        <div className="tag-row">
          <DataTag kind="confirmed">{journeyLabels[app.form.journeyType]} 여정</DataTag>
          <DataTag kind={app.form.priority === "challenge" ? "estimated" : "confirmed"}>{priorityLabels[app.form.priority]} 우선</DataTag>
          <DataTag kind={app.form.walkingLimit > 800 ? "estimated" : "confirmed"}>도보 {app.form.walkingLimit}m</DataTag>
        </div>
        <button className="text-button settings-apply" type="button" onClick={() => setForm({ walkingLimit: app.settings.walkingDefault })}>설정의 도보 기본값 {app.settings.walkingDefault}m 적용</button>
      </section>

      <p className="section-label">검색 결과 상태 시연</p>
      <div className="status-switch" role="group" aria-label="검색 결과 상태">
        {[["normal", "경로 있음"], ["noPath", "NO_PATH"], ["dataGap", "DATA_GAP"]].map(([value, label]) => (
          <button key={value} type="button" className={`segment-button ${app.demoState === value ? "active" : ""}`} onClick={() => setApp((state) => ({ ...state, demoState: value }))}>{label}</button>
        ))}
      </div>
      <p className="helper">NO_PATH는 검증 범위에서 조건 충족 경로 없음, DATA_GAP은 필수 원천 누락입니다.</p>
      {invalid && <p className="error-text" role="alert">출발·도착·날짜·시각을 모두 입력해 주세요.</p>}
      <div className="button-stack"><button className="primary-button" type="button" disabled={invalid} onClick={search}>경로 찾기</button></div>
      <p className="fineprint">데이터 기준일 2026.08.27 · 조회일 2026.08.31. 실제 출발 전 지자체·운송기관의 공식 정보를 확인하세요.</p>
    </main>
  );
}

function LoadingScreen({ app }) {
  const journey = { day: "당일", overnight: "1박", twoDays: "2일", auto: "자동" }[app.form.journeyType];
  const priority = { safe: "안전", fast: "최단", walk: "최소도보", transfer: "최소환승", challenge: "도전" }[app.form.priority];
  return (
    <main className="screen">
      <div className="big-status" aria-live="polite">
        <div>
          <div className="loading-mark"><span className="loading-dot" /></div>
          <h2>도보와 환승 여유를<br />함께 계산 중입니다</h2>
          <p className="lede">{app.form.from} → {app.form.to}</p>
          <div className="tag-row" style={{ justifyContent: "center" }}>
            <DataTag kind={app.form.walkingLimit > 800 ? "estimated" : "confirmed"}>도보 {app.form.walkingLimit}m</DataTag>
            <DataTag kind={app.form.priority === "challenge" ? "estimated" : "confirmed"}>{priority} 우선</DataTag>
            <DataTag kind="confirmed">{journey} 여정</DataTag>
            <DataTag kind="estimated">DEMO 계산</DataTag>
          </div>
        </div>
      </div>
    </main>
  );
}

function ResultsScreen({ app, setApp, selectRoute, toggleSave }) {
  const order = ["walk", "transfer"].includes(app.form.priority) ? ["A", "B", "C"] : app.form.priority === "challenge" || app.form.priority === "fast" ? ["C", "B", "A"] : ["B", "A", "C"];
  const orderedRoutes = order.map((id) => ROUTES.find((route) => route.id === id));
  const journey = { day: "당일", overnight: "1박", twoDays: "2일", auto: "자동" }[app.form.journeyType];
  const priority = { safe: "안전", fast: "최단", walk: "최소도보", transfer: "최소환승", challenge: "도전" }[app.form.priority];
  const applyChallenge = () => setApp((state) => ({ ...state, selectedRoute: "C", guardMessage: "", form: { ...state.form, priority: "challenge", walkingLimit: 3000, journeyType: "day" } }));

  return (
    <main className="screen">
      <ScreenTitle eyebrow="Top 3 · DEMO comparison" title="세 가지 여정 비교" lede={`${app.form.from} → ${app.form.to}`} onBack={() => setApp((s) => ({ ...s, screen: "home" }))} />
      <div className="notice-box success">
        <h3>{priority} 우선 정렬 · {journey} · 도보 {app.form.walkingLimit}m</h3>
        <p>데이터 기준일 2026.08.27 · 조회일 2026.08.31. C 도전 경로는 일반 추천과 분리합니다.</p>
      </div>
      {app.guardMessage && <div className="notice-box danger" role="alert"><h3>조건 확인 필요</h3><p>{app.guardMessage}</p></div>}
      <button className="trust-button" type="button" aria-expanded={app.trustExpanded} onClick={() => setApp((s) => ({ ...s, trustExpanded: !s.trustExpanded }))}>
        신뢰등급·정밀도 정책 {app.trustExpanded ? "접기" : "보기"}
      </button>
      {app.trustExpanded && (
        <section className="trust-panel">
          <p><strong>A 90–100</strong> 추천 · <strong>B 75–89</strong> 주의 후 추천 · <strong>C 60–74</strong> 참고/도전 · <strong>D/E 0–59</strong> 기본 제외 · <strong>U</strong> 미수집</p>
          <p><strong>S3</strong> 회차별 stop_times · <strong>S2</strong> 기점+구간추정(최대 B) · <strong>S1</strong> 첫·막차/배차(최대 C) · <strong>S0</strong> 연결만 확인(추천 제외)</p>
          <p>핵심 경계에 회차별 시각이 없으면 최대 C, 핵심 연결 하나가 E이면 전체 최대 D입니다.</p>
        </section>
      )}
      {orderedRoutes.map((route) => {
        const saved = app.savedRoutes.includes(route.id);
        const availability = routeAvailability(route, app.form);
        const locked = !availability.allowed;
        const open = () => { if (!locked) selectRoute(route.id); };
        return (
          <article className={`ticket result-ticket ${app.selectedRoute === route.id ? "selected" : ""} ${locked ? "locked" : ""}`} key={route.id} role={locked ? "group" : "button"} tabIndex={locked ? -1 : 0} aria-label={locked ? `${route.name} 경로, 조건 미충족` : `${route.name} 상세 열기`} onClick={open} onKeyDown={(event) => { if (!locked && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); open(); } }}>
            <div className="ticket-top">
              <span className="route-letter">{route.id}</span>
              <div className="route-title"><h3>{route.name}</h3><p>기준일 {route.basis} · {route.overnight}</p></div>
              <div className="time-block"><strong>{route.duration}</strong><span className="demo-tiny">DEMO</span></div>
            </div>
            <div className="route-metrics">
              <div><span>탑승</span><strong>{route.rides}</strong></div>
              <div><span>총 도보</span><strong>{route.walk}</strong></div>
              <div><span>신뢰·정밀도</span><strong>{route.trust} · {route.precision}</strong></div>
            </div>
            <div className="tag-row">
              <DataTag kind={route.id === "C" ? "estimated" : "confirmed"}>{route.tag} · 신뢰 {route.trust}</DataTag>
              <DataTag kind="warning">{route.vulnerable}</DataTag>
            </div>
            <p className="warning-copy">주의 · {route.warning}</p>
            {locked && <p className="lock-copy">UNAVAILABLE · {availability.reasons.join(" · ")}</p>}
            <div className="inline-buttons">
              <button className="secondary-button" type="button" disabled={locked} onClick={(event) => { event.stopPropagation(); toggleSave(route.id); }}>{saved ? "저장 해제" : "여정 저장"}</button>
              {locked && route.id === "C" ? <button className="danger-button" type="button" onClick={(event) => { event.stopPropagation(); applyChallenge(); }}>도전 조건 적용</button> : locked ? <button className="secondary-button" type="button" onClick={(event) => { event.stopPropagation(); setApp((state) => ({ ...state, screen: "home", tab: "explore" })); }}>조건 조정</button> : <button className="primary-button" type="button" onClick={(event) => { event.stopPropagation(); selectRoute(route.id); }}>상세 일정</button>}
            </div>
          </article>
        );
      })}
    </main>
  );
}

function RouteTimeline({ details }) {
  return (
    <div className="route-ribbon">
      {details.legs.map((leg) => (
        <article className="route-stop" key={leg.no}>
          <div className="route-stop-head"><span className="route-no">{leg.at} · {leg.no}</span><span className="role">{leg.role}</span></div>
          <p>{leg.direction}<br /><strong>{leg.board}</strong> 승차 → <strong>{leg.alight}</strong> 하차</p>
          <div className="mini-metrics"><span>대기 {leg.wait}</span><span>도보 {leg.walk}</span><span>여유 {leg.buffer}</span><span>{leg.source}</span></div>
        </article>
      ))}
    </div>
  );
}

function DetailScreen({ app, setApp, toggleSave }) {
  const route = ROUTES.find((item) => item.id === app.selectedRoute) || ROUTES[0];
  const details = ROUTE_DETAILS[route.id];
  const saved = app.savedRoutes.includes(route.id);
  const share = async () => {
    const link = `https://prototype.local/busro-itda?route=${route.id}&demo=1`;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(link);
      setApp((s) => ({ ...s, shareStatus: "DEMO 공유 링크를 복사했습니다." }));
    } catch {
      setApp((s) => ({ ...s, shareStatus: "이 환경에서는 클립보드 복사를 지원하지 않습니다." }));
    }
  };

  return (
    <main className="screen">
      <ScreenTitle eyebrow={`Route ${route.id} · trust ${route.trust} · ${route.precision}`} title={`${route.name} 상세`} lede={`${details.range} · ${route.duration} · ${details.note}`} onBack={() => setApp((s) => ({ ...s, screen: "results", shareStatus: "" }))} />
      <div className="tag-row">
        <DataTag kind={route.precision === "S3" ? "live" : route.precision === "S2" ? "confirmed" : "estimated"}>{route.precision} · 신뢰 {route.trust}</DataTag>
        <DataTag kind="confirmed">기준 2026.08.27</DataTag>
        <DataTag kind="estimated">조회 2026.08.31</DataTag>
      </div>
      <div className="view-toggle" role="group" aria-label="상세 보기 방식">
        <button className={`segment-button ${app.detailView === "schedule" ? "active" : ""}`} type="button" onClick={() => setApp((s) => ({ ...s, detailView: "schedule" }))}>일정표</button>
        <button className={`segment-button ${app.detailView === "map" ? "active" : ""}`} type="button" onClick={() => setApp((s) => ({ ...s, detailView: "map" }))}>지도 관계도</button>
      </div>
      {app.detailView === "schedule" ? <RouteTimeline details={details} /> : (
        <div className="map-field" aria-label="실제 지도가 아닌 노선 순서 관계도">
          <span className="map-node n1" /><span className="map-node n2" /><span className="map-node n3" /><span className="map-node n4" />
          <div className="map-route-labels"><strong>601</strong><span>→</span><strong>607</strong><span>→</span><strong>11-6</strong><span>→</span><strong>55</strong><span>→</span><strong>1224</strong></div>
          <p className="map-caption">실제 위치·거리 지도가 아닌 DEMO 노선 순서 관계도</p>
        </div>
      )}
      <section className="paper-section">
        <h3>원천 스냅샷</h3>
        <ul className="source-list">
          <li>버전 bus-demo-20260827-{route.id}.json · 조회일 2026.08.31</li>
          <li>{route.precision} 원천 · {details.legs[0].source} · 전체 등급 {route.trust}</li>
          <li>실제 연동 시 지자체 BIS·운송기관 공식 링크를 제공합니다.</li>
        </ul>
      </section>
      <div className="notice-box warning"><h3>경로별 위험</h3><p>{details.risk}</p></div>
      <div className="inline-buttons">
        <button className="secondary-button" type="button" onClick={() => toggleSave(route.id)}>{saved ? "저장 삭제" : "일정 저장"}</button>
        <button className="secondary-button" type="button" onClick={share}>공유 링크 복사</button>
        <button className="secondary-button" type="button" onClick={() => window.print()}>PDF로 저장·인쇄</button>
        <button className="primary-button" type="button" onClick={() => setApp((s) => ({ ...s, screen: "risk" }))}>위험 확인</button>
      </div>
      {app.shareStatus && <p className="share-status" role="status">{app.shareStatus}</p>}
    </main>
  );
}

function RiskScreen({ app, setApp }) {
  const route = ROUTES.find((item) => item.id === app.selectedRoute) || ROUTES[0];
  const details = ROUTE_DETAILS[route.id];
  const ready = app.ackOfficial && app.ackRisk && (route.id !== "C" || app.ackChallenge);
  return (
    <main className="screen">
      <ScreenTitle eyebrow="Required gate" title="출발 전 두 가지 확인" lede="체크를 완료해야 여행 준비로 이동합니다." onBack={() => setApp((s) => ({ ...s, screen: "detail" }))} />
      <div className="notice-box danger"><h3>실제 운행 정보가 아닙니다</h3><p>이 화면의 노선·시각·실시간 표시는 UI 시연 데이터입니다.</p></div>
      <div className="notice-box warning"><h3>{route.id} · 신뢰 {route.trust} · {route.precision}</h3><p>{details.risk}</p></div>
      <section className="paper-section">
        <label className="check-row">
          <span className="check-copy"><strong>공식 운행 정보 확인</strong><small>지자체·운송기관의 당일 시간표와 막차를 별도로 확인</small></span>
          <input type="checkbox" checked={app.ackOfficial} onChange={(e) => setApp((s) => ({ ...s, ackOfficial: e.target.checked }))} />
        </label>
        <label className="check-row">
          <span className="check-copy"><strong>중단 기준과 안전 종료점 확인</strong><small>환승 실패 시 무리하게 이동하지 않고 공공시설에서 종료</small></span>
          <input type="checkbox" checked={app.ackRisk} onChange={(e) => setApp((s) => ({ ...s, ackRisk: e.target.checked }))} />
        </label>
        {route.id === "C" && <label className="check-row">
          <span className="check-copy"><strong>도전 조건 개별 동의</strong><small>총 도보 2.8km와 취약 환승 2회를 일반 추천 밖에서 감수</small></span>
          <input type="checkbox" checked={app.ackChallenge} onChange={(e) => setApp((s) => ({ ...s, ackChallenge: e.target.checked }))} />
        </label>}
      </section>
      {!ready && <p className="helper">{route.id === "C" ? "세 항목" : "두 항목"}을 모두 체크하면 다음 단계가 열립니다.</p>}
      <div className="button-stack"><button className="primary-button" type="button" disabled={!ready} onClick={() => setApp((s) => ({ ...s, screen: "prepare" }))}>여행 준비로 이동</button></div>
    </main>
  );
}

function PrepareScreen({ app, setApp, startJourney }) {
  const setPrep = (key, checked) => setApp((s) => ({ ...s, prep: { ...s.prep, [key]: checked } }));
  const ready = Object.values(app.prep).every(Boolean);
  const route = ROUTES.find((item) => item.id === app.selectedRoute) || ROUTES[0];
  const details = ROUTE_DETAILS[route.id];
  return (
    <main className="screen">
      <ScreenTitle eyebrow="Ready to depart" title="여행 준비" lede="긴 시내버스 여행은 작은 확인이 가장 큰 안전장치입니다." onBack={() => setApp((s) => ({ ...s, screen: "risk" }))} />
      <section className="paper-section">
        {[["battery", "배터리·보조배터리", "도착정보 확인을 위한 전원"], ["contact", "비상 연락처·안전 종료점", `${details.safeEnd}를 DEMO 종료점으로 지정`], ["official", "출발 직전 공식 정보 재확인", details.prepare]].map(([key, title, detail]) => (
          <label className="check-row" key={key}>
            <span className="check-copy"><strong>{title}</strong><small>{detail}</small></span>
            <input type="checkbox" checked={app.prep[key]} onChange={(e) => setPrep(key, e.target.checked)} />
          </label>
        ))}
      </section>
      <div className="notice-box"><h3>선택 여정 {route.id} · {details.note}</h3><p>{details.legs[2].at} {details.legs[2].no} 현재 탑승 장면부터 시연합니다.</p></div>
      <div className="button-stack"><button className="primary-button" type="button" disabled={!ready} onClick={startJourney}>DEMO 여행 시작</button></div>
    </main>
  );
}

function RideScreen({ app, setApp }) {
  const route = ROUTES.find((item) => item.id === app.selectedRoute) || ROUTES[0];
  const details = ROUTE_DETAILS[route.id];
  const leg = details.legs[app.journeyLeg];
  const next = details.legs[Math.min(app.journeyLeg + 1, details.legs.length - 1)];
  const progress = Math.round(((app.journeyLeg + 0.35) / details.legs.length) * 100);
  const routeNo = leg.no;
  const nextRouteNo = app.activeRouteNo || next.no;
  const buffer = app.activeBuffer || next.buffer;
  const arrival = app.expectedArrival || details.range.split("→").pop().trim();

  return (
    <main className="screen">
      <ScreenTitle eyebrow={`Journey ${route.id} · Leg ${app.journeyLeg + 1}/${details.legs.length}`} title="현재 탑승" lede={`${details.note} · 실시간 표시는 DEMO 재생`} />
      <div className="progress-track"><div className="progress-fill" style={{ width: `${progress}%` }} /></div>
      <p className="helper">전체 여정 DEMO 진행률 {progress}% · 예상 도착 {arrival}</p>
      <section className="ride-board">
        <p className="eyebrow">탑승 중 · 실시간 DEMO</p>
        <div className="ride-route-no">{routeNo}</div>
        <strong>{leg.direction}</strong>
        <p>{leg.board} → {leg.alight}</p>
      </section>
      <div className="metric-row">
        <div><span>남은 정류장</span><strong>{app.remainingStops}개</strong></div>
        <div><span>다음 노선</span><strong>{nextRouteNo}</strong></div>
        <div><span>환승 여유</span><strong>+{buffer}</strong></div>
      </div>
      {app.recovery && <div className="notice-box success"><h3>복구 {app.recovery}안 적용</h3><p>다음 {nextRouteNo} · 환승 +{buffer} · 예상 도착 {arrival} DEMO</p></div>}
      <div className="tag-row">
        <DataTag kind={app.settings.offlineSchedule ? "confirmed" : "estimated"}>오프라인 일정 {app.settings.offlineSchedule ? "준비" : "미사용"}</DataTag>
        <DataTag kind={app.settings.dataSaver ? "confirmed" : "live"}>{app.settings.dataSaver ? "데이터 절약" : "표준 갱신"}</DataTag>
      </div>
      {app.settings.delayAlerts && <div className="notice-box warning"><h3>지연 위험 감시 · DEMO</h3><p>환승 여유가 10분 아래로 줄면 A/B/C 복구안을 제시합니다.</p></div>}
      <div className="button-stack">
        <button className="secondary-button" type="button" disabled={app.remainingStops === 0} onClick={() => setApp((s) => ({ ...s, remainingStops: 0 }))}>{app.remainingStops === 0 ? "하차 안내 시점 재생 완료" : "하차 안내 시점으로 재생"}</button>
        <button className="danger-button" type="button" disabled={app.journeyLeg !== 2} onClick={() => setApp((s) => ({ ...s, screen: "delay", recovery: null }))}>{app.journeyLeg === 2 ? "대표 취약 구간 지연 시연" : "지연 시연은 대표 취약 구간에서 제공"}</button>
        <button className="primary-button" type="button" disabled={app.remainingStops > 0} onClick={() => setApp((s) => ({ ...s, screen: "alight" }))}>하차 확인</button>
      </div>
    </main>
  );
}

function DelayScreen({ app, setApp, continueRecovery, safeEnd }) {
  const details = ROUTE_DETAILS[app.selectedRoute] || ROUTE_DETAILS.B;
  return (
    <main className="screen">
      <ScreenTitle eyebrow="Delay recovery" title="환승 여유가 줄었습니다" lede="다음 환승 +6분 · 지연 위험 DEMO" onBack={() => setApp((s) => ({ ...s, screen: "ride" }))} />
      <div className="notice-box danger"><h3>무리한 환승을 권하지 않습니다</h3><p>안전 종료점은 {details.safeEnd}로 설정되어 있습니다. 실제 장소 정보가 아닙니다.</p></div>
      {[
        ["A", "원래 여정 유지", "다음 버스를 기다려 여유 +21분 · 도착 지연"],
        ["B", "대안 경로로 재배열", "다음 영천 55-1 DEMO로 전환 · 여유 +16분 · 예상 00:48"],
        ["C", "안전 종료점에서 마침", `${details.safeEnd} DEMO · 이후 공식 정보 재확인`],
      ].map(([key, title, detail]) => (
        <button className={`recovery-option ${app.recovery === key ? "selected" : ""}`} type="button" key={key} onClick={() => setApp((s) => ({ ...s, recovery: key }))}>
          <span className="recovery-letter">{key}</span><span className="recovery-copy"><strong>{title}</strong><span>{detail}</span></span>
        </button>
      ))}
      <div className="button-stack">
        <button className="primary-button" type="button" disabled={!app.recovery} onClick={() => app.recovery === "C" ? safeEnd() : continueRecovery()}>{app.recovery === "C" ? "안전 종료 선택" : "선택한 복구안 적용"}</button>
      </div>
    </main>
  );
}

function AlightScreen({ app, nextLeg, completeJourney }) {
  const details = ROUTE_DETAILS[app.selectedRoute] || ROUTE_DETAILS.B;
  const leg = details.legs[app.journeyLeg];
  const last = app.journeyLeg === details.legs.length - 1;
  return (
    <main className="screen">
      <div className="big-status">
        <div>
          <div className="status-mark">하차</div>
          <p className="eyebrow">Leg {app.journeyLeg + 1} complete</p>
          <h2>{leg.alight}<br />하차를 확인했습니다</h2>
          <p className="lede">현재 위치와 다음 승차 정류장을 직접 확인해 주세요.</p>
        </div>
      </div>
      {!last && <div className="notice-box"><h3>다음 구간 · {details.legs[app.journeyLeg + 1].no}</h3><p>{details.legs[app.journeyLeg + 1].board} 승차 · 환승 여유 {details.legs[app.journeyLeg + 1].buffer} DEMO</p></div>}
      <div className="button-stack"><button className="primary-button" type="button" onClick={last ? completeJourney : nextLeg}>{last ? "완주 기록 남기기" : "다음 구간 시작"}</button></div>
    </main>
  );
}

function CompleteScreen({ setApp }) {
  return (
    <main className="screen">
      <div className="big-status">
        <div>
          <div className="status-mark">완주</div>
          <p className="eyebrow">Demo journey complete</p>
          <h2>다섯 구간을<br />안전하게 이었습니다</h2>
          <p className="lede">이 완주 기록 역시 프로토타입용 DEMO입니다.</p>
        </div>
      </div>
      <div className="notice-box success"><h3>기록 저장 완료</h3><p>기록 탭에서 완료 시각과 선택한 안전 경로를 확인할 수 있습니다.</p></div>
      <div className="button-stack"><button className="primary-button" type="button" onClick={() => setApp((s) => ({ ...s, screen: "records", tab: "records" }))}>완주 기록 보기</button><button className="secondary-button" type="button" onClick={() => setApp((s) => ({ ...s, screen: "home", tab: "explore" }))}>새 여정 탐색</button></div>
    </main>
  );
}

function SafeEndScreen({ app, setApp }) {
  const details = ROUTE_DETAILS[app.selectedRoute] || ROUTE_DETAILS.B;
  return (
    <main className="screen">
      <div className="big-status">
        <div><div className="status-mark">안전</div><p className="eyebrow">Safe stop recorded</p><h2>안전 종료점에서<br />여행을 마쳤습니다</h2><p className="lede">{details.safeEnd} · DEMO 종료점</p></div>
      </div>
      <div className="notice-box warning"><h3>다음 행동</h3><p>공식 운행 정보를 다시 확인하고, 무리한 야간 이동 대신 숙박·귀가 수단을 선택합니다.</p></div>
      <div className="button-stack"><button className="primary-button" type="button" onClick={() => setApp((s) => ({ ...s, screen: "records", tab: "records" }))}>안전 종료 기록 보기</button></div>
    </main>
  );
}

function NoPathScreen({ setApp }) {
  return (
    <main className="screen">
      <ScreenTitle eyebrow="NO_PATH" title="검증 범위에서 경로 없음" lede="검증된 데이터 범위에서 조건 충족 경로가 없습니다." onBack={() => setApp((s) => ({ ...s, screen: "home" }))} />
      <div className="big-status"><div><div className="status-mark">0</div><h3>기준일 2026.08.27 스냅샷에서 현재 날짜·도보·여정 조건을 함께 만족하지 못했습니다.</h3></div></div>
      <div className="notice-box"><h3>가능한 행동</h3><p>날짜·출발 시각 또는 도보 한도를 조정합니다. 필수 원천이 누락된 DATA_GAP과는 다릅니다.</p></div>
      <div className="button-stack"><button className="primary-button" type="button" onClick={() => setApp((s) => ({ ...s, screen: "home", form: { ...s.form, walkingLimit: 1000 }, demoState: "normal" }))}>도보 1,000m로 조정</button></div>
    </main>
  );
}

function DataGapScreen({ app, setApp }) {
  return (
    <main className="screen">
      <ScreenTitle eyebrow="DATA_GAP" title="공식 데이터가 부족합니다" lede="길이 없다고 단정할 수 없는 상태입니다." onBack={() => setApp((s) => ({ ...s, screen: "home" }))} />
      <div className="notice-box danger"><h3>여행 시작 보류</h3><p>영향 구간: 김천 경계 연결 · 누락 원천: 회차별 stop_times · 기준일 2026.08.27.</p></div>
      <button className="secondary-button" type="button" onClick={() => setApp((s) => ({ ...s, gapExpanded: !s.gapExpanded }))}>{app.gapExpanded ? "공식 확인 항목 접기" : "공식 확인 항목 펼치기"}</button>
      {app.gapExpanded && <section className="paper-section"><h3>확인해야 할 항목</h3><ul className="plain-list"><li>김천 경계 연결의 회차별 출발·도착 stop_times</li><li>운송기관의 첫차·막차·임시 우회 공지</li><li>2026.08.27 이후 변경된 공식 시간표</li></ul></section>}
      <div className="button-stack"><button className="primary-button" type="button" onClick={() => setApp((s) => ({ ...s, screen: "home", demoState: "normal" }))}>조건을 바꿔 재검색</button></div>
    </main>
  );
}

function JourneyHubScreen({ app, setApp }) {
  if (!app.journeyStarted) {
    return <main className="screen"><ScreenTitle eyebrow="Active journey" title="진행 중인 여행" lede="시작한 DEMO 여정이 없습니다." /><div className="empty-state"><div><div className="empty-lines" /><h3>상세 검증 후 여행을 시작하세요</h3><div className="button-stack"><button className="primary-button" type="button" onClick={() => setApp((s) => ({ ...s, screen: "home", tab: "explore" }))}>여정 탐색</button></div></div></div></main>;
  }
  const route = ROUTES.find((item) => item.id === app.selectedRoute) || ROUTES[0];
  const details = ROUTE_DETAILS[route.id];
  const leg = details.legs[app.journeyLeg];
  const next = details.legs[Math.min(app.journeyLeg + 1, details.legs.length - 1)];
  return <main className="screen"><ScreenTitle eyebrow="Active journey" title={`${route.id} 여정 계속하기`} lede={`${leg.no} · ${app.remainingStops}개 정류장 남음`} /><div className="ride-board"><p className="eyebrow">저장된 현재 탑승 · DEMO</p><div className="ride-route-no">{leg.no}</div><p>{leg.board} → {leg.alight}</p></div><div className="notice-box success"><h3>다음 {app.activeRouteNo || next.no}</h3><p>환승 여유 +{app.activeBuffer || next.buffer} · 예상 도착 {app.expectedArrival || details.range.split("→").pop().trim()}</p></div><div className="button-stack"><button className="primary-button" type="button" onClick={() => setApp((s) => ({ ...s, screen: "ride", tab: "journey" }))}>현재 여행 계속</button></div></main>;
}

function SavedScreen({ app, setApp, selectRoute, toggleSave }) {
  const saved = ROUTES.filter((route) => app.savedRoutes.includes(route.id));
  return (
    <main className="screen">
      <ScreenTitle eyebrow="Saved tickets" title="저장한 여정" lede="기기에만 남는 프로토타입 저장 목록입니다." />
      {saved.length === 0 ? <div className="empty-state"><div><div className="empty-lines" /><h3>아직 저장한 여정이 없습니다</h3><p className="lede">탐색 결과에서 여정을 저장해 비교해 보세요.</p><div className="button-stack"><button className="primary-button" type="button" onClick={() => setApp((s) => ({ ...s, screen: "home", tab: "explore" }))}>여정 탐색하기</button></div></div></div> : saved.map((route) => (
        <article className="saved-item" key={route.id}><div className="item-head"><div><h3>{route.id} · {route.name}</h3><p>{route.duration} · {route.rides} · 도보 {route.walk}</p><p>기준 2026.08.27 · 신뢰 {route.trust} · {route.precision} · bus-demo-20260827-{route.id}</p></div><span className="demo-tiny">SAVED</span></div><div className="inline-buttons"><button className="danger-button" type="button" onClick={() => toggleSave(route.id)}>삭제</button><button className="primary-button" type="button" onClick={() => selectRoute(route.id)}>상세 보기</button></div></article>
      ))}
    </main>
  );
}

function RecordsScreen({ app, setApp }) {
  return (
    <main className="screen">
      <ScreenTitle eyebrow="Journey log" title="여행 기록" lede="완주와 안전 종료를 같은 가치로 기록합니다." />
      {app.records.length === 0 ? <div className="empty-state"><div><div className="empty-lines" /><h3>아직 기록이 없습니다</h3><p className="lede">DEMO 여행을 시작해 첫 기록을 남겨 보세요.</p><div className="button-stack"><button className="primary-button" type="button" onClick={() => setApp((s) => ({ ...s, screen: "home", tab: "explore" }))}>첫 여정 찾기</button></div></div></div> : app.records.map((record) => (
        <article className="record-item" key={record.id}><div className="item-head"><div><h3>P2 개념 · {record.outcome}</h3><p>{record.route} · {record.when}</p><p>실제시각 {record.actual} · 지연 {record.delay} · 상태 {record.status} (시연값)</p></div><DataTag kind={record.outcome === "안전 종료" ? "estimated" : "confirmed"}>DEMO 기록</DataTag></div></article>
      ))}
    </main>
  );
}

function SettingsScreen({ app, setApp, resetDemo }) {
  const setSetting = (key, value) => setApp((s) => ({ ...s, settings: { ...s.settings, [key]: value } }));
  return (
    <main className="screen">
      <ScreenTitle eyebrow="Preferences" title="설정" lede="변경 사항은 이 브라우저에 저장됩니다." />
      <section className="paper-section">
        <label className="field"><span>도보 기본값 · {app.settings.walkingDefault}m</span><input type="range" min="200" max="3000" step="100" value={app.settings.walkingDefault} onChange={(event) => setSetting("walkingDefault", Number(event.target.value))} /></label>
        <SwitchRow title="지연 위험 알림" detail="환승 여유 10분 미만 DEMO 경고" checked={app.settings.delayAlerts} onChange={(v) => setSetting("delayAlerts", v)} />
        <SwitchRow title="오프라인 일정" detail="여행 탭에 저장된 일정 상태 표시" checked={app.settings.offlineSchedule} onChange={(v) => setSetting("offlineSchedule", v)} />
        <SwitchRow title="데이터 절약" detail="여행 화면에서 표준 갱신 대신 절약 상태 표시" checked={app.settings.dataSaver} onChange={(v) => setSetting("dataSaver", v)} />
      </section>
      <div className="notice-box"><h3>저장 위치</h3><p>localStorage 키: busro-itda-demo-v1 · 실제 계정이나 서버에는 전송되지 않습니다.</p></div>
      {!app.resetReady ? <button className="danger-button" type="button" onClick={() => setApp((s) => ({ ...s, resetReady: true }))}>모든 DEMO 상태 초기화</button> : <div className="notice-box danger"><h3>초기화할까요?</h3><p>저장 여정·기록·설정·현재 진행 위치를 삭제합니다.</p><div className="inline-buttons"><button className="secondary-button" type="button" onClick={() => setApp((s) => ({ ...s, resetReady: false }))}>취소</button><button className="danger-button" type="button" onClick={resetDemo}>초기화 확정</button></div></div>}
    </main>
  );
}

function BottomNav({ app, setApp }) {
  const items = [["explore", "탐색"], ["journey", "여행"], ["saved", "저장"], ["records", "기록"], ["settings", "설정"]];
  const screens = { explore: "home", journey: "journey", saved: "saved", records: "records", settings: "settings" };
  return (
    <nav className="bottom-nav" aria-label="주요 메뉴">
      {items.map(([key, label]) => <button className={`nav-button ${app.tab === key ? "active" : ""}`} type="button" key={key} onClick={() => setApp((s) => ({ ...s, tab: key, screen: screens[key] }))}><span className={`nav-glyph ${key}`} /><span>{label}</span></button>)}
    </nav>
  );
}

function App() {
  const [app, setApp] = useState(loadInitialState);

  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(app)); } catch { /* prototype remains usable without persistence */ }
  }, [app]);

  useEffect(() => {
    if (app.screen !== "loading") return undefined;
    const timer = window.setTimeout(() => {
      setApp((state) => ({ ...state, screen: state.demoState === "noPath" ? "noPath" : state.demoState === "dataGap" ? "dataGap" : "results" }));
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [app.screen, app.demoState]);

  const route = useMemo(() => ROUTES.find((item) => item.id === app.selectedRoute) || ROUTES[0], [app.selectedRoute]);
  const toggleSave = (id) => setApp((state) => {
    if (state.savedRoutes.includes(id)) return { ...state, guardMessage: "", savedRoutes: state.savedRoutes.filter((item) => item !== id) };
    const target = ROUTES.find((item) => item.id === id);
    const availability = target ? routeAvailability(target, state.form) : { allowed: false, reasons: ["알 수 없는 경로"] };
    if (!availability.allowed) return { ...state, screen: "results", tab: "explore", guardMessage: `${id} 경로: ${availability.reasons.join(" · ")}` };
    return { ...state, guardMessage: "", savedRoutes: [...state.savedRoutes, id] };
  });
  const selectRoute = (id) => setApp((state) => {
    const target = ROUTES.find((item) => item.id === id);
    const availability = target ? routeAvailability(target, state.form) : { allowed: false, reasons: ["알 수 없는 경로"] };
    if (!availability.allowed) return { ...state, screen: "results", tab: "explore", guardMessage: `${id} 경로: ${availability.reasons.join(" · ")}` };
    return { ...state, selectedRoute: id, screen: "detail", tab: "explore", guardMessage: "", ackOfficial: false, ackRisk: false, ackChallenge: false, prep: { ...DEFAULT_APP.prep }, recovery: null, journeyStarted: false, journeyLeg: 2, remainingStops: ROUTE_DETAILS[id].legs[2].remaining || 2, activeRouteNo: null, activeBuffer: null, expectedArrival: null, shareStatus: "" };
  });
  const addRecord = (outcome) => setApp((state) => ({ ...state, records: [{ id: Date.now(), outcome, route: `${route.id} · ${route.name}`, when: "2026-09-01 · DEMO", actual: outcome === "안전 종료" ? "19:42" : "00:51", delay: outcome === "안전 종료" ? "+12분" : "+16분", status: outcome }, ...state.records].slice(0, 20), journeyStarted: false, tab: "journey", screen: outcome === "안전 종료" ? "safeEnd" : "complete" }));
  const startJourney = () => { const legs = ROUTE_DETAILS[app.selectedRoute].legs; setApp((state) => ({ ...state, screen: "ride", tab: "journey", journeyStarted: true, journeyLeg: 2, remainingStops: legs[2].remaining, recovery: null, activeRouteNo: null, activeBuffer: null, expectedArrival: null })); };
  const nextLeg = () => setApp((state) => { const legs = ROUTE_DETAILS[state.selectedRoute].legs; const next = Math.min(state.journeyLeg + 1, legs.length - 1); return { ...state, journeyLeg: next, remainingStops: legs[next].remaining, screen: "ride", tab: "journey", recovery: null, activeRouteNo: null, activeBuffer: null }; });
  const applyRecovery = () => setApp((state) => state.recovery === "B" ? { ...state, screen: "ride", tab: "journey", activeRouteNo: "영천 55-1", activeBuffer: "16분", expectedArrival: "00:48", remainingStops: Math.max(1, state.remainingStops) } : { ...state, screen: "ride", tab: "journey", activeRouteNo: null, activeBuffer: "21분", remainingStops: Math.max(1, state.remainingStops) });
  const resetDemo = () => { try { localStorage.removeItem(STORAGE_KEY); } catch { /* no-op */ } setApp({ ...DEFAULT_APP, form: { ...DEFAULT_APP.form }, prep: { ...DEFAULT_APP.prep }, settings: { ...DEFAULT_APP.settings } }); };

  const props = { app, setApp };
  let content;
  switch (app.screen) {
    case "loading": content = <LoadingScreen {...props} />; break;
    case "results": content = <ResultsScreen {...props} selectRoute={selectRoute} toggleSave={toggleSave} />; break;
    case "detail": content = <DetailScreen {...props} toggleSave={toggleSave} />; break;
    case "risk": content = <RiskScreen {...props} />; break;
    case "prepare": content = <PrepareScreen {...props} startJourney={startJourney} />; break;
    case "ride": content = <RideScreen {...props} />; break;
    case "delay": content = <DelayScreen {...props} continueRecovery={applyRecovery} safeEnd={() => addRecord("안전 종료")} />; break;
    case "alight": content = <AlightScreen {...props} nextLeg={nextLeg} completeJourney={() => addRecord("완주")} />; break;
    case "complete": content = <CompleteScreen setApp={setApp} />; break;
    case "safeEnd": content = <SafeEndScreen {...props} />; break;
    case "noPath": content = <NoPathScreen setApp={setApp} />; break;
    case "dataGap": content = <DataGapScreen {...props} />; break;
    case "journey": content = <JourneyHubScreen {...props} />; break;
    case "saved": content = <SavedScreen {...props} selectRoute={selectRoute} toggleSave={toggleSave} />; break;
    case "records": content = <RecordsScreen {...props} />; break;
    case "settings": content = <SettingsScreen {...props} resetDemo={resetDemo} />; break;
    default: content = <HomeScreen {...props} search={() => setApp((s) => ({ ...s, screen: "loading", tab: "explore" }))} />;
  }

  return (
    <div className="stage">
      <div className="phone-frame">
        <div className="app-shell">
          <div className="demo-banner">모든 노선·시각은 프로토타입용 시연 데이터이며 실제 운행 정보가 아닙니다</div>
          <header className="app-header"><div><p className="header-kicker">Local bus travel journal</p><p className="wordmark">버스로 잇다</p></div><span className="demo-seal">PROTOTYPE</span></header>
          <div className="screen-scroll">{content}</div>
          <BottomNav app={app} setApp={setApp} />
        </div>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
