# JUG-30 Variable Load device type

Agreed with the owner on 2026-10-21 (chat). Source: GitHub issue 21 (an Alfen charger on Modbus, a
second socket on the same fuse, phase switching, Node-RED balancer to replace). The design below is
settled; details not in it are the implementer's to propose (see "Open implementation points").

## What it is

A load Load Juggler cannot talk to by OCPP, a plug or a climate entity: anything that takes a current or
power setpoint through an entity (a Modbus charger, a heater, a battery or inverter charge setpoint). The
site allocation treats it like any other modulating load, so the main fuse, circuit groups (the
reporter's "CS fuse") and the power budget apply with no new code. It also publishes its allowance as
sensors, so a user can build their own logic on top.

## Settings

- Setpoint entity (a `number` / `input_number`): optional. **No setpoint entity = output-only mode**: the
  device only publishes its numbers and writes nothing (the reporter's stated minimum).
- Setpoint unit: amps or watts (watts needs the phase voltage and the legs).
- Minimum and maximum current (or power), phase legs (the EVSE's phase-count legs, including a
  single-phase device on any one phase), priority.
- Readings, all optional: a current sensor per leg, or a power sensor (power only is enough); "connected"
  and "charging" binary sensors that gate the load (not connected = no demand).
- Update frequency (minimum time between setpoint writes) and a re-send interval (a device that goes to a
  safe mode when its setpoint is not refreshed). A fixed bias (the device delivers less than it is told).

## Modes

The EVSE's four: Standard, Solar Priority, Solar Only, Excess, plus a Hold that sets the setpoint to 0
(minimum when the device cannot take 0). Same switches as the other loads (catch-up, pacing, early
start) where they make sense for a modulating load; the implementer says which.

## Outputs (sensors, per device)

- Allowed current per leg, and the lowest across the legs in use (a leg is in use when the device draws
  more than 1 A on it; with no current sensors, the legs it is configured on).
- Allowed power, and the percentage of maximum.
- **Phase advice** (version 1, advice only, nothing is ever written to the device): `single` or `multi`
  with the reason, from the allowed power with configurable thresholds (defaults: above 4000 W multi,
  below 2000 W single), a hysteresis between them and a minimum time between changes (default 10
  minutes). The device's own phase switch stays the user's (an automation on the advice).

## Not in version 1

Writing a phase switch to a device. Custom EVSE as a separate type: this type with the optional
connected / charging entities covers it.

## Testing

Unit and closed-loop tests like the other loads (the rig in `dev/tests/closed_loop.py`). Live: the owner
suggests the third site (Andrej's); only when there is a dev version and only on the owner's word. The
reporter offered to test; **no reply on the issue until a dev version is ready** (owner, 2026-10-21).

## Open implementation points

Which existing load path to reuse (power station or EVSE), how the config flow asks for the readings
(one page, optional fields), entity ids and translations (English and Slovenian, the owner reviews every
Slovenian string), docs (README, RELEASE_NOTES, `docs/design-decisions.md` for what looks like a bug).

Settled (built 2026-10-21; the owner's decisions on the first build):

- **Reused path:** the EVSE allocation (one current per leg, the modulating load tuple in
  `target_calculator.py`) and the shared load processor (smoothing, cut-now, update-frequency gate);
  own builder (`_build_variable_load`), own control module, one config page with optional fields.
- **Single-phase mode (built):** with a current sensor per leg it is sized on the legs drawing over 1 A,
  remembered while it draws nothing until the connected sensor goes off. With only a power sensor, or no
  reading, it is sized on all its configured legs. Legs not in use publish 0. W setpoints and W bounds
  spread over the legs it is sized on.
- **Switching back to more legs:** on the reading that shows the new legs the allowance restarts at the
  minimum and goes out at once (no command slot, no shortage confirmation): 0 where a phase has no room
  for the minimum, otherwise it climbs through the normal ramp. Fuse and circuit-group limits apply per
  leg as always. The cycle or two before the reading shows the switch is not preventable from here.
- **Phase advice while on fewer legs:** reads the per-leg current all its configured legs could be given
  (physical pool and mode source limit on the configured mask, the pools left by the loads ranked ahead,
  `target_calculator._set_potential_current`, published as `load_potential`) x legs x voltage, capped at
  its maximum; on all its legs it reads the allowed power as before. Circuit groups are left out of it.
- **Stale grid CTs:** the EVSE path in `_apply_grid_stale_fallback`: a running device (drawing over
  1 A) keeps its minimum, an idle one gets 0.
- **Fallback setpoint (the equivalent of the EVSE's default profile):** the EVSE's stale-data fallback
  is the OCPP TxDefaultProfile with its timeout, which the charger itself applies; an entity setpoint
  has nothing like it. The closest equivalents: the device's own watchdog fed by the re-send interval
  (already there), and an optional fallback setpoint written when the entry unloads (reload, remove,
  disable). Unset (default) writes nothing, as before. It is not written on stale data (the EVSE path
  applies) nor on an HA shutdown (HA does not unload entries then).
- Re-send interval default 0 (off), cut speed as the shared processor, unknown live readings accepted,
  Load Insights seeing the new type is fine. Slovenian strings unreviewed until the owner's pass.
