"""The site, as the Energy dashboard already describes it.

Home Assistant's energy preferences are the one place a user has already said
what their site consists of: grid import/export, PV production, battery in/out,
individual metered devices - and, from 2025.12 on, a POWER sensor beside each
energy one (``stat_rate``), from 2026.3 a ``PowerConfig`` for grid and battery
(one signed sensor, an inverted one, or a from/to pair), from 2026.6 the
battery's SOC and capacity. This module turns that dict into a ``SiteModel``
and nothing else: no lookups, no defaults invented, no Home Assistant imports.

Sign convention throughout is the dashboard's: positive = import / discharge,
negative = export / charge. Consumption is
    grid_in - grid_out + solar + battery_out - battery_in
and the unmetered REMAINDER is consumption minus every listed device whose
total is not already inside another listed device (``included_in_stat``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PowerSpec:
    """How a signed power figure is read for a source (grid or battery).

    Exactly the dashboard's ``PowerConfig``: ``rate`` is one signed sensor,
    ``rate_inverted`` the same with the opposite polarity, ``rate_from`` /
    ``rate_to`` a pair (import / export, discharge / charge). A source may
    also carry a bare ``stat_rate`` outside a PowerConfig (2025.12 shape);
    that is read as ``rate``.
    """

    rate: Optional[str] = None
    rate_inverted: Optional[str] = None
    rate_from: Optional[str] = None
    rate_to: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return not any((self.rate, self.rate_inverted, self.rate_from, self.rate_to))

    @property
    def polarity(self) -> Optional[int]:
        """+1 when a positive reading means import, -1 when it means export.

        The dashboard already asks this - none, normal, inverted, or a pair
        of sensors - so it is DECLARED here and should never be guessed at
        (2026-09-18, about a SolarEdge M1 that reports export
        positive). A pair needs no polarity: each sensor names its own
        direction, so it answers +1 for the import half.

        None when the source declares no power reading at all, which is when
        the reading's own behaviour has to settle it instead."""
        if self.rate_inverted:
            return -1
        if self.rate or self.rate_from or self.rate_to:
            return 1
        return None

    @classmethod
    def from_source(cls, src: dict) -> Optional["PowerSpec"]:
        pc = src.get("power_config") or {}
        spec = cls(
            rate=pc.get("stat_rate") or src.get("stat_rate") or None,
            rate_inverted=pc.get("stat_rate_inverted") or None,
            rate_from=pc.get("stat_rate_from") or None,
            rate_to=pc.get("stat_rate_to") or None,
        )
        return None if spec.is_empty else spec


@dataclass(frozen=True)
class Device:
    """One individually metered device from ``device_consumption``."""

    energy: str
    name: Optional[str] = None
    power: Optional[str] = None
    included_in: Optional[str] = None

    @property
    def label(self) -> str:
        return self.name or self.energy


