"""Per-device command modules (OCPP, smart plug, hot water tank) and
load-update logic (compliance, smoothing, status)."""

from datetime import datetime, timezone


def stamp_command(sensor, now_mono):
    """The load's command went out (or was settled) this cycle: stamp it on
    the wall clock its last_update attribute reports and on the monotonic one
    the command-interval gate measures from."""
    sensor._last_update = datetime.now(timezone.utc)
    sensor._last_command_time = now_mono
