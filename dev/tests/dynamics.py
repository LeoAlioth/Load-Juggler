"""Closed-loop dynamics of the control loop, at a thousand times wall clock.

The Docker rig in ``dev/ha-test`` answers "does this work on a real Home
Assistant". This answers a narrower question much faster: given a site whose
production is MOVING, does the loop track it, and does it ring at a fixed
operating point? A 300 s scenario at a 1 s cadence is 300 iterations, so a
sweep that costs 40 minutes of wall clock on the rig runs here in under a
second.

That speed is the point. Tuning a control loop from single runs is how you end
up preferring an unstable configuration: on 2026-09-08 a shorter permit filter
scored a BETTER mean tracking error (501 W against 608 W) while oscillating
600 W peak-to-peak on a dead-flat input, because mean-|error| rewards a ring
centred on the right answer over an honest lag. Only a fixed-point ring test
caught it, and at seven minutes a run there was no way to repeat it enough to
separate signal from scatter.

WHAT IS REAL HERE AND WHAT IS NOT
---------------------------------
Real, imported rather than reimplemented:
  * ``calculate_all_load_targets`` - the whole allocator
  * ``apply_smoothing``            - EMA, Schmitt trigger, adaptive rate limit
  * ``readers._smooth``            - the input filter, on the same keys
  * ``excess_margin``              - the verdict, with its hysteresis latch
  * ``grid_without_managed_draws`` - the feedback subtraction

Modelled, mirroring ``dev/ha-test`` rather than Home Assistant:
  * the site's physics, including inverter curtailment to an export limit
  * the CT measurement delay
  * the device's own ramp toward its setpoint

VALIDATED, not assumed. On first run (2026-09-08) the PERMIT_TAU_S sweep
reproduced the rig's own conclusion from that day: a sustained ~1 kW ring at
every tau up to 4 s, decaying at 5.6 s, fully settled at 8 s. The rig reached
that over 40 minutes of wall clock and two noisy columns; this reaches it in
0.8 s. It also reproduces the trap - mean tracking error is BEST in the
oscillating region (264 W at tau 1.0 against 289 W at 5.6), while curtailed
energy, which is the quantity that actually costs anything, is best where the
ring is zero. Trust the ORDERING here, not the absolute figures: the moving
error runs about half the rig's, because the omissions below all cost real
watts.

NOT modelled at all, and this is the limit of the tool: everything on the Home
Assistant path. The per-load ``update_frequency`` gate, the charge pause, entity
restore across restarts, template staleness, the grace window. Every one of
those has produced a real bug in this project, and none of them would show up
here. Screen candidates with this; confirm the winner on the rig.

    python3 dev/tests/dynamics.py                 # the standard comparison
    python3 dev/tests/dynamics.py --plot out.html # and a chart to eyeball
"""

import math
import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from standalone_loader import load_pure_modules

# "hub_calculation" pulls the whole engine chain, which is what makes
# readers importable - it reaches forecast_reader, which needs the
# calculations package __init__ executed. Same combination the
# availability-contract tests use.
load_pure_modules(
    engine_modules=("hub_calculation",), control_modules=("smoothing",)
)

from custom_components.dynamic_ocpp_evse.calculations.models import (  # noqa: E402
    LoadContext,
    PhaseValues,
    SiteContext,
)
from custom_components.dynamic_ocpp_evse.calculations.target_calculator import (  # noqa: E402
    calculate_all_load_targets,
    excess_margin,
)
from custom_components.dynamic_ocpp_evse.calculations.utils import (  # noqa: E402
    grid_without_managed_draws,
)
from custom_components.dynamic_ocpp_evse.const import (  # noqa: E402
    CONF_SITE_UPDATE_FREQUENCY,
    STATION_CHARGE_POWER_STEP,
)
from custom_components.dynamic_ocpp_evse.control.smoothing import (  # noqa: E402
    apply_smoothing,
)
from custom_components.dynamic_ocpp_evse.engine.readers import (  # noqa: E402
    _smooth,
    set_ema_interval,
)

V = 230.0