@dataclass(frozen=True)
class SiteModel:
    grid_import: tuple = ()
    grid_export: tuple = ()
    grid_power: tuple = ()
    solar: tuple = ()
    solar_power: tuple = ()
    solar_forecast_entries: tuple = ()
    battery_in: tuple = ()
    battery_out: tuple = ()
    battery_power: tuple = ()
    battery_soc: tuple = ()
    battery_capacity_kwh: Optional[float] = None
    devices: tuple = ()

    # ------------------------------------------------------------------ build
    @classmethod
    def from_prefs(cls, prefs: Optional[dict], ignore: frozenset = frozenset()) -> "SiteModel":
        """Build from ``EnergyManager.data`` (or None when nothing is set up).

        ``ignore`` names devices to leave out: this integration's own
        estimates, which a user may list on the dashboard to see them there,
        but which must never come back in as meters of what they estimate."""
        if not prefs:
            return cls()
        grid_in, grid_out, grid_pw = [], [], []
        solar, solar_pw, forecasts = [], [], []
        batt_in, batt_out, batt_pw, batt_soc = [], [], [], []
        capacity = None

        for src in prefs.get("energy_sources") or []:
            kind = src.get("type")
            if kind == "grid":
                if "flow_from" in src or "flow_to" in src:
                    # Pre-2026 shape: lists of flows, power in a sibling list.
                    grid_in += [f["stat_energy_from"] for f in src.get("flow_from") or [] if f.get("stat_energy_from")]
                    grid_out += [f["stat_energy_to"] for f in src.get("flow_to") or [] if f.get("stat_energy_to")]
                    for p in src.get("power") or []:
                        spec = PowerSpec.from_source(p)
                        if spec:
                            grid_pw.append(spec)
                else:
                    if src.get("stat_energy_from"):
                        grid_in.append(src["stat_energy_from"])
                    if src.get("stat_energy_to"):
                        grid_out.append(src["stat_energy_to"])
                    spec = PowerSpec.from_source(src)
                    if spec:
                        grid_pw.append(spec)
            elif kind == "solar":
                if src.get("stat_energy_from"):
                    solar.append(src["stat_energy_from"])
                if src.get("stat_rate"):
                    solar_pw.append(src["stat_rate"])
                forecasts += list(src.get("config_entry_solar_forecast") or [])
            elif kind == "battery":
                if src.get("stat_energy_to"):
                    batt_in.append(src["stat_energy_to"])
                if src.get("stat_energy_from"):
                    batt_out.append(src["stat_energy_from"])
                spec = PowerSpec.from_source(src)
                if spec:
                    batt_pw.append(spec)
                if src.get("stat_soc"):
                    batt_soc.append(src["stat_soc"])
                if src.get("capacity") is not None:
                    capacity = (capacity or 0.0) + float(src["capacity"])
            # gas / water: not electrical consumption, ignored on purpose

        devices = tuple(
            Device(
                energy=d["stat_consumption"],
                name=d.get("name") or None,
                power=d.get("stat_rate") or None,
                included_in=d.get("included_in_stat") or None,
            )
            for d in prefs.get("device_consumption") or []
            if d.get("stat_consumption") and d["stat_consumption"] not in ignore
        )
        return cls(
            grid_import=tuple(grid_in), grid_export=tuple(grid_out), grid_power=tuple(grid_pw),
            solar=tuple(solar), solar_power=tuple(solar_pw), solar_forecast_entries=tuple(forecasts),
            battery_in=tuple(batt_in), battery_out=tuple(batt_out), battery_power=tuple(batt_pw),
            battery_soc=tuple(batt_soc), battery_capacity_kwh=capacity, devices=devices,
        )

    # ------------------------------------------------------------------ query
    @property
    def has_sources(self) -> bool:
        """Consumption can be formed: at least a grid import figure exists."""
        return bool(self.grid_import)

    def consumption_terms(self) -> tuple:
        """(statistic_id, sign) pairs whose signed sum is site consumption."""
        terms = [(s, 1.0) for s in self.grid_import]
        terms += [(s, -1.0) for s in self.grid_export]
        terms += [(s, 1.0) for s in self.solar]
        terms += [(s, 1.0) for s in self.battery_out]
        terms += [(s, -1.0) for s in self.battery_in]
        return tuple(terms)

    def remainder_devices(self) -> tuple:
        """Devices to subtract for the unmetered remainder: every listed
        device except those whose consumption another LISTED device already
        contains, so a sub-metered heater inside a metered workshop is not
        subtracted twice."""
        return self.parts_of(None)

    def parts_of(self, parent: Optional[str]) -> tuple:
        """The listed devices directly inside ``parent`` (None = the site). A
        device whose ``included_in`` names an unlisted statistic counts as
        top-level."""
        listed = {d.energy for d in self.devices}
        return tuple(d for d in self.devices
                     if (d.included_in if d.included_in in listed else None) == parent)

    def all_statistic_ids(self) -> set:
        ids = {s for s, _ in self.consumption_terms()}
        ids |= {d.energy for d in self.devices}
        return ids

    def summary(self) -> dict:
        """Counts for a config-flow description; names for attributes."""
        return {
            "grid": len(self.grid_import) + len(self.grid_export),
            "solar": len(self.solar),
            "battery": len(self.battery_in) + len(self.battery_out),
            "devices": len(self.devices),
            "forecasts": len(self.solar_forecast_entries),
            "device_names": [d.label for d in self.devices],
        }
