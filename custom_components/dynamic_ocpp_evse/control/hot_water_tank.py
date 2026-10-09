"""Hot water tank control - setpoint resolution and thermostat commands.

The thermostat - a ``climate`` or a ``water_heater`` entity - owns all
temperature regulation (hysteresis, min cycle, sensor). Load Juggler only gates
power (on/off) and writes the setpoint chosen by the tank's operating mode.
"""

import logging
import math

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.translation import async_get_translations

from ..const import (
    CONF_BINARY_MIN_OFF_TIME,
    CONF_CLIMATE_ENTITY_ID,
    CONF_ENTITY_ID,
    CONF_NAME,
    DOMAIN,
    CONF_SOLAR_GRACE_PERIOD,
    DEFAULT_BINARY_MIN_OFF_TIME,
    DEFAULT_SOLAR_GRACE_PERIOD,
    CONF_HEATING_ELEMENT_POWER,
    DEFAULT_HEATING_ELEMENT_POWER,
    CONF_TANK_AWAY_TEMPERATURE,
    CONF_TANK_NORMAL_TEMPERATURE,
    CONF_TANK_BOOST_TEMPERATURE,
    CONF_TANK_OFF_OPERATION_MODE,
    DEFAULT_TANK_AWAY_TEMPERATURE,
    DEFAULT_TANK_NORMAL_TEMPERATURE,
    DEFAULT_TANK_BOOST_TEMPERATURE,
    TANK_MODE_FREEZE_PROTECTION,
    TANK_MODE_SOLAR_PRIORITY,
    DEFAULT_OPERATING_MODE_HOT_WATER_TANK,
)
from ..helpers import get_entry_value
from . import stamp_command

_LOGGER = logging.getLogger(__name__)


def resolve_tank_setpoint(
    mode: str,
    away: float,
    normal: float,
    boost: float,
    element_power: float,
    hub_data: dict,
) -> tuple[float, str]:
    """Return (setpoint_temperature, label) for the tank's operating mode.

    Pure function - unit-testable. ``label`` is "away" / "normal" / "boost".

    - Below the battery's minimum SOC every mode holds away: the battery then
      carries the tanks for that alone, down to the hub's freeze floor
      (target_calculator._freeze_reserve) - decided before any surplus, as
      it is about the house's own reserve.
    - Freeze Protection: the away setpoint, raised to boost when the hub reports
      excess - the site can't absorb its own production anywhere else - or the
      battery is over its target SOC (ride free energy whenever it's available).
    - Solar Priority: boost at/above the target SOC **or** on the same excess verdict the other two read, and
      normal in between. The battery keeps its priority up to target only
      while there is something to give it - excess means the pack is already
      taking all it is permitted to take.
    - Normal: normal setpoint, raised to boost on the same surplus test as
      Freeze Protection.
    """
    soc = hub_data.get("battery_soc")
    soc_min = hub_data.get("battery_soc_min")
    soc_target = hub_data.get("battery_soc_target")
    export = hub_data.get("total_export_power") or 0

    # "There is real surplus" is decided once, by the hub's excess gate
    # (calculations.excess_margin + its hysteresis latch), and every
    # Excess-mode load reads that same verdict. Fall back to comparing export
    # against the element's own draw only if the hub published no verdict -
    # a stale hub_data shouldn't strand the tank at its floor forever.
    excess_available = hub_data.get("excess_available")
    if excess_available is None:
        excess_available = export > element_power

    # Free energy is available on that verdict, or once the battery has charged
    # past its target SOC. Both Freeze Protection and Normal ride this surplus
    # up to the boost setpoint.
    if soc is not None and soc_min is not None and soc < soc_min:
        return away, "away"

    over_target = soc is not None and soc_target is not None and soc > soc_target
    surplus_available = over_target or excess_available

    if mode == TANK_MODE_FREEZE_PROTECTION.key:
        return (boost, "boost") if surplus_available else (away, "away")

    if mode == TANK_MODE_SOLAR_PRIORITY.key:
        # At or over the target SOC, OR the hub says the site cannot place its
        # own production anywhere else.
        #
        # The excess half was missing (Anze, 2026-09-09, kozolec): this branch
        # read SOC alone, so a tank sat at its normal setpoint while the hub
        # published excess_available - and excess means the battery is already
        # taking every watt it is PERMITTED to take, so "the battery has
        # priority until target" has nothing left to protect. The surplus was
        # going into the pack above its own allowance instead of into hot
        # water. Live at the time: SOC 78 % against an 87 % target with the
        # pack pulling 3 332 W against a 3 000 W allowance, so 332 W was
        # placed nowhere the site had chosen to put it.
        #
        at_or_over_target = (
            soc is not None and soc_target is not None and soc >= soc_target
        )
        if at_or_over_target or excess_available:
            return boost, "boost"
        return normal, "normal"

    # Normal mode (and any unrecognized mode).
    return (boost, "boost") if surplus_available else (normal, "normal")


