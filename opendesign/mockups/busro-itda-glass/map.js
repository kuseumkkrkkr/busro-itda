(function initBusroMap(global) {
  "use strict";

  function validCoordinate(value) {
    return Array.isArray(value) && value.length >= 2 && Number.isFinite(Number(value[0])) && Number.isFinite(Number(value[1]));
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
  }

  function create(element) {
    if (!global.L) throw new Error("OSM 지도 라이브러리를 불러오지 못했습니다.");
    const map = global.L.map(element, { zoomControl: false, attributionControl: true, preferCanvas: true }).setView([36.35, 127.8], 7);
    global.L.control.zoom({ position: "topright" }).addTo(map);
    global.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors',
    }).addTo(map);
    const layers = global.L.layerGroup().addTo(map);

    function render({ geometry, stops = [], positions = [] } = {}) {
      layers.clearLayers();
      const bounds = [];
      if (geometry?.type && geometry?.coordinates) {
        const route = global.L.geoJSON({ type: "Feature", properties: {}, geometry }, {
          style: { color: "#74f5c7", weight: 6, opacity: 0.88, lineCap: "round", lineJoin: "round" },
        }).addTo(layers);
        const routeBounds = route.getBounds();
        if (routeBounds.isValid()) bounds.push(routeBounds.getSouthWest(), routeBounds.getNorthEast());
      }
      stops.slice(0, 240).forEach((stop, index) => {
        const lat = Number(stop.latitude ?? stop.gpslati);
        const lon = Number(stop.longitude ?? stop.gpslong);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
        bounds.push([lat, lon]);
        const endpoint = index === 0 || index === stops.length - 1;
        global.L.circleMarker([lat, lon], {
          radius: endpoint ? 6 : 3,
          color: endpoint ? "#ffffff" : "#b8d8d0",
          weight: endpoint ? 2 : 1,
          fillColor: endpoint ? "#ff9f7d" : "#072a35",
          fillOpacity: 1,
        }).bindPopup(`<strong>${escapeHtml(stop.node_name || stop.nodenm || "정류장")}</strong><br>${escapeHtml(stop.node_order ?? stop.nodeord ?? index + 1)}번째`).addTo(layers);
      });
      positions.slice(0, 80).forEach((position) => {
        const lat = Number(position.latitude ?? position.gpslati);
        const lon = Number(position.longitude ?? position.gpslong);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
        bounds.push([lat, lon]);
        const icon = global.L.divIcon({ className: "live-bus-map-icon", html: '<i class="ph ph-bus"></i>', iconSize: [34, 34], iconAnchor: [17, 17] });
        global.L.marker([lat, lon], { icon }).bindPopup(`<strong>${escapeHtml(position.vehicle_no || position.vehicleno || "운행 차량")}</strong><br>${escapeHtml(position.node_name || position.nodenm || "위치 관측")}`).addTo(layers);
      });
      if (bounds.length) map.fitBounds(bounds, { padding: [28, 28], maxZoom: 15, animate: false });
      else map.setView([36.35, 127.8], 7, { animate: false });
      global.setTimeout(() => map.invalidateSize(), 0);
    }

    return { map, render, destroy: () => map.remove(), invalidate: () => map.invalidateSize() };
  }

  global.BusroMap = { create };
})(window);
