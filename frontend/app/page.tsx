"use client";

import { useEffect, useRef, useState } from "react";
import * as maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

if (typeof window !== "undefined") {
  maplibregl.setWorkerUrl("/maplibre-gl-worker.mjs");
}
import {
  Bus,
  Footprints,
  Clock,
  ArrowRight,
  Navigation,
  MapPin,
  Layers,
  Sparkles,
  Search,
} from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const BASEMAPS: Record<
  string,
  { label: string; style: string | maplibregl.StyleSpecification }
> = {
  vectorDark: {
    label: "Dark Vector",
    style: "https://tiles.openfreemap.org/styles/dark",
  },
  esriDark: {
    label: "Dark Canvas",
    style: {
      version: 8,
      sources: {
        "esri-dark": {
          type: "raster",
          tiles: [
            "https://services.arcgisonline.com/arcgis/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
          ],
          tileSize: 256,
          attribution: "Esri, HERE, Garmin, © OpenStreetMap contributors",
        },
      },
      layers: [
        {
          id: "esri-dark-tiles",
          type: "raster",
          source: "esri-dark",
          minzoom: 0,
          maxzoom: 16,
        },
      ],
    },
  },
  osm: {
    label: "OpenStreetMap",
    style: {
      version: 8,
      sources: {
        "osm-tiles": {
          type: "raster",
          tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
          tileSize: 256,
          attribution: "© OpenStreetMap contributors",
        },
      },
      layers: [
        {
          id: "osm-tiles-layer",
          type: "raster",
          source: "osm-tiles",
          minzoom: 0,
          maxzoom: 19,
        },
      ],
    },
  },
};

const PRESETS = [
  {
    label: "Kashmere Gate → Nehru Place (8:30 AM Tue)",
    from: "Kashmere Gate",
    to: "Nehru Place",
    depart: "2026-11-17T08:30",
  },
  {
    label: "Kashmere Gate → Nehru Place (11:00 PM Tue)",
    from: "Kashmere Gate",
    to: "Nehru Place",
    depart: "2026-11-17T23:00",
  },
  {
    label: "Anand Vihar → Connaught Place (9:00 AM)",
    from: "Anand Vihar",
    to: "Shivaji Stadium",
    depart: "2026-11-17T09:00",
  },
];

interface JourneyBreakdown {
  accessWalkMin: number;
  expectedWaitMin: number;
  busTravelMin: number;
  transferWalkMin: number;
  egressWalkMin: number;
  componentSum: number;
  totalDurationMin: number;
}

function computeJourneyBreakdown(journey: any): JourneyBreakdown {
  let accessWalkMin = 0;
  let egressWalkMin = 0;
  let transferWalkMin = 0;
  let busTravelMin = 0;
  let expectedWaitMin = 0;

  const legs = journey.legs || [];
  legs.forEach((leg: any, idx: number) => {
    if (leg.mode === "WALK") {
      const dur = Number(leg.duration_min) || 0;
      if (idx === 0) {
        accessWalkMin += dur;
      } else if (idx === legs.length - 1) {
        egressWalkMin += dur;
      } else {
        transferWalkMin += dur;
      }
    } else if (leg.mode === "BUS") {
      busTravelMin += Number(leg.ride_min) || 0;
      expectedWaitMin += Number(leg.wait_min) || 0;
    }
  });

  accessWalkMin = Math.round(accessWalkMin * 10) / 10;
  expectedWaitMin = Math.round(expectedWaitMin * 10) / 10;
  busTravelMin = Math.round(busTravelMin * 10) / 10;
  transferWalkMin = Math.round(transferWalkMin * 10) / 10;
  egressWalkMin = Math.round(egressWalkMin * 10) / 10;

  const componentSum =
    Math.round(
      (accessWalkMin + expectedWaitMin + busTravelMin + transferWalkMin + egressWalkMin) * 10
    ) / 10;

  return {
    accessWalkMin,
    expectedWaitMin,
    busTravelMin,
    transferWalkMin,
    egressWalkMin,
    componentSum,
    totalDurationMin: Number(journey.total_duration_min) || componentSum,
  };
}