def hold_tank_label(
    wanted, held, since, dip_since, now, min_switch_s, grace_s, protective=False
):
    """Hold the setpoint label against a verdict that flips.

    Returns ``(label, since, dip_since)``: the label to write, when it was
    last changed, and when a drop out of boost began (None when none is under
    way). The caller keeps the last two between cycles.

    The setpoint used to follow the hub's Excess verdict cycle by cycle, and a
    verdict at its edge flipped the thermostat 75 -> 42 -> 75 C on nearly every
    cycle, the element's relay with it (Kozolec, 3 Oct 2026: 113 setpoint
    changes in a day, the relay switching every 5-20 s). It now holds the way a
    binary load's permit does (entities/load.py):

    - any change waits until the label has held ``min_switch_s`` - the tank's
      minimum off time, its "how often may this switch" setting;
    - leaving boost also waits until the drop has lasted ``grace_s`` - the solar
      grace period every solar load rides a dip through; the verdict coming
      back inside it starts the wait over;
    - a ``protective`` change - the battery's minimum SOC - acts at once,
      exactly as the SOC floor cuts through every grace hold.

    Pure function - unit-testable.
    """
    if held is None or wanted == held:
        return wanted, now if held is None else since, None
    if protective:
        return wanted, now, None
    if held == "boost":
        dip_since = now if dip_since is None else dip_since
        if now - dip_since < grace_s:
            return held, since, dip_since
    if since is not None and now - since < min_switch_s:
        return held, since, dip_since
    return wanted, now, None


# A target the device does not keep: the readbacks that may say so.
KEPT_SETTLE_S = 300.0
KEPT_READBACKS = 3
# How many other asks keep their count - away, normal, boost and a kept one.
KEPT_ASKS = 4


def judge_kept_target(rec, asked, readback, stamp, now,
                      settle_s=KEPT_SETTLE_S, needed=KEPT_READBACKS):
    """Whether the device keeps a lower target than the one asked of it.

    Returns ``(rec, kept)``: the record of this ask the caller keeps between
    cycles (``None`` starts one), and the device's own value once it has
    answered ``asked`` with the same lower one on ``needed`` separate
    readbacks, else None.

    A MELCloud heat pump asked for 60 C on boost read back 55 at each of its
    15-minute cloud polls and kept heating to 55 (a user's site, 2-3 Oct 2026):
    our write echoes back at once, the unit's own ceiling only with the next
    poll. So a readback counts only when

    - it is lower than asked - a higher one is the device's or the user's own
      choice, not a ceiling;
    - the ask has stood ``settle_s`` - our write's echo, or a cloud poll not yet
      through, reads anything;
    - it is a new readback (``stamp``: the state's ``last_reported``), and
      ``settle_s`` after the last one counted - a frozen state, or one cloud
      poll seen on two cycles, is one answer;
    - it is the same value as the ones before it - a lag shows the previous
      ask once, a ceiling the same value every time.

    A readback of ``asked`` itself resets nothing: a cloud device echoes our
    write until its next poll whatever the unit does with it. Each ask keeps
    its own count (``rec["others"]`` holds the asks not in force), so a label
    flipping boost -> normal -> boost on a passing cloud finds its readbacks
    where it left them rather than starting the three polls (45 min) over;
    its settle time does start again - our write is new.

    Pure function - unit-testable.
    """
    if rec is None or rec["asked"] != asked:
        others = rec.pop("others", {}) if rec is not None else {}
        if rec is not None:
            others[rec["asked"]] = rec
        rec = others.pop(asked, None) or {"asked": asked, "kept": None, "seen": 0,
                                           "stamp": None, "at": None}
        rec["since"] = now
        rec["others"] = dict(list(others.items())[-KEPT_ASKS:])
    if (
        readback is None
        or readback > asked - 0.05
        or now - rec["since"] < settle_s
        or stamp is None
        or stamp == rec["stamp"]
        or (rec["at"] is not None and now - rec["at"] < settle_s)
    ):
        return rec, None
    if rec["kept"] is None or abs(readback - rec["kept"]) > 0.05:
        rec.update(kept=readback, seen=0)
    rec.update(seen=rec["seen"] + 1, stamp=stamp, at=now)
    return rec, rec["kept"] if rec["seen"] >= needed else None


