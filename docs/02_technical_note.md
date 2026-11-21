# Technical Note: Delhi Transit Engine
### Real-Time Bus Traffic Model and Multi-Modal Transit Journey Planner (Problem Statement 1)
**Delhi Transport Stack Hackathon 2026**

---

## 1. Executive Summary

The **Delhi Transit Engine** transforms Delhi's fleet of thousands of public buses into a real-time distributed traffic sensor network. This technical note documents the mathematical principles, spatial algorithms, and architectural pipeline implemented to satisfy the three core requirements of Problem Statement 1:
1. **Map-Matching:** Snapping noisy, irregular GPS points to physical roads and GTFS transit shapes.
2. **Segment Speed Profiling:** Estimating traversal times and congestion delay across 106,160 directional road segments from historical GPS trajectories.
3. **Live & Historical Fusion:** Robustly blending streaming real-time GTFS-RT telemetry with historical speed priors using a multi-factor reliability framework.

---

## 2. Linking GPS Points to Roads and Routes (Map-Matching)

### 2.1 Spatial Reference Frame & Canonical Segments
- **Projected Coordinate Reference System:** All spatial geometries are projected to **EPSG:32643 (WGS 84 / UTM Zone 43N)**. This eliminates Euclidean distortion and ensures distance calculations are in true meters ($\pm 0.05\text{m}$).
- **Segment Network Definition:** Static GTFS `shapes.txt` polylines are partitioned into $106,160$ directional road segments indexed by $(r, s_{\text{from}}, s_{\text{to}})$. Each segment carries an along-track start distance $d_{\text{start}}$, end distance $d_{\text{end}}$, and arc length $L_s = d_{\text{end}} - d_{\text{start}}$.

### 2.2 Bounding Box & Outlier Filtering
Raw pings $(lat, lon, ts)$ are ingested and classified before projection:
- **Study Area Envelope:** Enforces Delhi NCT bounding box ($28.30^\circ\text{N} \le lat \le 28.95^\circ\text{N}$, $76.80^\circ\text{E} \le lon \le 77.45^\circ\text{E}$).
- **Corrupt Coordinate Isolation:** Pings with null, non-numeric, or mathematically impossible coordinates ($lat = 0.0, lon = 0.0$) are rejected.
- **Sub-Second Deduplication:** Consecutive pings for the same physical vehicle with $\Delta t \le 1.0\text{s}$ are filtered to eliminate duplicate transmissions.

### 2.3 Candidate Shape Generation & Orthogonal Snapping (M01–M13)
1. **Candidate Lookup:** When a ping reports `route_id`, candidate shapes $\mathcal{S}_{\text{cand}}$ are retrieved from static GTFS `trips.txt` joins. If `route_id` is unmapped or omitted, a KD-Tree queries all shapes within a $65\text{m}$ radius.
2. **Orthogonal Linear Referencing:** For each candidate shape $S \in \mathcal{S}_{\text{cand}}$, the ping $P(x, y)$ is projected using orthogonal linear referencing:
   $$d_{\text{along}} = \text{ST\_LineLocatePoint}(S, P) \cdot \text{length}(S)$$
   $$d_{\text{cross}} = \text{ST\_Distance}(S, P)$$
3. **Threshold & Matching Outcomes:**
   - **Snap Radius:** $d_{\text{cross}} \le 65.0\text{ meters}$.
   - **Matching Classification:**
     - `VALIDATED_INFERRED_MATCH`: Exactly one candidate shape within $65\text{m}$ (or single best with $\ge 20\text{m}$ margin).
     - `AMBIGUOUS_MATCH`: Multiple overlapping shapes within $65\text{m}$ where heading/route cannot uniquely disambiguate. Retained for active buffering; rejected for live speed emission.
     - `UNMATCHED_OFF_ROUTE`: $d_{\text{cross}} > 65.0\text{m}$ from all known network shapes (bus deadheading, depot layover, or detour).

---

## 3. Estimating Travel Times on Each Stretch of Road (Speed Model)

### 3.1 Historical Trajectory Ingestion & Temporal Slicing
From October 2026 daily movement databases, physical vehicle traces are reconstructed:
- **Monotonic Sorting:** Trajectories are ordered strictly by $(vehicle\_id, timestamp)$.
- **Time-of-Week Discretization:** The network is partitioned into $48$ time bins ($24$ hours of day $\times$ $2$ calendar day types: Weekday vs Weekend).
- **Total State Space:** $106,160 \text{ segments} \times 48 \text{ bins} = 5,095,680 \text{ state cells}$.

