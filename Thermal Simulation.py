"""
Cyclosys — Thermal Network Simulation Engine
=============================================
Architecture: Reduced-Order Model (ROM) using a thermal RC-network analogy.

Each rack node is modeled as:
  - A heat source:      Q_rack  [W]        (IT load dissipation)
  - A thermal mass:     C_th    [J/°C]     (capacitor — rack thermal inertia)
  - A conduction path:  R_th    [°C/W]     (resistor — rack-to-coolant coupling)

Airflow / cooling loops are modeled as directed edges in a graph
(NetworkX) whose edge weights encode the effective thermal resistance
between nodes (including CRAC/CRAH supply paths).

Model Predictive Control (MPC) predicts Q_cool over a receding horizon
using a linearised state-space representation of the network.

Author : Cyclosys Senior Architecture Team
Version: 1.0.0
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# 1.  DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RackNode:
    """
    Represents a single server rack as a lumped thermal node.

    Parameters
    ----------
    rack_id : str           Unique identifier  e.g. "R_02_05"
    row : int               Grid row index (0-based)
    col : int               Grid column index (0-based)
    power_density_kw : float  Rated IT load [kW].  AI/GPU clusters >> storage.
    thermal_resistance : float  R_th [°C/W] — coupling to supply air stream.
    thermal_capacitance : float C_th [J/°C] — thermal mass of the rack.
    cooling_efficiency : float  η ∈ [0, 1].  Set to 0 to simulate failure.
    T_inlet : float         Current supply-air temperature [°C].
    T_node  : float         Current rack average temperature [°C] (state).
    """
    rack_id: str
    row: int
    col: int
    power_density_kw: float = 5.0          # default: standard compute rack
    thermal_resistance: float = 0.0005     # °C/W — typical CRAC-coupled rack (5 kW → ΔT ~2.5 °C)
    thermal_capacitance: float = 10_000.0  # J/°C — ~10 kJ/°C per rack
    cooling_efficiency: float = 1.0        # 1.0 = fully operational
    T_inlet: float = 18.0                  # °C — ASHRAE A2 supply default
    T_node: float = 25.0                   # °C — initial rack temperature

    # ── derived ──────────────────────────────────────────────────────────────
    @property
    def Q_watts(self) -> float:
        """IT heat dissipation in Watts."""
        return self.power_density_kw * 1_000.0

    @property
    def T_steady_state(self) -> float:
        """
        Steady-state rack temperature under full cooling:
          T_ss = T_inlet + Q * R_th * (1 − η_loss)
        η_loss captures how much thermal resistance *increases* when cooling
        efficiency degrades (η → 0 doubles effective R_th).
        """
        effective_R = self.thermal_resistance / max(self.cooling_efficiency, 1e-6)
        return self.T_inlet + self.Q_watts * effective_R

    @property
    def is_critical(self) -> bool:
        """Flag rack as critical if steady-state temperature ≥ 35 °C (ASHRAE limit)."""
        return self.T_steady_state >= 35.0


@dataclass
class CoolingNode:
    """
    Represents a CRAC/CRAH cooling unit or liquid-cooling distribution manifold.

    cooling_capacity_kw : float  Maximum extractable heat [kW].
    efficiency          : float  COP-normalised efficiency ∈ [0, 1].
                                 Set to 0 to simulate maintenance/failure.
    T_supply            : float  Supply air/water temperature [°C].
    """
    node_id: str
    cooling_capacity_kw: float = 30.0
    efficiency: float = 1.0
    T_supply: float = 18.0
    assigned_racks: List[str] = field(default_factory=list)

    @property
    def available_capacity_kw(self) -> float:
        return self.cooling_capacity_kw * self.efficiency


# ─────────────────────────────────────────────────────────────────────────────
# 2.  THERMAL SIMULATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class ThermalSimulationEngine:
    """
    Lightweight thermal network simulation engine for data-centre cooling.

    Core algorithm
    ──────────────
    1. Build an undirected weighted graph G where:
         • nodes  = rack nodes + cooling nodes
         • edges  = thermal conductance paths (1/R_th)
    2. Assemble the nodal thermal conductance matrix [K] from G.
    3. Integrate the RC ODE forward in time using explicit Euler:
         C · dT/dt = Q_source − [K] · (T − T_ref)
    4. MPC layer runs a receding-horizon optimisation over
       forecasted workload to pre-adjust cooling set-points.
    5. Resilience mode: zero out a CoolingNode's efficiency,
       re-solve the steady state, and report thermal overshoot.
    """

    # ── class-level safety limits (ASHRAE A2 envelope) ────────────────────
    T_CRITICAL_HIGH: float = 35.0   # °C  — rack inlet critical threshold
    T_WARNING_HIGH:  float = 30.0   # °C  — rack inlet warning threshold
    T_AMBIENT:       float = 22.0   # °C  — ambient hall temperature
    DT_SIM:          float = 60.0   # s   — simulation time-step (1 minute)

    def __init__(
        self,
        n_rows: int = 4,
        n_cols: int = 6,
        default_power_kw: float = 5.0,
        t_supply: float = 18.0,
    ):
        """
        Initialise a uniform N×M rack grid.

        Parameters
        ----------
        n_rows, n_cols      : Grid dimensions.
        default_power_kw    : Default IT load per rack [kW].
        t_supply            : CRAC supply temperature [°C].
        """
        self.n_rows = n_rows
        self.n_cols = n_cols
        self.t_supply = t_supply

        # Storage for rack and cooling objects
        self.racks:   Dict[str, RackNode]    = {}
        self.coolers: Dict[str, CoolingNode] = {}

        # Thermal network graph
        self.graph: nx.Graph = nx.Graph()

        # Time-series history for Streamlit rendering
        self._history: List[Dict] = []

        # Seed the grid
        self._build_grid(default_power_kw)
        self._attach_cooling_nodes()
        self._build_thermal_graph()

    # ─────────────────────────────────────────────────────────────────────
    # 2a.  Grid & graph construction
    # ─────────────────────────────────────────────────────────────────────

    def _rack_id(self, row: int, col: int) -> str:
        return f"R_{row:02d}_{col:02d}"

    def _cooler_id(self, idx: int) -> str:
        return f"C_{idx:02d}"

    def _build_grid(self, default_power_kw: float) -> None:
        """Populate the racks dict with a uniform N×M configuration."""
        for r in range(self.n_rows):
            for c in range(self.n_cols):
                rid = self._rack_id(r, c)
                self.racks[rid] = RackNode(
                    rack_id=rid,
                    row=r,
                    col=c,
                    power_density_kw=default_power_kw,
                    T_inlet=self.t_supply,
                    T_node=self.t_supply + default_power_kw * 1_000 * 0.05,
                )

    def _attach_cooling_nodes(self) -> None:
        """
        One CRAC unit per two rack columns (hot/cold aisle pair).
        Assign racks to their nearest CRAC.
        """
        n_coolers = math.ceil(self.n_cols / 2)
        for i in range(n_coolers):
            cid = self._cooler_id(i)
            assigned = []
            for r in range(self.n_rows):
                for c in [2 * i, 2 * i + 1]:
                    if c < self.n_cols:
                        assigned.append(self._rack_id(r, c))
            self.coolers[cid] = CoolingNode(
                node_id=cid,
                cooling_capacity_kw=self.n_rows * 30.0,
                T_supply=self.t_supply,
                assigned_racks=assigned,
            )

    def _build_thermal_graph(self) -> None:
        """
        Construct the thermal resistance network graph.

        Edge weight = thermal conductance g = 1 / R_th [W/°C].

        Topology:
          • Rack ↔ adjacent racks  (conduction through shared air plenum)
          • Rack ↔ CRAC            (convection — primary cooling path)
        """
        self.graph.clear()

        # Add rack nodes
        for rid, rack in self.racks.items():
            self.graph.add_node(rid, type="rack", obj=rack)

        # Add cooler nodes
        for cid, cooler in self.coolers.items():
            self.graph.add_node(cid, type="cooler", obj=cooler)

        # Rack ↔ rack edges (4-connected grid, shared plenum conductance)
        for rid, rack in self.racks.items():
            for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                nr, nc = rack.row + dr, rack.col + dc
                if 0 <= nr < self.n_rows and 0 <= nc < self.n_cols:
                    neighbour_id = self._rack_id(nr, nc)
                    g_plenum = 5.0  # W/°C — air-side inter-rack conductance
                    if not self.graph.has_edge(rid, neighbour_id):
                        self.graph.add_edge(rid, neighbour_id, conductance=g_plenum)

        # Rack ↔ CRAC edges
        for cid, cooler in self.coolers.items():
            for rid in cooler.assigned_racks:
                rack = self.racks[rid]
                # Primary cooling conductance = efficiency / R_th
                g_crac = cooler.efficiency / max(rack.thermal_resistance, 1e-9)
                self.graph.add_edge(rid, cid, conductance=g_crac)

    def set_power_density(
        self,
        rack_id: str,
        power_kw: float,
        rack_type: str = "standard",
    ) -> None:
        """
        Override the power density of a specific rack.

        rack_type hints:
          "ai_gpu"   → high density, lower R_th (liquid cooling assumed)
          "storage"  → low density, higher R_th (air-only)
          "standard" → default compute
        """
        type_presets = {
            "ai_gpu":   {"thermal_resistance": 0.0004, "thermal_capacitance": 8_000},
            "storage":  {"thermal_resistance": 0.0008, "thermal_capacitance": 15_000},
            "standard": {"thermal_resistance": 0.0005, "thermal_capacitance": 10_000},
        }
        if rack_id not in self.racks:
            raise KeyError(f"Rack '{rack_id}' not found in grid.")
        preset = type_presets.get(rack_type, type_presets["standard"])
        rack = self.racks[rack_id]
        rack.power_density_kw     = power_kw
        rack.thermal_resistance   = preset["thermal_resistance"]
        rack.thermal_capacitance  = preset["thermal_capacitance"]
        # Rebuild edges for this rack in the graph
        self._rebuild_rack_edges(rack_id)

    def _rebuild_rack_edges(self, rack_id: str) -> None:
        """Re-compute edge conductances touching a modified rack."""
        rack = self.racks[rack_id]
        for cid, cooler in self.coolers.items():
            if rack_id in cooler.assigned_racks and self.graph.has_edge(rack_id, cid):
                g_crac = cooler.efficiency / max(rack.thermal_resistance, 1e-9)
                self.graph[rack_id][cid]["conductance"] = g_crac

    # ─────────────────────────────────────────────────────────────────────
    # 2b.  MPC Cooling Demand Calculator
    # ─────────────────────────────────────────────────────────────────────

    def compute_mpc_cooling_demand(
        self,
        workload_forecast_kw: List[float],
        sensor_telemetry: Dict[str, float],
        horizon_steps: int = 10,
        safety_margin: float = 1.15,
    ) -> Dict:
        """
        Model Predictive Control — cooling demand estimator.

        Uses a linearised single-zone thermal model to project rack
        temperatures over `horizon_steps` time steps and calculates
        the minimum cooling power required to keep all racks below
        T_WARNING_HIGH.

        Parameters
        ----------
        workload_forecast_kw  : Predicted total IT load per future step [kW].
                                Length must equal horizon_steps.
        sensor_telemetry      : {rack_id: current_temperature_°C} from sensors.
        horizon_steps         : MPC prediction horizon (default 10 min).
        safety_margin         : Multiply Q_cool by this factor for headroom.

        Returns
        -------
        dict with keys:
          Q_cool_kw_per_step  : Required cooling per step [kW]
          Q_cool_peak_kw      : Peak cooling demand over horizon [kW]
          T_predicted_max     : Maximum predicted rack temperature [°C]
          set_point_T_supply  : Recommended CRAC supply temperature [°C]
          horizon_df          : DataFrame for st.line_chart
        """
        if len(workload_forecast_kw) < horizon_steps:
            warnings.warn("Forecast shorter than horizon; padding with last value.")
            pad = workload_forecast_kw[-1] if workload_forecast_kw else 0.0
            workload_forecast_kw = list(workload_forecast_kw) + [pad] * (
                horizon_steps - len(workload_forecast_kw)
            )

        # Update rack temperatures from telemetry
        for rid, temp in sensor_telemetry.items():
            if rid in self.racks:
                self.racks[rid].T_node = float(temp)

        # Aggregate thermal state
        total_C  = sum(r.thermal_capacitance for r in self.racks.values())
        total_R  = np.mean([r.thermal_resistance for r in self.racks.values()])
        T_current = np.mean([r.T_node for r in self.racks.values()])

        Q_cool_per_step, T_pred_per_step = [], []
        T = T_current

        for step in range(horizon_steps):
            Q_it_w = workload_forecast_kw[step] * 1_000.0   # kW → W

            # Required cooling to hold T ≤ T_WARNING_HIGH:
            # Q_cool = Q_it − (T_warning − T_current) * C / dt
            delta_T_allowed = max(self.T_WARNING_HIGH - T, 0.0)
            Q_passive_w     = delta_T_allowed * total_C / self.DT_SIM
            Q_cool_w        = max(Q_it_w - Q_passive_w, 0.0) * safety_margin

            # Forward Euler temperature update
            dT = (Q_it_w - Q_cool_w) * self.DT_SIM / total_C
            T  = T + dT

            Q_cool_per_step.append(Q_cool_w / 1_000.0)   # back to kW
            T_pred_per_step.append(round(T, 3))

        # Optimal supply temperature — target minimum feasible T_supply
        Q_peak_kw    = max(Q_cool_per_step)
        T_max_pred   = max(T_pred_per_step)
        # Simple inverse: T_supply = T_warning - Q_peak * R_eff
        R_eff        = total_R / len(self.racks)
        T_set_supply = max(
            self.t_supply,
            self.T_WARNING_HIGH - Q_peak_kw * 1_000.0 * R_eff,
        )

        horizon_df = pd.DataFrame({
            "step":         list(range(horizon_steps)),
            "Q_cool_kw":    Q_cool_per_step,
            "T_predicted":  T_pred_per_step,
            "W_forecast_kw": workload_forecast_kw[:horizon_steps],
        }).set_index("step")

        return {
            "Q_cool_kw_per_step":  Q_cool_per_step,
            "Q_cool_peak_kw":      round(Q_peak_kw, 2),
            "T_predicted_max":     round(T_max_pred, 2),
            "set_point_T_supply":  round(T_set_supply, 2),
            "horizon_df":          horizon_df,
        }

    # ─────────────────────────────────────────────────────────────────────
    # 2c.  Transient simulation (RC integration)
    # ─────────────────────────────────────────────────────────────────────

    def run_simulation(
        self,
        duration_minutes: int = 60,
        workload_profile: Optional[Dict[str, List[float]]] = None,
    ) -> pd.DataFrame:
        """
        Integrate the full nodal RC network over `duration_minutes`.

        dT_i/dt = (Q_i − Σ_j g_ij (T_i − T_j)) / C_i

        For cooling nodes the temperature is held fixed at T_supply
        (ideal chiller assumption — can be relaxed for chiller dynamics).

        Parameters
        ----------
        duration_minutes : Simulation duration.
        workload_profile : Optional {rack_id: [power_kw_per_step]} dict
                           to model dynamic load changes over time.

        Returns
        -------
        DataFrame  rows = time steps, columns = rack temperatures.
                   Ready for st.line_chart.
        """
        n_steps = duration_minutes   # 1 step = 1 minute = DT_SIM seconds
        records = []

        for step in range(n_steps):
            # Optionally update workload
            if workload_profile:
                for rid, profile in workload_profile.items():
                    if rid in self.racks and step < len(profile):
                        self.racks[rid].power_density_kw = profile[rid][step]

            row = {"time_min": step}

            # Nodal energy balance — explicit Euler
            dT_map: Dict[str, float] = {}
            for rid, rack in self.racks.items():
                Q_in = rack.Q_watts  # IT heat generation

                # Sum conductance-weighted temperature differences with neighbours
                Q_net_exchange = 0.0
                for nbr in self.graph.neighbors(rid):
                    g = self.graph[rid][nbr]["conductance"]
                    if nbr in self.racks:
                        T_nbr = self.racks[nbr].T_node
                    elif nbr in self.coolers:
                        # Cooling node is a fixed temperature boundary
                        cooler = self.coolers[nbr]
                        # If cooler has failed (η=0), it acts as an open circuit
                        g_eff  = g * cooler.efficiency
                        T_nbr  = cooler.T_supply
                        Q_net_exchange += g_eff * (rack.T_node - T_nbr)
                        continue
                    Q_net_exchange += g * (rack.T_node - T_nbr)

                dT = (Q_in - Q_net_exchange) * self.DT_SIM / rack.thermal_capacitance
                dT_map[rid] = dT
                row[rid] = round(rack.T_node, 3)

            # Commit temperature updates after all nodes evaluated (explicit Euler)
            for rid, dT in dT_map.items():
                self.racks[rid].T_node = max(
                    self.t_supply,                       # floor at supply temp
                    self.racks[rid].T_node + dT,
                )

            records.append(row)

        df = pd.DataFrame(records).set_index("time_min")
        self._history = records
        return df

    # ─────────────────────────────────────────────────────────────────────
    # 2d.  Resilience / Failure Simulation
    # ─────────────────────────────────────────────────────────────────────

    def simulate_cooling_failure(
        self,
        failed_cooler_id: str,
        failure_duration_minutes: int = 30,
        workload_kw_per_rack: Optional[float] = None,
    ) -> Dict:
        """
        Simulate a cooling loop maintenance / failure event.

        Sets CoolingNode `failed_cooler_id` to η = 0, runs the transient
        simulation, then restores normal operations and reports:
          - Per-rack peak temperatures during the outage
          - Which racks breach T_CRITICAL_HIGH (35 °C)
          - Recommended load-shedding list to maintain stability
          - Heatmap DataFrame (N×M) for Streamlit st.dataframe / plotly

        Parameters
        ----------
        failed_cooler_id         : ID of the CRAC/CRAH unit to fail.
        failure_duration_minutes : How long the unit is offline.
        workload_kw_per_rack     : Override rack loads during failure period.

        Returns
        -------
        dict with keys:
          peak_temp_df   : DataFrame — peak rack temps during failure.
          heatmap_df     : N×M DataFrame — spatial thermal map.
          critical_racks : List of rack IDs that breached T_CRITICAL_HIGH.
          shedding_plan  : Ordered list of racks to power-down (hottest first).
          resilience_score : float ∈ [0, 1] — fraction of racks staying safe.
          time_series_df : Full per-rack temperature time series.
        """
        if failed_cooler_id not in self.coolers:
            raise KeyError(f"Cooling node '{failed_cooler_id}' not found.")

        # ── 1. Snapshot pre-failure state ────────────────────────────────
        pre_failure_temps = {rid: r.T_node for rid, r in self.racks.items()}

        # ── 2. Apply failure ─────────────────────────────────────────────
        cooler = self.coolers[failed_cooler_id]
        original_efficiency = cooler.efficiency
        cooler.efficiency = 0.0
        # Rebuild edges to propagate η=0
        self._build_thermal_graph()

        # Optionally bump workload
        original_loads = {}
        if workload_kw_per_rack is not None:
            for rid in cooler.assigned_racks:
                if rid in self.racks:
                    original_loads[rid] = self.racks[rid].power_density_kw
                    self.racks[rid].power_density_kw = workload_kw_per_rack

        # ── 3. Run transient simulation under failure ─────────────────────
        failure_ts_df = self.run_simulation(duration_minutes=failure_duration_minutes)

        # ── 4. Compute resilience metrics ────────────────────────────────
        peak_temps = failure_ts_df.max(axis=0).rename("peak_temp_C")
        critical_racks = peak_temps[peak_temps >= self.T_CRITICAL_HIGH].index.tolist()

        # Spatial heatmap — reshape peak temperatures to N×M grid
        heatmap_data = np.full((self.n_rows, self.n_cols), np.nan)
        for rid in self.racks:
            rack = self.racks[rid]
            if rid in peak_temps.index:
                heatmap_data[rack.row][rack.col] = peak_temps[rid]

        heatmap_df = pd.DataFrame(
            heatmap_data,
            index=[f"Row_{r}" for r in range(self.n_rows)],
            columns=[f"Col_{c}" for c in range(self.n_cols)],
        )

        # Load-shedding plan: rank affected racks by peak temperature (desc)
        affected = [
            rid for rid in cooler.assigned_racks
            if rid in peak_temps.index
        ]
        shedding_plan = (
            peak_temps[affected]
            .sort_values(ascending=False)
            .index.tolist()
        )

        total_racks    = len(self.racks)
        safe_racks     = total_racks - len(critical_racks)
        resilience_score = safe_racks / total_racks

        # ── 5. Restore system state ───────────────────────────────────────
        cooler.efficiency = original_efficiency
        for rid, load in original_loads.items():
            self.racks[rid].power_density_kw = load
        for rid, temp in pre_failure_temps.items():
            self.racks[rid].T_node = temp
        self._build_thermal_graph()

        return {
            "peak_temp_df":      peak_temps.to_frame(),
            "heatmap_df":        heatmap_df,
            "critical_racks":    critical_racks,
            "shedding_plan":     shedding_plan,
            "resilience_score":  round(resilience_score, 4),
            "time_series_df":    failure_ts_df,
            "failed_cooler":     failed_cooler_id,
            "failure_duration":  failure_duration_minutes,
        }

    # ─────────────────────────────────────────────────────────────────────
    # 2e.  Steady-state snapshot utilities
    # ─────────────────────────────────────────────────────────────────────

    def get_steady_state_heatmap(self) -> pd.DataFrame:
        """
        Return an N×M DataFrame of steady-state temperatures.
        Compatible with Streamlit st.dataframe + plotly imshow.
        """
        data = np.full((self.n_rows, self.n_cols), np.nan)
        for rid, rack in self.racks.items():
            data[rack.row][rack.col] = round(rack.T_steady_state, 2)
        return pd.DataFrame(
            data,
            index=[f"Row_{r}" for r in range(self.n_rows)],
            columns=[f"Col_{c}" for c in range(self.n_cols)],
        )

    def get_system_summary(self) -> Dict:
        """
        Return a structured summary dictionary ready for Streamlit st.metric.

        Keys
        ----
        total_it_load_kw     : Total IT heat generated [kW].
        total_cooling_kw     : Total available cooling capacity [kW].
        pue_estimate         : Rough PUE estimate.
        rack_count           : Total rack count.
        critical_rack_count  : Racks above T_CRITICAL_HIGH.
        avg_rack_temp_C      : Average current rack temperature [°C].
        max_rack_temp_C      : Maximum current rack temperature [°C].
        cooling_headroom_kw  : Spare cooling capacity [kW].
        """
        total_it   = sum(r.power_density_kw for r in self.racks.values())
        total_cool = sum(c.available_capacity_kw for c in self.coolers.values())
        temps      = [r.T_node for r in self.racks.values()]
        critical   = sum(1 for r in self.racks.values() if r.is_critical)

        # Simplified PUE: assume cooling overhead ≈ 0.3 × IT load
        pue = 1.0 + (0.3 * total_it) / max(total_it, 1e-6)

        return {
            "total_it_load_kw":    round(total_it, 2),
            "total_cooling_kw":    round(total_cool, 2),
            "pue_estimate":        round(pue, 3),
            "rack_count":          len(self.racks),
            "critical_rack_count": critical,
            "avg_rack_temp_C":     round(float(np.mean(temps)), 2),
            "max_rack_temp_C":     round(float(np.max(temps)), 2),
            "cooling_headroom_kw": round(total_cool - total_it, 2),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 3.  EXAMPLE USAGE  (also serves as a smoke-test)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 65)
    print("  Cyclosys Thermal Network Simulation Engine — Demo")
    print("=" * 65)

    # ── Step 1: Build a 4×6 data-centre grid ─────────────────────────────
    engine = ThermalSimulationEngine(n_rows=4, n_cols=6, default_power_kw=5.0)

    # ── Step 2: Configure heterogeneous rack types ────────────────────────
    # Two AI/GPU high-density clusters in column 2 & 3 of rows 0–1
    for row in range(2):
        for col in [2, 3]:
            engine.set_power_density(f"R_{row:02d}_{col:02d}", power_kw=25.0, rack_type="ai_gpu")

    # Storage racks — low density, columns 4–5
    for row in range(4):
        for col in [4, 5]:
            engine.set_power_density(f"R_{row:02d}_{col:02d}", power_kw=2.0, rack_type="storage")

    # ── Step 3: System summary ────────────────────────────────────────────
    summary = engine.get_system_summary()
    print("\n📊 System Summary")
    for k, v in summary.items():
        print(f"   {k:<30s}: {v}")

    # ── Step 4: Steady-state heatmap ──────────────────────────────────────
    heatmap = engine.get_steady_state_heatmap()
    print("\n🌡  Steady-State Heatmap (°C):")
    print(heatmap.to_string())

    # ── Step 5: MPC cooling demand over 10-minute horizon ─────────────────
    forecast_kw   = [90, 95, 100, 105, 110, 115, 110, 105, 100, 95]
    telemetry     = {rid: rack.T_node for rid, rack in engine.racks.items()}
    mpc_result    = engine.compute_mpc_cooling_demand(forecast_kw, telemetry)

    print("\n🧠 MPC Cooling Demand (10-min horizon)")
    print(f"   Peak cooling required : {mpc_result['Q_cool_peak_kw']} kW")
    print(f"   Max predicted temp    : {mpc_result['T_predicted_max']} °C")
    print(f"   Recommended T_supply  : {mpc_result['set_point_T_supply']} °C")
    print("\n   Horizon DataFrame (for st.line_chart):")
    print(mpc_result["horizon_df"].to_string())

    # ── Step 6: Transient simulation ──────────────────────────────────────
    print("\n⏱  Running 30-minute transient simulation …")
    ts_df = engine.run_simulation(duration_minutes=30)
    print(f"   Shape: {ts_df.shape}  (time steps × racks)")
    print("   Final temperatures (sample):")
    print(ts_df.tail(1).iloc[:, :6].to_string())

    # ── Step 7: Cooling failure resilience test ───────────────────────────
    print("\n🔴 Simulating failure of cooling unit C_00 for 20 minutes …")
    resilience = engine.simulate_cooling_failure("C_00", failure_duration_minutes=20)
    print(f"   Resilience score  : {resilience['resilience_score']:.2%}")
    print(f"   Critical racks    : {resilience['critical_racks']}")
    print(f"   Load-shedding plan: {resilience['shedding_plan']}")
    print("\n   Spatial Heatmap During Failure (°C):")
    print(resilience["heatmap_df"].to_string())

    print("\n✅ Demo complete. All outputs are Streamlit-ready DataFrames.")