async def send_hot_water_tank_command(
    sensor, limit: float, hub_data: dict, now_mono: float
) -> None:
    """Drive a hot water tank's thermostat: gate heating and set the target.

    ``limit`` is the engine's allocated current after smoothing - > 0 means the
    engine found power for the tank, so heating is permitted.
    """
    climate_entity = sensor.config_entry.data.get(CONF_CLIMATE_ENTITY_ID)
    if not climate_entity:
        _LOGGER.error(
            "No climate entity configured for hot water tank %s", sensor._attr_name
        )
        return

    load_rt = sensor._runtime()
    mode = load_rt.get(
        "operating_mode", DEFAULT_OPERATING_MODE_HOT_WATER_TANK.key
    )
    away = load_rt.get("tank_away_temperature") or get_entry_value(
        sensor.config_entry,
        CONF_TANK_AWAY_TEMPERATURE,
        DEFAULT_TANK_AWAY_TEMPERATURE,
    )
    normal = load_rt.get("tank_normal_temperature") or get_entry_value(
        sensor.config_entry,
        CONF_TANK_NORMAL_TEMPERATURE,
        DEFAULT_TANK_NORMAL_TEMPERATURE,
    )
    boost = load_rt.get("tank_boost_temperature") or get_entry_value(
        sensor.config_entry,
        CONF_TANK_BOOST_TEMPERATURE,
        DEFAULT_TANK_BOOST_TEMPERATURE,
    )
    element_power = get_entry_value(
        sensor.config_entry,
        CONF_HEATING_ELEMENT_POWER,
        DEFAULT_HEATING_ELEMENT_POWER,
    )

    _, wanted = resolve_tank_setpoint(
        mode, away, normal, boost, element_power, hub_data
    )
    # Held against a flipping verdict (hold_tank_label). A mode the user has
    # just picked starts afresh: choosing a mode is an explicit instruction,
    # as it is for the permit's own dwell.
    entry = sensor.config_entry
    label, load_rt["_tank_label_since"], load_rt["_tank_boost_dip_since"] = (
        hold_tank_label(
            wanted,
            load_rt.get("tank_setpoint_label")
            if load_rt.get("_tank_label_mode") == mode
            else None,
            load_rt.get("_tank_label_since"),
            load_rt.get("_tank_boost_dip_since"),
            now_mono,
            60 * float(get_entry_value(
                entry, CONF_BINARY_MIN_OFF_TIME, DEFAULT_BINARY_MIN_OFF_TIME
            ) or 0),
            60 * float(get_entry_value(
                entry, CONF_SOLAR_GRACE_PERIOD, DEFAULT_SOLAR_GRACE_PERIOD
            ) or 0),
            # Away outside Freeze Protection is the battery's minimum SOC.
            protective=mode != TANK_MODE_FREEZE_PROTECTION.key and wanted == "away",
        )
    )
    load_rt["_tank_label_mode"] = mode
    settings = {"away": away, "normal": normal, "boost": boost}
    heating_permitted = limit > 0
    climate_state = sensor.hass.states.get(climate_entity)

    # Denied power, a tank already at its floor - away in Freeze Protection
    # and below the battery's minimum SOC, normal otherwise - waits AT that
    # floor rather than off: it draws nothing there either way, and off is
    # kept for a tank that would heat. A boost ending went through off or the
    # lowest target first, because the label rides the dip at boost while the
    # allocator sizes a boosting tank by the surplus alone and withdraws its
    # power at once, and the minimum off time then held it there (Andrej's
    # site, 4 Oct 2026: water_heater.tc 46 -> 40 at 13:49, 40 -> 42 at 13:54,
    # and 46 -> 40 -> 46 -> 40 through 11:21-12:13; Home's workshop boiler
    # 80 heat -> off -> 80 heat -> off, its normal 42 never shown). Off for
    # the minimum off time since e7a4228 (7 Sep), for the grace too once
    # 5de0d45 (3 Oct) held the label at boost through it. And not while the
    # device, already set to the floor, says it heats - its own word, power
    # sensor or hvac_action, as the load builder reads them (tank_hvac_action):
    # a tank cooled past its thermostat's hysteresis heats at the floor target
    # while its temperature, a MELCloud poll late, still reads at the floor;
    # denied, it goes off. Set higher (a boost being withdrawn), its heating
    # is for that target, and lowering it to the floor ends it.
    target, at_floor = label, False
    if not heating_permitted:
        floor = (
            "away"
            if mode == TANK_MODE_FREEZE_PROTECTION.key or label == "away"
            else "normal"
        )
        try:
            at_floor = (
                float(climate_state.attributes["current_temperature"])
                >= settings[floor]
                and not (
                    load_rt.get("tank_hvac_action") == "heating"
                    and float(climate_state.attributes["temperature"])
                    <= settings[floor] + 0.05
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
        if at_floor:
            target = floor
    setpoint = settings[target]

    # Clamp to the climate entity's own limits. HA hard-rejects an
    # out-of-range set_temperature, and with blocking=False that rejection is
    # invisible here - the thermostat would silently keep its previous target
    # (e.g. a 90 °C boost against a 75 °C max_temp leaves it at the away
    # setpoint and the tank never heats). Warn once per offending setpoint.
    if climate_state is not None:
        clamped = setpoint
        max_temp = climate_state.attributes.get("max_temp")
        min_temp = climate_state.attributes.get("min_temp")
        try:
            if max_temp is not None:
                clamped = min(clamped, float(max_temp))
            if min_temp is not None:
                clamped = max(clamped, float(min_temp))
        except (TypeError, ValueError):
            clamped = setpoint
        if clamped != setpoint:
            if load_rt.get("_tank_clamp_warned_for") != setpoint:
                load_rt["_tank_clamp_warned_for"] = setpoint
                _LOGGER.warning(
                    "Hot water tank %s: %s setpoint %.0f°C is outside %s's "
                    "supported range (%s–%s°C) - clamped to %.0f°C. Adjust the "
                    "setpoint slider or the thermostat's limits.",
                    sensor._attr_name,
                    target,
                    setpoint,
                    climate_entity,
                    min_temp,
                    max_temp,
                    clamped,
                )
            setpoint = clamped
        else:
            load_rt.pop("_tank_clamp_warned_for", None)

    # A target the device does not keep (judge_kept_target): asking it again
    # each poll is a cloud write for nothing, so the setting comes down to what
    # the device holds. Judged only while heating is permitted - a denied
    # tank waits at its floor or is held off, neither of them an ask.
    if heating_permitted and climate_state is not None:
        try:
            readback = float(climate_state.attributes["temperature"])
            # A target below the device's own minimum is none it can be set to.
            if readback < float(climate_state.attributes.get("min_temp") or 0):
                readback = None
        except (KeyError, TypeError, ValueError):
            readback = None
        load_rt["_tank_kept"], kept = judge_kept_target(
            load_rt.get("_tank_kept"),
            setpoint,
            readback,
            getattr(climate_state, "last_reported", None),
            now_mono,
        )
        if kept is not None:
            setpoint = await _adopt_kept_target(
                sensor,
                settings,
                label,
                setpoint,
                kept,
                climate_entity,
            )
    else:
        load_rt.pop("_tank_kept", None)

    # Publish state for the tank status sensor.
    if load_rt is not None:
        load_rt["tank_setpoint"] = setpoint
        load_rt["tank_setpoint_label"] = label
        load_rt["tank_heating_permitted"] = heating_permitted

    _LOGGER.debug(
        "Hot water tank %s [%s]: setpoint=%.0f°C (%s), heating %s",
        sensor._attr_name,
        mode,
        setpoint,
        target,
        "permitted" if heating_permitted else "forbidden",
    )

    # The integration is the master controller - re-assert each command cycle.
    try:
        if climate_entity.startswith("water_heater."):
            await _command_water_heater(
                sensor.hass,
                climate_entity,
                climate_state,
                heating_permitted or at_floor,
                setpoint,
                load_rt,
                get_entry_value(entry, CONF_TANK_OFF_OPERATION_MODE, "") or "",
            )
        elif heating_permitted or at_floor:
            await sensor.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": climate_entity, "temperature": setpoint},
                blocking=False,
            )
            await sensor.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": climate_entity, "hvac_mode": "heat"},
                blocking=False,
            )
        else:
            await sensor.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": climate_entity, "hvac_mode": "off"},
                blocking=False,
            )
    except Exception as e:
        _LOGGER.warning(
            "Hot water tank climate command failed for %s: %s",
            sensor._attr_name,
            e,
        )

    stamp_command(sensor, now_mono)