### 3.2 Signal Wait & Dwell Time Isolation
Traffic delay and passenger dwell are treated rigorously:
1. **Kinematic States (K01–K16):** Progression between successive pings is categorized into 16 mutually exclusive kinematic outcomes.
2. **Stationary Delay Isolation:** Consecutive stationary pings ($v < 0.5\text{ m/s}$) near intersections are capped at a single $90\text{s}$ red-light cycle. Cumulative standstill exceeding $300\text{s}$ is classified as depot/terminal layover and excluded from running speed calculations.
3. **Segment Traversal Extraction:** When a bus crosses segment boundary $d_{\text{start}}$ to $d_{\text{end}}$, traversal time is computed via boundary interpolation:
   $$\tau_s = t_{\text{exit}} - t_{\text{enter}}$$

### 3.3 Robust Aggregation & Hierarchical Fallback
For each segment $s$ and time bin $(is\_weekend, hour)$:
- **Primary Estimator:** Sample median traversal speed $\tilde{v}_{s, w, h} = \text{median}(\{v_{s, i}\})$.
- **Held-Out Validation:** Evaluated on $1,147,748$ held-out October 29 observations:
  - Static GTFS Timetable MAE: **$34.94\text{ seconds}$**
  - Historical Speed Model MAE: **$24.20\text{ seconds}$** (**$+30.74\%$ accuracy gain**)
  - Historical Speed Model RMSE: **$37.05\text{ seconds}$** (**$+21.25\%$ error reduction**)
- **4-Tier Hierarchical Fallback:**
  - *Tier 1 (Direct Sample):* $\ge 5$ observations in cell $(s, w, h)$.
  - *Tier 2 (Corridor Window):* Median of adjacent $\pm 1$ hour on same segment.
  - *Tier 3 (Shape Average):* Median speed across all segments of the parent GTFS shape.
  - *Tier 4 (Global Default):* City-wide historical median ($18.5\text{ km/h}$).

---

## 4. Combining Live and Historical Data (Real-Time Fusion)

### 4.1 Piecewise Freshness Function $\Phi(\Delta t)$
Real-time observations decay with age according to an approved piecewise function:
$$\Phi(\Delta t) = \begin{cases}
1.0 & \text{for } 0 \le \Delta t \le 600\text{s (0 to 10 min)} \\
1.0 - \dfrac{\Delta t - 600}{600} & \text{for } 600\text{s} < \Delta t < 1,200\text{s (10 to 20 min)} \\
0.0 & \text{for } \Delta t \ge 1,200\text{s (}\ge 20\text{ min, Hard Cutoff)}
\end{cases}$$

### 4.2 Multi-Factor Reliability Weight $W_{\text{eff}}$
To ensure noisy or single-vehicle anomalies cannot distort routing, effective live weight $W_{\text{eff}} \in [0, 1]$ is computed as the product of five independent metrics:
$$W_{\text{eff}} = \Phi(\Delta t) \cdot \Psi(N_{\text{veh}}) \cdot \bar{\mathcal{M}} \cdot \bar{\mathcal{Q}} \cdot \bar{\mathcal{P}}$$

| Factor | Description | Mathematical Formulation |
| :--- | :--- | :--- |
| **Freshness $\Phi$** | Temporal decay of observation age | Piecewise linear decay (600s–1200s) |
| **Support $\Psi$** | Independent physical vehicle count | $\Psi(N) = \min(1.0, N / 5)$ (saturation at 5 distinct vehicles) |
| **Match Confidence $\bar{\mathcal{M}}$** | Map-matching geometric quality | $1.0$ for scheduled trip-id match, $0.70$ for inferred shape |
| **Quality $\bar{\mathcal{Q}}$** | Kinematic plausibility | Penalizes trajectory jitter and high cross-track error |
| **Provenance $\bar{\mathcal{P}}$** | Source authority | $1.0$ for official GTFS-RT feed; degraded for unvalidated streams |

