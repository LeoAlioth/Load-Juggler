"""The EVSE send gate: back-off after a failed send, slow while not Charging."""
from custom_components.dynamic_ocpp_evse.const import (
    EVSE_RT_SEND_BACKOFF_UNTIL,
    IDLE_SEND_INTERVAL_S,
    SEND_BACKOFF_S,
)
from custom_components.dynamic_ocpp_evse.control.ocpp import send_slot_open

INF = float("inf")


def test_charging_keeps_the_command_interval():
    rt = {"gate_status": "Charging"}
    assert not send_slot_open(rt, "Charging", 100.0, 15, 110.0)
    assert send_slot_open(rt, "Charging", 100.0, 15, 115.0)


def test_a_settled_idle_status_waits_the_idle_interval():
    for status in ("Available", "SuspendedEV", "Finishing"):
        rt = {"gate_status": status}
        assert not send_slot_open(rt, status, 100.0, 15, 150.0), status
        assert send_slot_open(rt, status, 100.0, 15, 100.0 + IDLE_SEND_INTERVAL_S), status


def test_preparing_and_suspended_by_us_keep_the_command_interval():
    for status in ("Preparing", "SuspendedEVSE"):
        rt = {"gate_status": status}
        assert send_slot_open(rt, status, 100.0, 15, 116.0), status


def test_a_status_change_uses_the_normal_interval():
    rt = {"gate_status": "Preparing"}
    assert send_slot_open(rt, "Charging", 100.0, 15, 116.0)
    rt = {"gate_status": "Available"}
    assert send_slot_open(rt, "Preparing", 100.0, 15, 116.0)


def test_backoff_blocks_everything_then_ends():
    rt = {EVSE_RT_SEND_BACKOFF_UNTIL: 100.0 + SEND_BACKOFF_S, "gate_status": "Charging"}
    assert not send_slot_open(rt, "Charging", -INF, 15, 120.0)
    assert send_slot_open(rt, "Charging", -INF, 15, 100.0 + SEND_BACKOFF_S)