async def _adopt_kept_target(sensor, settings, label, asked, kept, device):
    """Lower the asked setting to the target the device keeps, and say so.

    ``settings`` are the tank's away / normal / boost temperatures, lowest
    first. The asked one comes down to ``kept`` - and so does any below it that
    sat above ``kept``, so away <= normal <= boost still holds; the ones above
    it are judged when they are asked. In whole degrees, the sliders' step,
    rounded down: rounded up, the slider would ask above the device again.
    Through each slider's own set service, so the slider shows it and keeps it
    across a restart. The notification replaces the tank's previous one.
    Returns the setpoint to write now.
    """
    hass, entry, load_rt = sensor.hass, sensor.config_entry, sensor._runtime()
    value = math.floor(kept + 0.01)
    names = list(settings)
    registry = er.async_get(hass)
    lowered = []
    for name in names[: names.index(label) + 1]:
        if settings[name] <= value:
            continue
        key = f"tank_{name}_temperature"
        load_rt[key] = value
        lowered.append(f"{name.title()} Temperature")
        number = registry.async_get_entity_id(
            "number", DOMAIN, f"{entry.data.get(CONF_ENTITY_ID)}_{key}"
        )
        if number:
            await hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": number, "value": value},
                blocking=False,
            )

    fields = {
        "tank": entry.data.get(CONF_NAME) or sensor._attr_name,
        "device": device,
        "asked": f"{asked:g}",
        "kept": f"{kept:g}",
        "value": value,
        "setting": f"{label.title()} Temperature",
        "lowered": ", ".join(lowered),
    }
    _LOGGER.warning(
        "Hot water tank %(tank)s: %(device)s keeps %(kept)s°C when asked for "
        "%(asked)s°C - %(lowered)s lowered to %(value)s°C",
        fields,
    )
    strings = await async_get_translations(
        hass, hass.config.language, "issues", {DOMAIN}
    )
    text = f"component.{DOMAIN}.issues.tank_target_not_kept."
    await hass.services.async_call(
        "persistent_notification",
        "create",
        {
            "title": strings[text + "title"].format(**fields),
            "message": strings[text + "description"].format(**fields),
            "notification_id": f"{DOMAIN}_tank_target_not_kept_{entry.entry_id}",
        },
    )
    return value