export default function DelhiTransitInspector() {
  const mapContainer = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const markersRef = useRef<maplibregl.Marker[]>([]);

  const [origin, setOrigin] = useState("Kashmere Gate");
  const [destination, setDestination] = useState("Nehru Place");
  const [depart, setDepart] = useState("2026-11-17T08:30");
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<any>(null);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [showLiveBuses, setShowLiveBuses] = useState(true);
  const [liveCount, setLiveCount] = useState(0);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [basemapKey, setBasemapKey] = useState<string>("vectorDark");

  const [telemetrySnapshot, setTelemetrySnapshot] = useState<{
    status: string;
    feedTimestampText: string;
    freshnessMode: "ARCHIVED" | "LIVE" | "STALE";
    engineFallbackText: string;
    activeBuffers: number;
    completedTraversals: number;
    totalVehicles: number;
  }>({
    status: "Initializing telemetry...",
    feedTimestampText: "16 Nov 2026, 10:02:55 AM IST",
    freshnessMode: "ARCHIVED",
    engineFallbackText: "Historical Empirical Tensor (W_eff = 0.0)",
    activeBuffers: 3091,
    completedTraversals: 0,
    totalVehicles: 5961,
  });

  const pendingRouteRef = useRef<GeoJSON.FeatureCollection | null>(null);
  const pendingLiveRef = useRef<GeoJSON.FeatureCollection | null>(null);

  const setupMapLayers = (map: maplibregl.Map) => {
    if (!map.getSource("selected-route")) {
      map.addSource("selected-route", {
        type: "geojson",
        data: pendingRouteRef.current || { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "selected-route-casing",
        type: "line",
        source: "selected-route",
        paint: {
          "line-color": "#0284c7",
          "line-width": 7,
          "line-opacity": 0.6,
        },
      });
      map.addLayer({
        id: "selected-route-line",
        type: "line",
        source: "selected-route",
        paint: {
          "line-color": "#38bdf8",
          "line-width": 4.5,
          "line-opacity": 0.95,
        },
      });
    } else if (pendingRouteRef.current) {
      (map.getSource("selected-route") as maplibregl.GeoJSONSource).setData(
        pendingRouteRef.current
      );
    }

    if (!map.getSource("live-buses")) {
      map.addSource("live-buses", {
        type: "geojson",
        data: pendingLiveRef.current || { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "live-buses-layer",
        type: "circle",
        source: "live-buses",
        paint: {
          "circle-radius": 3.5,
          "circle-color": "#10b981",
          "circle-opacity": 0.85,
          "circle-stroke-width": 1,
          "circle-stroke-color": "#064e3b",
        },
      });
    } else if (pendingLiveRef.current) {
      (map.getSource("live-buses") as maplibregl.GeoJSONSource).setData(
        pendingLiveRef.current
      );
    }
  };

  // Initialize MapLibre GL
  useEffect(() => {
    if (!mapContainer.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: mapContainer.current,
      style: BASEMAPS[basemapKey].style,
      center: [77.2167, 28.6139],
      zoom: 11,
      pitch: 20,
    });

    map.setMissingStyleImageResolver((id) => {
      if (!map.hasImage(id)) {
        map.addImage(id, { width: 1, height: 1, data: new Uint8Array(4) });
      }
    });

    map.on("styleimagemissing", (e) => {
      const id = e.id;
      if (!map.hasImage(id)) {
        map.addImage(id, { width: 1, height: 1, data: new Uint8Array(4) });
      }
    });

    map.addControl(new maplibregl.NavigationControl(), "top-right");
    map.on("load", () => {
      setupMapLayers(map);
    });
    mapRef.current = map;

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  const changeBasemap = (key: string) => {
    setBasemapKey(key);
    const map = mapRef.current;
    if (!map) return;
    map.setStyle(BASEMAPS[key].style);
    map.once("style.load", () => {
      setupMapLayers(map);
    });
  };

  // Poll live GTFS-RT vehicles periodically
  useEffect(() => {
    let timer: NodeJS.Timeout;
    const syncLive = async () => {
      try {
        const [vRes, sRes] = await Promise.all([
          fetch(`${API_BASE}/vehicles/live?limit=800`),
          fetch(`${API_BASE}/api/telemetry/snapshot`),
        ]);

        let vData: any = null;
        let sData: any = null;

        if (vRes.ok) {
          vData = await vRes.json();
          setLiveCount(vData.count || (vData.vehicles || []).length);
        }
        if (sRes.ok) {
          sData = await sRes.json();
        }

        // Determine actual feed timestamp and freshness
        const firstVehicleTs = vData?.vehicles?.[0]?.timestamp;
        let tsStr = "16 Nov 2026, 10:02:55 AM IST";
        let mode: "ARCHIVED" | "LIVE" | "STALE" = "ARCHIVED";
        let engineDesc = "Historical Empirical Tensor (W_eff = 0.0)";

        if (firstVehicleTs) {
          const d = new Date(firstVehicleTs * 1000);
          tsStr = d.toLocaleString("en-IN", {
            timeZone: "Asia/Kolkata",
            dateStyle: "medium",
            timeStyle: "medium",
          });
          const nowSec = Math.floor(Date.now() / 1000);
          const ageSec = nowSec - firstVehicleTs;

          if (sData?.completed_traversals > 0 && ageSec < 600) {
            mode = "LIVE";
            engineDesc = "Dynamic Live Blending (W_eff > 0)";
          } else if (ageSec < 1200 && sData?.completed_traversals > 0) {
            mode = "STALE";
            engineDesc = "Decaying Telemetry Blending";
          } else {
            mode = "ARCHIVED";
            engineDesc = "Historical Empirical Tensor (W_eff = 0.0)";
          }
        }

        setTelemetrySnapshot({
          status: sData?.status || vData?.status || "Archive Feed Loaded",
          feedTimestampText: tsStr,
          freshnessMode: mode,
          engineFallbackText: engineDesc,
          activeBuffers: sData?.active_buffers || 3091,
          completedTraversals: sData?.completed_traversals || 0,
          totalVehicles: sData?.total_vehicles || vData?.count || 5961,
        });

        if (!mapRef.current) return;

        const geojson: GeoJSON.FeatureCollection = {
          type: "FeatureCollection",
          features:
            showLiveBuses && vData
              ? (vData.vehicles || []).map((v: any) => ({
                  type: "Feature",
                  geometry: { type: "Point", coordinates: [v.lng, v.lat] },
                  properties: { route_id: v.route_id, vehicle_id: v.vehicle_id },
                }))
              : [],
        };
        pendingLiveRef.current = geojson;

        const map = mapRef.current;
        if (map.isStyleLoaded()) {
          setupMapLayers(map);
          const source = map.getSource("live-buses") as maplibregl.GeoJSONSource | undefined;
          if (source) {
            source.setData(geojson);
          }
        }
      } catch {}
    };

    syncLive();
    timer = setInterval(syncLive, 15000);
    return () => clearInterval(timer);
  }, [showLiveBuses]);

  const runSearch = async (o = origin, d = destination, dep = depart) => {
    setLoading(true);
    setErrorMsg(null);
    try {
      const params = new URLSearchParams({
        from: o.trim(),
        to: d.trim(),
        depart: `${dep}:00+05:30`,
      });
      const res = await fetch(`${API_BASE}/journeys?${params.toString()}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: "Routing error" }));
        throw new Error(err.detail || `Server returned ${res.status}`);
      }
      const data = await res.json();
      setResult(data);
      setSelectedIdx(0);
      if (data.journeys?.length) {
        drawJourneyOnMap(data, data.journeys[0]);
      }
    } catch (e: any) {
      setErrorMsg(e.message || "Failed to find routes");
    } finally {
      setLoading(false);
    }
  };

  const drawJourneyOnMap = async (payload: any, journey: any) => {
    const map = mapRef.current;
    if (!map) return;

    // Clear old markers
    markersRef.current.forEach((m) => m.remove());
    markersRef.current = [];

    const features: GeoJSON.Feature[] = [];

    for (const leg of journey.legs) {
      if (leg.mode === "BUS" && leg.segment_ids?.length) {
        try {
          const shapeRes = await fetch(
            `${API_BASE}/journeys/shape?segment_ids=${leg.segment_ids.join(",")}`
          );
          if (shapeRes.ok) {
            const geom = await shapeRes.json();
            if (geom.coordinates?.length) {
              features.push({
                type: "Feature",
                geometry: geom,
                properties: { mode: "BUS", route_name: leg.route_name },
              });
            }
          }
        } catch {}
      }
    }

    const fc: GeoJSON.FeatureCollection = { type: "FeatureCollection", features };
    pendingRouteRef.current = fc;

    if (map.isStyleLoaded()) {
      setupMapLayers(map);
      const routeSource = map.getSource("selected-route") as maplibregl.GeoJSONSource | undefined;
      if (routeSource) {
        routeSource.setData(fc);
      }
    }

    // Add markers for Origin and Destination
    if (payload.origin && payload.destination) {
      const origEl = document.createElement("div");
      origEl.className = "w-4 h-4 rounded-full bg-emerald-500 border-2 border-white shadow-lg";
      const destEl = document.createElement("div");
      destEl.className = "w-4 h-4 rounded-full bg-rose-500 border-2 border-white shadow-lg";

      const m1 = new maplibregl.Marker({ element: origEl })
        .setLngLat([payload.origin.lng, payload.origin.lat])
        .addTo(map);

      const m2 = new maplibregl.Marker({ element: destEl })
        .setLngLat([payload.destination.lng, payload.destination.lat])
        .addTo(map);

      markersRef.current = [m1, m2];

      let minLng = Math.min(payload.origin.lng, payload.destination.lng);
      let maxLng = Math.max(payload.origin.lng, payload.destination.lng);
      let minLat = Math.min(payload.origin.lat, payload.destination.lat);
      let maxLat = Math.max(payload.origin.lat, payload.destination.lat);

      for (const f of features) {
        if (f.geometry && f.geometry.type === "LineString") {
          for (const coord of (f.geometry as any).coordinates) {
            minLng = Math.min(minLng, coord[0]);
            maxLng = Math.max(maxLng, coord[0]);
            minLat = Math.min(minLat, coord[1]);
            maxLat = Math.max(maxLat, coord[1]);
          }
        }
      }

      map.fitBounds(
        [
          [minLng - 0.02, minLat - 0.02],
          [maxLng + 0.02, maxLat + 0.02],
        ],
        { padding: 70, maxZoom: 14 }
      );
    }
  };

  return (
    <div className="flex h-screen w-screen bg-slate-950 text-slate-100 font-sans antialiased overflow-hidden">
      {/* Sidebar Drawer */}
      <aside className="w-[420px] border-r border-slate-800/80 p-5 flex flex-col gap-4 overflow-y-auto bg-slate-950/95 backdrop-blur-md z-10 shrink-0">
        <div>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className="flex h-2.5 w-2.5 rounded-full bg-sky-400 animate-pulse" />
              <h1 className="text-base font-bold tracking-tight text-white flex items-center gap-1.5">
                Delhi Transit Engine
              </h1>
            </div>
            <a
              href="http://localhost:8000/docs"
              target="_blank"
              rel="noreferrer"
              className="text-[10px] text-sky-400 hover:text-sky-300 bg-sky-950/60 hover:bg-sky-900/60 border border-sky-800/60 px-2 py-0.5 rounded-md transition font-medium flex items-center gap-1"
              title="Open interactive Swagger API documentation on port 8000"
            >
              API Docs ↗
            </a>
          </div>
          <p className="text-xs text-slate-400 mt-0.5">
            Empirical GPS Speed Tensor + Live Telemetry Fusion
          </p>
        </div>

        {/* Inputs */}
        <div className="space-y-2.5 bg-slate-900/80 p-3.5 rounded-xl border border-slate-800/90 shadow-sm">
          <div className="relative">
            <MapPin className="absolute left-3 top-2.5 h-4 w-4 text-emerald-400" />
            <input
              value={origin}
              onChange={(e) => setOrigin(e.target.value)}
              placeholder="Origin stop name or lat,lon"
              className="w-full bg-slate-950 border border-slate-800 focus:border-sky-500 rounded-lg pl-9 pr-3 py-2 text-xs text-slate-100 placeholder-slate-500 outline-none transition"
            />
          </div>
          <div className="relative">
            <Navigation className="absolute left-3 top-2.5 h-4 w-4 text-rose-400" />
            <input
              value={destination}
              onChange={(e) => setDestination(e.target.value)}
              placeholder="Destination stop name or lat,lon"
              className="w-full bg-slate-950 border border-slate-800 focus:border-sky-500 rounded-lg pl-9 pr-3 py-2 text-xs text-slate-100 placeholder-slate-500 outline-none transition"
            />
          </div>
          <div className="relative">
            <Clock className="absolute left-3 top-2.5 h-4 w-4 text-sky-400" />
            <input
              type="datetime-local"
              value={depart}
              onChange={(e) => setDepart(e.target.value)}
              className="w-full bg-slate-950 border border-slate-800 focus:border-sky-500 rounded-lg pl-9 pr-3 py-2 text-xs text-slate-100 outline-none transition"
            />
          </div>
          <button
            onClick={() => runSearch()}
            disabled={loading}
            className="w-full bg-sky-500 hover:bg-sky-400 text-slate-950 font-semibold py-2 rounded-lg text-xs transition flex items-center justify-center gap-1.5 disabled:opacity-50"
          >
            {loading ? (
              <span className="flex items-center gap-2">
                <span className="w-3.5 h-3.5 border-2 border-slate-950 border-t-transparent rounded-full animate-spin" />
                Computing Empirical Paths...
              </span>
            ) : (
              <>
                <Search className="w-3.5 h-3.5" /> Find Bus Journeys
              </>
            )}
          </button>
        </div>

        {errorMsg && (
          <div className="text-xs bg-rose-500/10 border border-rose-500/30 text-rose-300 p-2.5 rounded-lg">
            {errorMsg}
          </div>
        )}

        {/* Benchmark Scenarios */}
        <div className="space-y-1.5">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-1">
            <Sparkles className="w-3 h-3 text-sky-400" /> Benchmark Scenarios
          </span>
          <div className="space-y-1">
            {PRESETS.map((p) => (
              <button
                key={p.label}
                onClick={() => {
                  setOrigin(p.from);
                  setDestination(p.to);
                  setDepart(p.depart);
                  runSearch(p.from, p.to, p.depart);
                }}
                className="w-full text-left text-xs bg-slate-900/60 hover:bg-slate-800/80 border border-slate-800/70 hover:border-slate-700 px-3 py-2 rounded-lg text-slate-300 transition"
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>

        {/* Results */}
        <div className="space-y-3 flex-1">
          {result?.journeys && (
            <div className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
              {result.journeys.length} Options Found
            </div>
          )}

          {result?.journeys?.map((j: any, idx: number) => {
            const breakdown = computeJourneyBreakdown(j);
            return (
              <div
                key={j.option_id}
                onClick={() => {
                  setSelectedIdx(idx);
                  drawJourneyOnMap(result, j);
                }}
                className={`p-3.5 rounded-xl border cursor-pointer transition ${
                  selectedIdx === idx
                    ? "bg-slate-900 border-sky-500 shadow-md ring-1 ring-sky-500/20"
                    : "bg-slate-900/40 border-slate-800/80 hover:border-slate-700"
                }`}
              >
                <div className="flex justify-between items-start gap-2">
                  <div>
                    <span className="text-xl font-extrabold text-white">
                      {j.total_duration_min} min
                    </span>
                    <div className="text-[11px] text-slate-400 mt-0.5">
                      {j.transfers === 0 ? "Direct Bus" : `${j.transfers} Transfer`} • Arrives{" "}
                      {new Date(j.estimated_arrival).toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit",
                      })}
                    </div>
                  </div>
                  <div className="text-right">
                    <span
                      className="inline-block text-[10px] font-medium px-2 py-0.5 rounded-full bg-slate-800/90 text-amber-300 border border-slate-700"
                      title="Comparison against empirical minimum duration across off-peak night hours 01:00–05:00 AM (hours 1, 2, 3, and 4 inclusive). Indicates estimated peak congestion friction relative to night transit operations, not timetable delay."
                    >
                      +{j.traffic_delay_min}m vs 01:00–05:00 baseline ({j.quiet_hour_duration_min}m)
                    </span>
                  </div>
                </div>

                {/* Journey-Time Component Breakdown */}
                <div className="mt-2.5 bg-slate-950/70 p-2 rounded-lg border border-slate-800/80">
                  <div className="grid grid-cols-5 gap-1 text-center text-[10px]">
                    <div>
                      <div className="text-slate-400">Access</div>
                      <div className="font-semibold text-slate-200">{breakdown.accessWalkMin}m</div>
                    </div>
                    <div className="border-l border-slate-800/80">
                      <div
                        className="text-amber-400 font-medium"
                        title="Estimated route headway wait (frequency model), not scheduled departure"
                      >
                        Exp. Wait*
                      </div>
                      <div className="font-semibold text-amber-300">{breakdown.expectedWaitMin}m</div>
                    </div>
                    <div className="border-l border-slate-800/80">
                      <div className="text-sky-400">Bus Ride</div>
                      <div className="font-semibold text-sky-300">{breakdown.busTravelMin}m</div>
                    </div>
                    <div className="border-l border-slate-800/80">
                      <div className="text-slate-400">Transfer</div>
                      <div className="font-semibold text-slate-200">{breakdown.transferWalkMin}m</div>
                    </div>
                    <div className="border-l border-slate-800/80">
                      <div className="text-slate-400">Egress</div>
                      <div className="font-semibold text-slate-200">{breakdown.egressWalkMin}m</div>
                    </div>
                  </div>
                  <div className="mt-1 pt-1 border-t border-slate-800/60 flex justify-between items-center text-[9px] text-slate-400">
                    <span title="Displayed components are rounded to 0.1m; total duration is computed from unrounded continuous sum before rounding.">
                      Sum: {breakdown.componentSum}m {Math.abs(breakdown.componentSum - breakdown.totalDurationMin) < 0.05 ? "=" : "≈"} Total {breakdown.totalDurationMin}m
                    </span>
                    <span className="italic">*Freq-based wait</span>
                  </div>
                </div>

                {/* Detailed Steps */}
                <div className="mt-3 space-y-2 border-t border-slate-800/80 pt-2.5">
                  {j.legs.map((leg: any, lIdx: number) => {
                    if (leg.mode === "WALK") {
                      return (
                        <div key={lIdx} className="text-xs flex items-center justify-between gap-2">
                          <div className="flex items-center gap-1.5 truncate">
                            <Footprints className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                            <span className="truncate text-slate-300">
                              {leg.from === leg.to ? (
                                <>Access walk at {leg.from}</>
                              ) : (
                                <>Walk to {leg.to}</>
                              )}
                            </span>
                          </div>
                          <span className="text-slate-400 shrink-0 text-[11px]">
                            {leg.duration_min}m ({leg.distance_m}m)
                          </span>
                        </div>
                      );
                    }

                    // BUS leg
                    return (
                      <div key={lIdx} className="space-y-1.5">
                        {leg.wait_min > 0 && (
                          <div className="text-xs flex items-center justify-between gap-2 pl-1 border-l-2 border-amber-500/50">
                            <div className="flex items-center gap-1.5 truncate">
                              <Clock className="w-3.5 h-3.5 text-amber-400 shrink-0" />
                              <span className="truncate text-amber-200/90 text-[11px]">
                                Expected wait at {leg.from_stop} (freq-based headway)
                              </span>
                            </div>
                            <span className="text-amber-300 font-semibold shrink-0 text-[11px]">
                              {leg.wait_min}m
                            </span>
                          </div>
                        )}
                        <div className="text-xs flex items-center justify-between gap-2">
                          <div className="flex items-center gap-1.5 truncate">
                            <Bus className="w-3.5 h-3.5 text-sky-400 shrink-0" />
                            <span className="truncate text-slate-300">
                              <strong className="text-sky-300 font-semibold">{leg.route_name}</strong>:{" "}
                              {leg.from_stop} → {leg.to_stop}
                            </span>
                          </div>
                          <span className="text-sky-300 font-semibold shrink-0 text-[11px]">
                            {leg.ride_min}m ({leg.stops_count} stops)
                          </span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </aside>

      {/* Map View */}
      <main className="flex-1 relative h-full">
        <div ref={mapContainer} className="w-full h-full" />

        {/* Telemetry Freshness & Fleet Status Pill */}
        <div className="absolute bottom-5 left-5 z-10 flex items-center gap-3 bg-slate-900/95 backdrop-blur-md border border-slate-700/80 px-4 py-2 rounded-xl text-xs shadow-xl">
          <div className="flex items-center gap-2.5">
            <span
              className={`w-2.5 h-2.5 rounded-full ${
                telemetrySnapshot.freshnessMode === "LIVE"
                  ? "bg-emerald-400 animate-pulse"
                  : telemetrySnapshot.freshnessMode === "STALE"
                  ? "bg-amber-400"
                  : "bg-amber-500"
              }`}
            />
            <div>
              <div className="font-semibold text-slate-100 flex items-center gap-2">
                <span>
                  {telemetrySnapshot.freshnessMode === "LIVE"
                    ? "Live Telemetry Active"
                    : telemetrySnapshot.freshnessMode === "STALE"
                    ? "Telemetry Stale"
                    : "Archived Telemetry Snapshot"}
                </span>
                <span className="text-[10px] px-1.5 py-0.2 rounded bg-slate-800 text-slate-300 border border-slate-700">
                  {telemetrySnapshot.totalVehicles.toLocaleString()} vehicles
                </span>
              </div>
              <div className="text-[10px] text-slate-400 flex items-center gap-2 mt-0.5">
                <span>Feed: {telemetrySnapshot.feedTimestampText}</span>
                <span>•</span>
                <span className="text-sky-300 font-medium">
                  {telemetrySnapshot.engineFallbackText}
                </span>
              </div>
            </div>
          </div>

          <button
            onClick={() => setShowLiveBuses((v) => !v)}
            className="ml-2 text-[11px] px-2.5 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition"
          >
            {showLiveBuses ? "Hide Fleet" : "Show Fleet"}
          </button>
        </div>

        {/* Basemap Switcher Pill */}
        <div className="absolute bottom-5 right-12 z-10 flex items-center gap-1 bg-slate-900/90 backdrop-blur-sm border border-slate-700/80 p-1 rounded-full text-xs shadow-lg">
          <div className="flex items-center gap-1 px-2 text-slate-400">
            <Layers className="w-3.5 h-3.5" />
            <span className="text-[11px] font-semibold text-slate-300">Style:</span>
          </div>
          {Object.entries(BASEMAPS).map(([key, config]) => (
            <button
              key={key}
              onClick={() => changeBasemap(key)}
              className={`px-2.5 py-1 rounded-full text-[11px] font-medium transition ${
                basemapKey === key
                  ? "bg-sky-500 text-white shadow-sm"
                  : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {config.label}
            </button>
          ))}
        </div>
      </main>
    </div>
  );
}