class Sim:
    """One site, stepped a cycle at a time.

    Defaults mirror ``dev/ha-test/moving_surplus.py`` exactly, so a number out
    of here is comparable in shape to one off the rig. Changing a default here
    without changing it there silently ends that.
    """

    def __init__(
        self,
        *,
        site_freq=1.0,
        ct_lag_s=7.0,
        device_ramp=1.0,
        household_phase_w=300.0,
        export_limit_w=11000.0,
        trigger_margin_w=500.0,
        hysteresis_w=0.0,
        tank_w=2000.0,
        station_min_w=200.0,
        station_max_w=2400.0,
        curtail=True,
    ):
        self.dt = float(site_freq)
        self.device_ramp = device_ramp
        self.hh_w = household_phase_w
        self.export_limit_w = export_limit_w
        self.threshold_w = export_limit_w - trigger_margin_w
        self.hysteresis_w = hysteresis_w
        self.tank_w = tank_w
        self.station_min_w = station_min_w
        self.station_max_w = station_max_w
        self.curtail = curtail

        # The measurement delay, as whole cycles. A real CT chain is a filter
        # rather than a pure delay, but the readers' EMA downstream supplies
        # the filtering; what this stands in for is the transport lag, which is
        # what actually costs the loop its phase margin.
        self.lag = deque([(0.0, 0.0, 0.0)] * max(1, round(ct_lag_s / self.dt)))

        self.ema = {}
        set_ema_interval(self.ema, self.dt)
        self.excess_on = False
        self.station_draw_w = 0.0
        self.commanded_w = 0.0

        # apply_smoothing keeps its state on the sensor entity. It only ever
        # touches these five attributes, so a namespace is the whole contract.
        self.sensor = SimpleNamespace(
            _attr_name="station",
            _ema_current=None,
            _schmitt_current=None,
            _schmitt_state="rising",
            _rate_limited_current=0.0,
        )
        self.hub_entry = SimpleNamespace(
            options={CONF_SITE_UPDATE_FREQUENCY: self.dt}, data={}
        )

    # -- loads ------------------------------------------------------------
    def _loads(self):
        tank_a = self.tank_w / V
        return [
            LoadContext(
                load_id="tank",
                entity_id="tank",
                min_current=tank_a,
                max_current=tank_a,
                phases=1,
                priority=1,
                device_type="hot_water_tank",
                operating_mode="Normal",
                mode_behavior="full_power",
                mode_priority=1,
                active_phases_mask="B",
                l1_phase="B",
                l1_current=tank_a,
                rated_current=tank_a,
            ),
            LoadContext(
                load_id="station",
                entity_id="station",
                min_current=self.station_min_w / V,
                max_current=self.station_max_w / V,
                phases=1,
                priority=2,
                device_type="power_station",
                operating_mode="Excess",
                mode_behavior="excess",
                mode_priority=4,
                active_phases_mask="C",
                l1_phase="C",
                l1_current=self.station_draw_w / V,
                connector_status="Charging",
            ),
        ]

    # -- physics ----------------------------------------------------------
    def ideal_station_w(self, solar_w):
        """What the station should be permitted, from first principles."""
        left = solar_w - 3 * self.hh_w - self.tank_w - self.threshold_w
        return 0.0 if left < self.station_min_w else min(left, self.station_max_w)

    def step(self, solar_w):
        managed = (0.0, self.tank_w, self.station_draw_w)
        demand_w = 3 * self.hh_w + sum(managed)

        potential_w = solar_w
        delivered_w = (
            min(potential_w, max(0.0, demand_w + self.export_limit_w))
            if self.curtail
            else potential_w
        )
        curtailed_w = max(0.0, potential_w - delivered_w)
        inv_phase_a = delivered_w / 3.0 / V

        # True per-phase grid position, positive = importing.
        true = tuple(
            (self.hh_w + m) / V - inv_phase_a for m in managed
        )

        self.lag.append(true)
        delayed = self.lag.popleft()
        measured = [
            _smooth(self.ema, f"grid_{i}", g) or 0.0 for i, g in enumerate(delayed)
        ]

        site = SiteContext(
            voltage=V,
            main_breaker_rating=25.0,
            consumption=PhaseValues(*[max(0.0, g) for g in measured]),
            export_current=PhaseValues(*[max(0.0, -g) for g in measured]),
            grid_current=PhaseValues(*measured),
            excess_export_threshold=self.threshold_w,
            battery_soc=None,
            battery_power=None,
            battery_max_charge_power=None,
            battery_max_discharge_power=None,
            loads=self._loads(),
            circuit_groups=[],
        )

        draws = tuple(sum(c.get_site_phase_draw()[i] for c in site.loads) for i in range(3))
        if any(d > 0 for d in draws):
            site.consumption, site.export_current = grid_without_managed_draws(
                site.consumption, site.export_current, draws
            )

        margin = excess_margin(site, self.hysteresis_w if self.excess_on else 0.0)
        self.excess_on = margin >= 0
        site.excess_hysteresis = self.hysteresis_w if self.excess_on else 0.0

        calculate_all_load_targets(site)
        station = next(c for c in site.loads if c.entity_id == "station")

        permit_a = apply_smoothing(
            self.sensor, station.available_current, False, self.hub_entry
        )
        permit_w = permit_a * V

        # The register the engine writes, floored to the step the device
        # accepts - the same rule as resolve_station_charge_speed.
        if permit_w < self.station_min_w:
            self.commanded_w = 0.0
        else:
            speed = int(permit_w // STATION_CHARGE_POWER_STEP) * STATION_CHARGE_POWER_STEP
            self.commanded_w = max(self.station_min_w, min(speed, self.station_max_w))

        # The device closes a fraction of the remaining gap each cycle.
        self.station_draw_w += (self.commanded_w - self.station_draw_w) * self.device_ramp

        export_w = -sum(true) * V
        return {
            "solar": solar_w,
            "ideal": self.ideal_station_w(solar_w),
            "permit": permit_w,
            "reg": self.commanded_w,
            "draw": self.station_draw_w,
            "managed": self.tank_w + self.station_draw_w,
            "export": export_w,
            "curtailed": curtailed_w,
            "margin": margin,
        }


# -- drivers --------------------------------------------------------------
def sine(mean_w=14700.0, amplitude_w=1100.0, period_s=150.0, periods=2, dt=1.0):
    for i in range(int(period_s * periods / dt)):
        yield mean_w + amplitude_w * math.sin(2 * math.pi * (i * dt) / period_s)


def hold(value_w, seconds, dt=1.0):
    for _ in range(int(seconds / dt)):
        yield value_w


def run(sim, driver, warmup_s=120.0, warmup_w=14700.0):
    """Settle the loop, then record. The warmup is discarded, as on the rig."""
    for w in hold(warmup_w, warmup_s, sim.dt):
        sim.step(w)
    rows = []
    for t_i, w in enumerate(driver):
        r = sim.step(w)
        r["t"] = t_i * sim.dt
        rows.append(r)
    return rows


# -- metrics --------------------------------------------------------------
def report(rows, label=""):
    errs = [abs(r["permit"] - r["ideal"]) for r in rows]
    lost = [r["curtailed"] for r in rows]
    exp = [r["export"] for r in rows]
    regs = [r["reg"] for r in rows]
    writes = sum(1 for a, b in zip(regs, regs[1:]) if a != b)
    mins = (rows[-1]["t"] - rows[0]["t"]) / 60.0 or 1.0
    out = {
        "label": label,
        "err_mean": sum(errs) / len(errs),
        "err_max": max(errs),
        "curtailed_mean": sum(lost) / len(lost),
        "curtailed_max": max(lost),
        "export_mean": sum(exp) / len(exp),
        "export_min": min(exp),
        "export_max": max(exp),
        "writes_per_min": writes / mins,
    }
    return out


def ring(rows, window_s=30.0):
    """Peak-to-peak per window, so decaying / sustained / growing is legible."""
    out = []
    t0 = rows[0]["t"]
    span = rows[-1]["t"] - t0
    lo = 0.0
    while lo < span:
        w = [r for r in rows if lo <= r["t"] - t0 < lo + window_s]
        if len(w) >= 3:
            out.append(
                (
                    lo,
                    max(r["reg"] for r in w) - min(r["reg"] for r in w),
                    max(r["export"] for r in w) - min(r["export"] for r in w),
                )
            )
        lo += window_s
    return out


# -- plotting -------------------------------------------------------------
# Deliberately hand-rolled SVG. Nothing under dev/ has a third-party
# dependency - the pure tier's whole premise is that it runs on a machine with
# nothing installed - and a chart is not worth breaking that for. The palette
# and dark ground match Home Assistant's history card so a run here can be held
# up against "The last hour" on the rig dashboard without re-reading the
# colours.
SERIES = [
    ("solar", "#f5c518", "Solar"),
    ("managed", "#e8705a", "Managed load total"),
    ("curtailed", "#4bbf9a", "Curtailed"),
    ("export_neg", "#5a8dee", "Grid net power"),
]


def _svg(runs, width=960, height=380, pad=54):
    """One panel per run, sharing a y scale so they can be compared by eye."""
    vals = []
    for rows in runs.values():
        for r in rows:
            vals += [r["solar"], r["managed"], r["curtailed"], -r["export"]]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    lo, hi = lo - span * 0.08, hi + span * 0.08

    def y(v):
        return pad + (hi - v) / (hi - lo) * (height - 2 * pad)

    out = []
    for name, rows in runs.items():
        t0, t1 = rows[0]["t"], rows[-1]["t"]
        tspan = (t1 - t0) or 1.0

        def x(t):
            return pad + (t - t0) / tspan * (width - 2 * pad)

        grid = []
        for frac in range(5):
            gy = pad + frac * (height - 2 * pad) / 4
            val = hi - frac * (hi - lo) / 4
            grid.append(
                f'<line x1="{pad}" y1="{gy:.1f}" x2="{width - pad}" y2="{gy:.1f}" '
                f'stroke="#3a3f45" stroke-width="1"/>'
                f'<text x="{pad - 8}" y="{gy + 4:.1f}" fill="#9aa0a6" font-size="11" '
                f'text-anchor="end">{val:,.0f}</text>'
            )
        for frac in range(5):
            gx = pad + frac * (width - 2 * pad) / 4
            grid.append(
                f'<line x1="{gx:.1f}" y1="{pad}" x2="{gx:.1f}" y2="{height - pad}" '
                f'stroke="#3a3f45" stroke-width="1"/>'
                f'<text x="{gx:.1f}" y="{height - pad + 18}" fill="#9aa0a6" '
                f'font-size="11" text-anchor="middle">'
                f'{t0 + frac * tspan / 4:.0f}s</text>'
            )

        paths = []
        for key, colour, _ in SERIES:
            pts = " ".join(
                f"{x(r['t']):.1f},{y(-r['export'] if key == 'export_neg' else r[key]):.1f}"
                for r in rows
            )
            paths.append(
                f'<polyline points="{pts}" fill="none" stroke="{colour}" '
                f'stroke-width="1.6" stroke-linejoin="round"/>'
            )

        legend = []
        for i, (_, colour, label) in enumerate(SERIES):
            lx = pad + i * 210
            legend.append(
                f'<circle cx="{lx}" cy="{height - 14}" r="5" fill="{colour}"/>'
                f'<text x="{lx + 11}" y="{height - 10}" fill="#c8cdd2" '
                f'font-size="12">{label}</text>'
            )

        out.append(
            f'<figure style="margin:0 0 26px 0">'
            f'<figcaption style="color:#e8eaed;font:600 15px system-ui;'
            f'margin:0 0 6px 2px">{name}</figcaption>'
            f'<svg width="{width}" height="{height}" '
            f'style="background:#1c1f24;border-radius:12px">'
            f'<rect width="{width}" height="{height}" fill="#1c1f24"/>'
            f'{"".join(grid)}{"".join(paths)}{"".join(legend)}'
            f'<text x="{pad}" y="{pad - 14}" fill="#9aa0a6" font-size="11">W</text>'
            f"</svg></figure>"
        )
    return "".join(out)


def plot(runs, path, summaries=None):
    """Write a self-contained page. Open it in a browser; no server needed."""
    rows_html = ""
    if summaries:
        head = (
            "<tr><th>run</th><th>err mean</th><th>err max</th>"
            "<th>curtailed mean</th><th>export min</th><th>writes/min</th></tr>"
        )
        body = "".join(
            f"<tr><td>{s['label']}</td><td>{s['err_mean']:,.0f}</td>"
            f"<td>{s['err_max']:,.0f}</td><td>{s['curtailed_mean']:,.0f}</td>"
            f"<td>{s['export_min']:,.0f}</td><td>{s['writes_per_min']:.1f}</td></tr>"
            for s in summaries
        )
        rows_html = (
            "<table style='border-collapse:collapse;color:#c8cdd2;"
            "font:13px system-ui;margin-top:8px'>"
            "<style>th,td{border:1px solid #3a3f45;padding:5px 11px;text-align:right}"
            "th:first-child,td:first-child{text-align:left}</style>"
            f"{head}{body}</table>"
        )
    Path(path).write_text(
        "<meta charset='utf-8'><title>Loop dynamics</title>"
        "<body style='background:#111418;margin:0;padding:22px'>"
        f"{_svg(runs)}{rows_html}</body>",
        encoding="utf-8",
    )
    return path


# -- entry point ----------------------------------------------------------
def _configs():
    """The three the rig measured on 2026-09-08, so the harness can be checked
    against numbers that came off real hardware before it is trusted."""
    from custom_components.dynamic_ocpp_evse import const

    return [
        ("tau 5.6, ramp 50", dict(device_ramp=0.5)),
        ("tau 5.6, ramp 100", dict(device_ramp=1.0)),
        ("10 s refresh", dict(site_freq=10.0, device_ramp=1.0)),
    ]


def sweep_permit_tau(values=(1.0, 1.5, 2.0, 3.0, 4.0, 5.6, 8.0), **kw):
    """Where does the permit filter stop damping the loop?

    A single run cannot answer this. On the rig each point costs seven minutes
    and the window-to-window scatter is as large as the effect, so the
    2026-09-08 A/B of 5.6 against 2.0 came down to reading two noisy columns.
    Here the whole curve costs a second.

    ``smoothing`` binds the constant at import (``from ..const import ...``),
    so the sweep rebinds it on that module rather than on ``const`` - patching
    const would change nothing already imported.
    """
    from custom_components.dynamic_ocpp_evse.control import smoothing

    original = smoothing.PERMIT_TAU_S
    out = []
    try:
        for tau in values:
            smoothing.PERMIT_TAU_S = tau
            sim = Sim(**kw)
            flat = run(sim, hold(14700.0, 210.0, sim.dt), warmup_s=120.0)
            pp = ring(flat)
            sim2 = Sim(**kw)
            moving = run(sim2, sine(dt=sim2.dt), warmup_s=120.0)
            r = report(moving, f"tau {tau}")
            # The last window says whether it settles; the mean over the second
            # half says whether it is still hunting.
            late = [w[1] for w in pp[len(pp) // 2:]]
            out.append(
                {
                    "tau": tau,
                    "ring_last": pp[-1][1],
                    "ring_late_mean": sum(late) / len(late),
                    "err_mean": r["err_mean"],
                    "curtailed_mean": r["curtailed_mean"],
                    "writes_per_min": r["writes_per_min"],
                }
            )
    finally:
        smoothing.PERMIT_TAU_S = original
    return out
def main(argv):
    plot_path = None
    if "--plot" in argv:
        plot_path = argv[argv.index("--plot") + 1]

    runs, summaries = {}, []
    print("MOVING SURPLUS - solar 14.7 kW +/- 1.1 kW over 2 x 150 s\n")
    for label, kw in _configs():
        sim = Sim(**kw)
        rows = run(sim, sine(dt=sim.dt), warmup_s=120.0)
        s = report(rows, label)
        summaries.append(s)
        runs[f"moving surplus - {label}"] = rows
        print(
            f"  {label:<22} err {s['err_mean']:>5,.0f}/{s['err_max']:>5,.0f} W  "
            f"curtailed {s['curtailed_mean']:>4,.0f}/{s['curtailed_max']:>4,.0f} W  "
            f"export min {s['export_min']:>7,.0f} W  "
            f"writes {s['writes_per_min']:>4.1f}/min"
        )

    print("\nFIXED POINT - solar held flat, peak-to-peak per 30 s window\n")
    for label, kw in _configs():
        sim = Sim(**kw)
        rows = run(sim, hold(14700.0, 210.0, kw.get("site_freq", 1.0)),
                   warmup_s=120.0)
        runs[f"fixed point - {label}"] = rows
        pp = ring(rows)
        print(f"  {label}")
        for lo, reg_pp, exp_pp in pp:
            print(f"      {lo:>4.0f}-{lo + 30:<4.0f}  reg {reg_pp:>6,.0f} W   "
                  f"export {exp_pp:>6,.0f} W")

    print("\nPERMIT_TAU_S SWEEP - ring in the second half vs tracking\n")
    print(f"  {'tau':>5} {'ring late':>10} {'ring last':>10} "
          f"{'err mean':>9} {'curtailed':>10} {'writes/min':>11}")
    for row in sweep_permit_tau():
        print(f"  {row['tau']:>5.1f} {row['ring_late_mean']:>10,.0f} "
              f"{row['ring_last']:>10,.0f} {row['err_mean']:>9,.0f} "
              f"{row['curtailed_mean']:>10,.0f} {row['writes_per_min']:>11.1f}")

    if plot_path:
        plot(runs, plot_path, summaries)
        print(f"\nchart written to {plot_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