### 4.3 Unified Weighted Median Speed Estimation
For segments with live observations, the live speed $v_{\text{live}}$ is computed via **sample-weighted median** across all deduplicated vehicles $k \in \{1 \dots K\}$:
$$\sum_{k: v_k \le v_{\text{live}}} w_k \ge \frac{1}{2} \sum_{k=1}^K w_k$$

The final blended segment traversal time $\tau_{\text{blended}}$ is:
$$\tau_{\text{blended}} = W_{\text{eff}} \cdot \tau_{\text{live}} + (1 - W_{\text{eff}}) \cdot \tau_{\text{hist}}$$

When telemetry is older than 20 minutes ($W_{\text{eff}} = 0.0$), the system seamlessly and deterministically falls back to the calibrated historical tensor.

---

## 5. Journey-Planning Engine & Multi-Modal Routing

### 5.1 Diverse Alternative Itineraries Router (Iterative Route-Penalized Shortest Path)

The routing engine does not implement multi-criteria Pareto frontier exploration (such as RAPTOR or bi-criterion Pareto-Dijkstra). Instead, it implements an **iterative route-penalized diverse k-shortest path heuristic**:
1. The first pass computes the minimum generalized arrival time itinerary from origin to destination using Dijkstra's algorithm.
2. For subsequent passes ($k = 2, 3$), all network edges belonging to `route_id`s utilized in previously selected itineraries receive an additive penalty ($\Delta t_{\text{pen}} = +600.0\text{ s}$, or 10 minutes) on their generalized traversal cost.
3. This penalty forces Dijkstra's algorithm to explore structurally diverse corridors and alternative transit lines rather than returning trivial perturbations of the primary route.
4. Consequently, returned options are formally designated as **diverse alternative itineraries via iterative route penalization**, not mathematically guaranteed Pareto-optimal frontiers.
The routing engine models the complete passenger journey across five distinct components:
$$\text{Total Duration} = T_{\text{access}} + T_{\text{wait}} + T_{\text{bus}} + T_{\text{transfer}} + T_{\text{egress}}$$

1. **Access Walking ($T_{\text{access}}$):** Walk from origin coordinate to nearest stops ($r \le 650\text{m}$) at $1.2\text{ m/s}$ with a $1.25$ Manhattan detour factor.
2. **Frequency-Based Waiting ($T_{\text{wait}}$):** Evaluates expected waiting time from route frequency (half-headway model + $3.5\text{ min}$ boarding friction).
3. **In-Vehicle Bus Travel ($T_{\text{bus}}$):** Edge traversal cost dynamically looked up from $\tau_{\text{blended}}$ (or $\tau_{\text{hist}}$).
4. **Footpath Transfers ($T_{\text{transfer}}$):** Precomputed transfer graph between stops ($< 350\text{m}$) allowing multi-route interchange.
5. **Egress Walking ($T_{\text{egress}}$):** Final walk from destination stop to target coordinate.

### 5.2 Off-Peak Reference Baseline
Every itinerary compares estimated travel time against an empirical **01:00–05:00 AM off-peak baseline reference** ($T_{\text{quiet}}$, computed as the minimum historical duration across hours 1, 2, 3, and 4 inclusive in `pipeline/04_build_speed_model.py`):
$$\Delta_{\text{friction}} = \max(0.0, T_{\text{total}} - T_{\text{quiet}})$$
*Note:* This represents an indicative off-peak empirical baseline rather than measured private vehicle free-flow traffic.

### 5.3 Display Rounding vs. Unrounded Continuous Totals
Individual journey breakdown components (Access Walking, Frequency-Based Waiting, In-Vehicle Bus Ride, Transfer Walking, Egress Walking) are displayed rounded to $0.1\text{ minutes}$ for user readability. Total journey duration is computed directly from the unrounded continuous sum of seconds before a single final rounding:
$$\text{total\_min} = \text{round}\left(\frac{\sum t_{\text{component\_sec}}}{60.0}, 1\right)$$
Because each component is rounded independently, the sum of displayed rounded components may occasionally differ by $\pm 0.1\text{ min}$ from the displayed total duration due to normal floating-point rounding (e.g. $1.86 + 19.00 + 79.57 + 0.00 + 5.96 = 106.39\text{ min} \to 106.4\text{ min}$, while component display rounds sum to $1.9 + 19.0 + 79.6 + 0.0 + 6.0 = 106.5\text{ min}$).
This allows commuters to instantly visualize the exact penalty imposed by peak-hour traffic congestion.