async def _command_water_heater(hass, entity_id, state, permitted, setpoint,
                                load_rt=None, off_mode=""):
    """A water heater is gated by its target temperature: the setpoint while
    the tank may heat or waits at its floor, its lowest target otherwise - or,
    where the tank's settings name the device's own word for off
    (``off_mode``, CONF_TANK_OFF_OPERATION_MODE), that operation mode, and
    back to the one it was in once power returns.

    Never ``turn_off``: what that switches off is the integration's choice, and
    MELCloud's powers down the whole heat pump, space heating included
    (a user's site, 2026-09-25). No operation mode of its own choosing either -
    they are the integration's own words (Vaillant: heating / hot_water_only /
    stand_by; MELCloud: auto / force_hot_water), so only the user can name one
    as "off"; one the device does not list is warned about once and the tank
    held at its lowest target. A target is written only when it differs, where the climate path re-asserts
    every cycle: a water heater is often a cloud device (MELCloud) that
    rate-limits writes.
    """
    attrs = state.attributes if state is not None else {}
    modes = attrs.get("operation_list") or []
    if off_mode and off_mode not in modes:
        if load_rt is not None and load_rt.get("_tank_off_mode_warned") != off_mode:
            load_rt["_tank_off_mode_warned"] = off_mode
            _LOGGER.warning("Water heater %s has no operation mode %r (it has %s) - "
                            "held at its lowest target instead", entity_id, off_mode, modes)
        off_mode = ""
    if off_mode and load_rt is not None:
        current = state.state if state is not None else None
        if not permitted:
            if current != off_mode:
                load_rt["_tank_mode_before_off"] = current
                await hass.services.async_call(
                    "water_heater", "set_operation_mode",
                    {"entity_id": entity_id, "operation_mode": off_mode}, blocking=False)
            return
        if current == off_mode:
            back = load_rt.pop("_tank_mode_before_off", None)
            if back not in modes or back == off_mode:
                back = next((m for m in modes if m != off_mode), None)
            if back is not None:
                await hass.services.async_call(
                    "water_heater", "set_operation_mode",
                    {"entity_id": entity_id, "operation_mode": back}, blocking=False)
    target = setpoint if permitted or off_mode else attrs.get("min_temp")
    if target is None:
        return
    try:
        if abs(float(attrs.get("temperature")) - float(target)) < 0.05:
            return
    except (TypeError, ValueError):
        pass
    await hass.services.async_call(
        "water_heater",
        "set_temperature",
        {"entity_id": entity_id, "temperature": target},
        blocking=False,
    )
