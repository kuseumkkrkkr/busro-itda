(function initBusroApi(global) {
  "use strict";

  const DEFAULT_BASE = global.location && ["http:", "https:"].includes(global.location.protocol)
    ? `${global.location.origin}/api`
    : "http://127.0.0.1:8791/api";

  function getBase() {
    const configured = global.BUSRO_API_BASE || global.localStorage.getItem("busro-api-base");
    return String(configured || DEFAULT_BASE).replace(/\/$/, "");
  }

  function cleanBase(value) {
    const clean = String(value || "").trim().replace(/\/$/, "");
    if (!clean) return "";
    let parsed;
    try { parsed = new URL(clean); } catch { throw new Error("데이터 서비스 주소는 http(s) URL이어야 합니다."); }
    if (!["http:", "https:"].includes(parsed.protocol)) throw new Error("데이터 서비스 주소는 http(s)만 허용합니다.");
    if (parsed.username || parsed.password || parsed.search || parsed.hash) throw new Error("서비스 키·인증값은 주소나 브라우저 저장소에 넣을 수 없습니다.");
    return clean;
  }

  function safeCapabilityEndpoint(value, fallback) {
    const endpoint = typeof value === "string" ? value : value?.endpoint;
    const candidate = endpoint || (value === true ? fallback : "");
    if (!candidate || !String(candidate).startsWith("/") || String(candidate).startsWith("//")) return "";
    return String(candidate).startsWith("/api/") ? String(candidate).slice(4) : String(candidate);
  }

  function mappingRows(payload) {
    const rows = payload?.mappings || payload?.mapping?.legs || payload?.mapping_status?.legs || payload?.legs;
    if (Array.isArray(rows)) return rows;
    if (rows && typeof rows === "object") return Object.entries(rows).map(([id, value]) => ({ id, ...(value || {}) }));
    return [];
  }

  function normalizeMappingPayload(payload, requested) {
    const rows = mappingRows(payload);
    const entries = requested.map((mapping) => {
      const row = rows.find((item) => String(item.id || item.leg_id || "") === mapping.id);
      const state = String(row?.state || row?.status || "").toLowerCase();
      const verified = row?.verified === true || row?.api_mapped === true || ["verified", "valid", "mapped"].includes(state);
      return {
        id: mapping.id,
        verified,
        code: row?.code || (verified ? "VERIFIED" : "MAPPING_UNVERIFIED"),
        message: row?.message || (verified ? "공식 식별자 검증됨" : "서버가 이 식별자를 검증하지 않았습니다."),
      };
    });
    return { supported: true, entries, raw: payload };
  }

  function pastDates(days) {
    const end = new Date();
    return Array.from({ length: days }, (_, index) => {
      const value = new Date(end);
      value.setDate(end.getDate() - (days - index - 1));
      return value.toISOString().slice(0, 10);
    });
  }

  function backendIdentifier(value, fallback = "journey") {
    const clean = String(value || fallback).replace(/[^A-Za-z0-9_-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "").slice(0, 64);
    return clean || fallback;
  }

  function clockTime(value) {
    const clean = String(value || "");
    if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(clean)) throw new Error("검증된 실제 시간표 시각이 없는 구간은 재생할 수 없습니다.");
    return clean;
  }

  function journeyServiceDate(value) {
    const clean = String(value || "");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(clean)) throw new Error("여행 날짜를 YYYY-MM-DD 형식으로 선택해 주세요.");
    const [year, month, day] = clean.split("-").map(Number);
    const parsed = new Date(Date.UTC(year, month - 1, day));
    if (parsed.getUTCFullYear() !== year || parsed.getUTCMonth() !== month - 1 || parsed.getUTCDate() !== day) {
      throw new Error("유효한 여행 날짜를 선택해 주세요.");
    }
    return clean;
  }

  function journeyDepartureTime(value) {
    const clean = String(value || "");
    if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(clean)) throw new Error("출발 시각을 HH:MM 형식으로 선택해 주세요.");
    return clean;
  }

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timeout = global.setTimeout(() => controller.abort(), options.timeout || 6500);
    try {
      const response = await fetch(`${getBase()}${path}`, {
        method: options.method || "GET",
        headers: { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}) },
        body: options.body ? JSON.stringify(options.body) : undefined,
        signal: controller.signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const apiError = payload && typeof payload.error === "object" ? payload.error : null;
        const message = payload.message || apiError?.message || apiError?.code ||
          (typeof payload.error === "string" ? payload.error : "") || `API ${response.status}`;
        const error = new Error(message);
        error.status = response.status;
        error.payload = payload;
        throw error;
      }
      return payload;
    } finally {
      global.clearTimeout(timeout);
    }
  }

  global.BusroApi = {
    getBase,
    setBase(value) {
      const clean = cleanBase(value);
      if (clean) global.localStorage.setItem("busro-api-base", clean);
      else global.localStorage.removeItem("busro-api-base");
      return clean || DEFAULT_BASE;
    },
    status: () => request("/status"),
    async mappingStatus(statusPayload, mappings, options = {}) {
      const requested = (mappings || []).slice(0, 12).map((item) => ({
        id: String(item.id || ""),
        city_code: String(item.cityCode || item.city_code || ""),
        node_id: String(item.nodeId || item.node_id || ""),
        route_id: String(item.routeId || item.route_id || ""),
      }));
      const embeddedRows = mappingRows(statusPayload);
      if (embeddedRows.length) return normalizeMappingPayload(statusPayload, requested);

      const capabilities = statusPayload?.capabilities || {};
      const advertised = capabilities.mapping_validation || capabilities.mapping_status || capabilities.route_mapping_validation || capabilities.route_stop_mapping_validation;
      const endpoint = safeCapabilityEndpoint(advertised, "/mappings/validate");
      if (!endpoint) return { supported: false, code: "MAPPING_VALIDATION_UNAVAILABLE", entries: [], message: "DATA_GAP · 서버가 공식 식별자 검증 기능을 제공하지 않습니다." };

      try {
        const entries = await Promise.all(requested.map(async (mapping) => {
          if (!mapping.city_code || !mapping.node_id || !mapping.route_id) {
            return { id: mapping.id, verified: false, code: "MAPPING_INCOMPLETE", message: "cityCode·nodeId·routeId가 모두 필요합니다." };
          }
          const payload = await request(endpoint, {
            method: options.method || "POST",
            body: { city_code: mapping.city_code, node_id: mapping.node_id, route_id: mapping.route_id },
          });
          const verified = payload.valid === true;
          return {
            id: mapping.id,
            verified,
            code: payload.reason || (verified ? "ROUTE_CONTAINS_NODE" : "NODE_NOT_ON_ROUTE"),
            message: verified ? "공식 노선 경유 정류장과 일치합니다." : "공식 노선에서 이 정류장을 확인하지 못했습니다.",
          };
        }));
        return { supported: true, entries };
      } catch (error) {
        if ([404, 405, 501].includes(error.status)) return { supported: false, code: "MAPPING_VALIDATION_UNAVAILABLE", entries: [], message: "DATA_GAP · 서버가 공식 식별자 검증 기능을 제공하지 않습니다." };
        throw error;
      }
    },
    async validateMapping(statusPayload, mapping) {
      return this.mappingStatus(statusPayload, [mapping], { method: "POST" });
    },
    async passageCoverage(statusPayload, legs, days = 14) {
      const capabilities = statusPayload?.capabilities || {};
      const storageAdvertised = Object.prototype.hasOwnProperty.call(statusPayload?.storage || {}, "passages");
      const passageCapability = capabilities.passage_history || capabilities.passages || capabilities.passage_reconstruction || capabilities.vehicle_passages;
      if (!storageAdvertised && !passageCapability) return { supported: false, count: 0, eligibleDays: 0, gapCount: 0, dataGap: true, code: "PASSAGE_HISTORY_UNAVAILABLE" };

      const mapped = (Array.isArray(legs) ? legs : [legs]).filter((leg) => leg?.apiMapped && leg?.routeId);
      if (!mapped.length) return { supported: true, count: 0, eligibleDays: 0, gapCount: 0, dataGap: true, code: "MAPPING_REQUIRED" };
      const end = new Date();
      const start = new Date(end);
      start.setDate(end.getDate() - Math.max(1, days - 1));
      const params = new URLSearchParams({ from: start.toISOString(), to: end.toISOString(), limit: "500" });
      if (mapped.length === 1) params.set("route_id", mapped[0].routeId);
      const payload = await request(`/passages?${params.toString()}`);
      const routeIds = new Set(mapped.map((leg) => String(leg.routeId)));
      const rows = (payload.passages || []).filter((item) => mapped.length === 1 || routeIds.has(String(item.route_id || "")));
      const passageRows = rows.filter((item) => String(item.status || item.event_type || "").toUpperCase() === "PASSAGE");
      const gapRows = rows.filter((item) => String(item.status || item.event_type || "").toUpperCase() === "DATA_GAP");
      const dates = new Set(passageRows.map((item) => item.service_date || item.service_date_kst || String(item.observed_to || item.observed_at || "").slice(0, 10)).filter(Boolean));
      return { supported: true, count: passageRows.length, eligibleDays: dates.size, gapCount: gapRows.length, dataGap: passageRows.length === 0, code: passageRows.length ? "PASSAGE_HISTORY_AVAILABLE" : "INSUFFICIENT_PASSAGE_HISTORY" };
    },
    arrivals: (leg) => request(`/arrivals?city_code=${encodeURIComponent(leg.cityCode)}&node_id=${encodeURIComponent(leg.nodeId)}&leg_id=${encodeURIComponent(leg.id)}`),
    positions: (route) => request(`/positions?city_code=${encodeURIComponent(route.cityCode || route.city_code)}&route_id=${encodeURIComponent(route.routeId || route.route_id)}`),
    networkStatus: () => request("/network/status"),
    cities: () => request("/cities"),
    routes(cityCode, routeNo = "") {
      const params = new URLSearchParams({ city_code: String(cityCode), page: "1", limit: "100" });
      if (String(routeNo).trim()) params.set("route_no", String(routeNo).trim());
      return request(`/routes?${params.toString()}`);
    },
    routeInfo(cityCode, routeId) {
      const params = new URLSearchParams({ city_code: String(cityCode), route_id: String(routeId) });
      return request(`/routes/info?${params.toString()}`);
    },
    async routeStops(cityCode, routeId) {
      const stops = [];
      let page = 1;
      let total = 1;
      let last = {};
      while ((page - 1) * 100 < total && page <= 10) {
        const params = new URLSearchParams({ city_code: String(cityCode), route_id: String(routeId), page: String(page), limit: "100" });
        last = await request(`/routes/stops?${params.toString()}`);
        stops.push(...(last.stops || last.items || []));
        total = Math.max(0, Number(last.upstream?.total_count ?? last.total_count ?? stops.length));
        page += 1;
      }
      return { ...last, stops, count: stops.length, truncated: total > stops.length };
    },
    routeGeometry(routeRef, stops) {
      return request("/osm/geometry", { method: "POST", timeout: 24000, body: {
        route_ref: String(routeRef || ""),
        stops: (stops || []).slice(0, 160).map((stop) => ({
          node_id: String(stop.node_id || ""),
          node_name: String(stop.node_name || ""),
          node_order: Number(stop.node_order || 0),
          latitude: Number(stop.latitude),
          longitude: Number(stop.longitude),
        })),
      } });
    },
    async searchStops(query, cityCode = "") {
      const params = new URLSearchParams({ q: String(query || "").trim(), limit: "8" });
      if (cityCode) params.set("city_code", String(cityCode));
      try { return await request(`/network/stops?${params.toString()}`); }
      catch (error) {
        if (![404, 501, 503].includes(error.status) || !cityCode) throw error;
        const official = new URLSearchParams({ city_code: String(cityCode), node_name: String(query || "").trim(), page: "1", limit: "8" });
        return request(`/stops?${official.toString()}`);
      }
    },
    generateJourneys(payload = {}) {
      const serviceDate = journeyServiceDate(payload.service_date);
      const departureTime = journeyDepartureTime(payload.departure_time);
      return request("/journeys/generate", {
        method: "POST",
        timeout: 20000,
        body: { ...payload, service_date: serviceDate, departure_time: departureTime },
      });
    },
    hydrateRoute(cityCode, routeId) {
      return request("/network/hydrate", { method: "POST", timeout: 12000, body: {
        city_code: String(cityCode || ""),
        route_id: String(routeId || ""),
      } });
    },
    history(leg, days = 14) {
      const params = new URLSearchParams({
        city_code: String(leg.cityCode || ""),
        node_id: String(leg.nodeId || ""),
        route_id: String(leg.routeId || ""),
        limit: String(Math.min(500, Math.max(20, days * 20))),
      });
      return request(`/history?${params.toString()}`);
    },
    collect: (leg) => request("/collect", { method: "POST", body: { city_code: leg.cityCode, node_id: leg.nodeId, leg_id: leg.id } }),
    collectPositions: (leg) => request("/positions/collect", { method: "POST", body: { city_code: leg.cityCode, route_id: leg.routeId } }),
    replay(days, legs, journey) {
      const from = journey?.from_stop || journey?.from || {};
      const to = journey?.to_stop || journey?.to || {};
      const fromName = from.node_name || from.node_id || "출발";
      const toName = to.node_name || to.node_id || "도착";
      return request("/replay", { method: "POST", timeout: 12000, body: {
        route: {
          id: backendIdentifier(journey?.id || `${from.node_id || "from"}-${to.node_id || "to"}`),
          name: `${fromName} → ${toName}`.slice(0, 100),
        },
        dates: pastDates(days),
        legs: (legs || []).slice(0, 12).map((leg) => {
          if (!leg.timeEvidenceVerified || !leg.timeEvidenceSource) throw new Error("검증된 실제 시간표 출처가 없는 구간은 재생할 수 없습니다.");
          if (!leg.timeEvidenceFeedId || !leg.nextTimeEvidenceFeedId) throw new Error("시간표 feed 버전 근거가 없는 구간은 재생할 수 없습니다.");
          if (!leg.alightNodeId || !Number.isInteger(Number(leg.alightNodeOrder))) throw new Error("도착 정류장 ID와 순번이 없는 구간은 재생할 수 없습니다.");
          if (!Number.isInteger(Number(leg.minimumTransfer))) throw new Error("실제 최소 환승시간이 없는 구간은 재생할 수 없습니다.");
          return {
            id: backendIdentifier(leg.id, "leg"),
            route_id: String(leg.routeId || ""),
            node_id: String(leg.alightNodeId),
            node_order: Number(leg.alightNodeOrder),
            scheduled_arrival: clockTime(leg.scheduledArrival),
            next_departure: clockTime(leg.nextDeparture),
            minimum_transfer_minutes: Number(leg.minimumTransfer),
            time_evidence_source: String(leg.timeEvidenceSource),
            time_evidence_trip_id: String(leg.timeEvidenceTripId),
            time_evidence_feed_id: String(leg.timeEvidenceFeedId),
            next_route_id: String(leg.nextRouteId),
            next_node_id: String(leg.nextNodeId),
            next_node_order: Number(leg.nextNodeOrder),
            next_time_evidence_trip_id: String(leg.nextTimeEvidenceTripId),
            next_time_evidence_feed_id: String(leg.nextTimeEvidenceFeedId),
          };
        }),
      } });
    },
  };
})(window);
