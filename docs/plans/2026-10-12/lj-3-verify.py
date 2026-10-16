#!/usr/bin/env python3
"""LJ-3: verify the four shipped-but-unverified Load Juggler TODO items from HA history (read-only).

Usage:  python3 lj-3-verify.py [1|2|3|4 ...]      (no args = all)
        END=2026-10-14T06:00:00Z python3 lj-3-verify.py 1   (default end = now)

Prints one verdict per item: PASS / FAIL / REVIEW (look at the listed cases) / NOT EXERCISED / N/A.
Only GETs /api/history/period and /api/error_log. Tokens are read from files, never printed.
Plan: /Users/anzek/Projects/plans/2026-10-12/lj-3-verify-shipped.md
"""
import bisect, datetime as dt, json, os, pathlib, sys, urllib.error, urllib.parse, urllib.request
from zoneinfo import ZoneInfo

HOSTS = {'home': 'ha.alpacasbarn.com', 'kozolec': 'ha.kozolec.hlevcek.com', 'andrejg': 'andrejg.duckdns.org:8123'}
LOCAL = ZoneInfo('Europe/Ljubljana')
UTC = dt.timezone.utc
END = os.environ.get('END') or dt.datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
UNPLUGGED = {'Available', 'Unavailable', 'unavailable', 'unknown', 'Faulted', 'Finishing'}
ASSUMED = 'waiting for a new reading - assumed current'


def _token(site):
    return pathlib.Path('/Users/anzek/Projects/Load_Insights/data/' + HOSTS[site].split(':')[0]).read_text().strip()


def _get(site, path):
    req = urllib.request.Request(f'https://{HOSTS[site]}{path}', headers={'Authorization': 'Bearer ' + _token(site)})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read().decode()


def ts(s):
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))


def fmt(t):
    return f"{t.astimezone(UTC):%m-%d %H:%M:%S}Z ({t.astimezone(LOCAL):%H:%M} local)"


class Series:
    """One entity's history as a step function: at(t) -> (state, attrs) in force at t."""
    def __init__(self, rows):
        self.t = [ts(r.get('last_updated') or r['last_changed']) for r in rows]
        self.s = [r['state'] for r in rows]
        self.a = [r.get('attributes') or {} for r in rows]

    def idx(self, t):
        return bisect.bisect_right(self.t, t) - 1

    def at(self, t):
        i = self.idx(t)
        return (self.s[i], self.a[i]) if i >= 0 else (None, {})

    def num(self, t):
        try:
            return float(self.at(t)[0])
        except (TypeError, ValueError):
            return None

    def between(self, t0, t1):
        i = max(self.idx(t0), 0)
        return [(self.t[j], self.s[j], self.a[j]) for j in range(i, len(self.t)) if self.t[j] <= t1]


def hist(site, ids, start, attrs=(), end=None):
    """{entity_id: Series}. Entities in `attrs` keep their attributes; the rest come minimal."""
    out = {}
    for group, extra in ((list(attrs), '&significant_changes_only=0'),
                         ([i for i in ids if i not in attrs], '&minimal_response&no_attributes')):
        if not group:
            continue
        q = f"end_time={urllib.parse.quote(end or END)}&filter_entity_id={','.join(group)}{extra}"
        for rows in json.loads(_get(site, f'/api/history/period/{start}?{q}')):
            if rows:
                eid = rows[0]['entity_id']
                for r in rows:  # minimal_response drops entity_id after the first row
                    r.setdefault('entity_id', eid)
                out[eid] = Series(rows)
    missing = [i for i in ids if i not in out]
    if missing:
        print('  ! no history for', ', '.join(missing))
    return out


def ticks(t0, t1, step=5):
    t = t0
    while t <= t1:
        yield t
        t += dt.timedelta(seconds=step)


def spans(flags, step=5):
    """[(start, seconds)] of consecutive True ticks in [(t, bool)]."""
    out, start = [], None
    for t, f in flags:
        if f and start is None:
            start = t
        if not f and start is not None:
            out.append((start, (t - start).total_seconds())); start = None
    if start is not None:
        out.append((start, (flags[-1][0] - start).total_seconds() + step))
    return out


def intervals(series, pred, end):
    """[(start, end)] where pred(state) holds."""
    out, start = [], None
    for t, s in zip(series.t, series.s):
        if pred(s) and start is None:
            start = t
        elif not pred(s) and start is not None:
            out.append((start, t)); start = None
    if start is not None:
        out.append((start, end))
    return out


def overlap(a, b):
    return [(max(p[0], q[0]), min(p[1], q[1])) for p in a for q in b if max(p[0], q[0]) < min(p[1], q[1])]


def max_in_window(times, width_s):
    best, j = 0, 0
    for i in range(len(times)):
        while (times[i] - times[j]).total_seconds() > width_s:
            j += 1
        best = max(best, i - j + 1)
    return best


# --------------------------------------------------------------------------------------------- item 1
def item1():
    """Slow meter on Andrej's two go-eChargers (c32bbc7 + 2139595, installed 2026-10-04 ~20:30-21:00 local)."""
    print('\n=== 1. Slow meter: assumed draw on Andrej\'s two go-eChargers')
    start = '2026-10-04T19:00:00Z'
    end = ts(END)
    C = ('charger', 'charger_nova')  # stara, nova
    # the light entities for the whole span; the meters (every few s) only for the both-plugged windows
    ids = [f'sensor.{c}_{k}' for c in C for k in ('status_connector', 'charging_status', 'effective_priority')]
    ids += ['select.site_load_management_distribution_mode']
    H = hist('andrejg', ids, start)
    ids = [f'sensor.{c}_{k}' for c in C for k in ('allocated_current', 'current_import', 'power_active_import')]
    ids += [f'sensor.site_load_management_available_current_{p}' for p in 'abc']
    ids += [f'sensor.solaredge_i2_m1_ac_current_{p}' for p in 'abc'] + [f'sensor.solaredge_i2_m1_ac_power_{p}' for p in 'abc']
    ids += ['sensor.solaredge_i2_m1_ac_power', 'sensor.glajena_povprecna_moc_do_konca_cb']
    g = lambda e: H.get(e)
    plugged = {c: intervals(g(f'sensor.{c}_status_connector'), lambda s: s not in UNPLUGGED, end) for c in C}
    both = overlap(plugged['charger'], plugged['charger_nova'])
    mode = g('select.site_load_management_distribution_mode')
    print(f'  both plugged: {len(both)} window(s)')
    verdicts = []

    def raw_room(t):
        """Room for the two chargers per phase from the meters (estimate - assumes each charger
        draws its current_import on all three phases; ponytail: per-phase charger currents if the
        ocpp integration ever exposes them)."""
        chg_a = sum(g(f'sensor.{c}_current_import').num(t) or 0 for c in C)
        chg_w = sum((g(f'sensor.{c}_power_active_import').num(t) or 0) * 1000 for c in C)
        rooms = []
        for p in 'abc':
            i, w = g(f'sensor.solaredge_i2_m1_ac_current_{p}').num(t), g(f'sensor.solaredge_i2_m1_ac_power_{p}').num(t)
            if i is None or w is None:
                return None
            rooms.append(20 - abs(i) * (1 if w <= 0 else -1) + chg_a)   # M1 power is export-positive
        allow, grid = g('sensor.glajena_povprecna_moc_do_konca_cb').num(t), g('sensor.solaredge_i2_m1_ac_power').num(t)
        if allow is not None and grid is not None:
            rooms.append((allow + grid + chg_w) / 690)   # import = -grid
        return min(rooms)

    def grid_max(t):
        vals = []
        for p in 'abc':
            i, w = g(f'sensor.solaredge_i2_m1_ac_current_{p}').num(t), g(f'sensor.solaredge_i2_m1_ac_power_{p}').num(t)
            if i is not None and w is not None:
                vals.append(abs(i) * (1 if w <= 0 else -1))
        return max(vals) if vals else None

    iso = lambda t: t.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
    for w0, w1 in both:
        H.update(hist('andrejg', ids, iso(w0 - dt.timedelta(minutes=5)), end=iso(w1)))
        m = mode.at(w0)[0]
        prio = {c: g(f'sensor.{c}_effective_priority').at(w0)[0] for c in C}
        first = min(C, key=lambda c: float(prio[c]) if prio[c] not in (None, 'unknown', 'unavailable') else 9)
        second = C[1] if first == C[0] else C[0]
        print(f'  -- {fmt(w0)} -> {fmt(w1)} ({(w1 - w0).total_seconds() / 3600:.1f} h), mode {m}, first {first}, second {second}')
        # (a) first charger's command settles: reversals of >= 3 A while it charges
        alloc = g(f'sensor.{first}_allocated_current')
        conn1 = g(f'sensor.{first}_status_connector')
        pts = [(t, float(s)) for t, s, _ in alloc.between(w0, w1)
               if s not in ('unknown', 'unavailable') and conn1.at(t)[0] == 'Charging']
        rev = []
        for k in range(2, len(pts)):
            d1, d2 = pts[k - 1][1] - pts[k - 2][1], pts[k][1] - pts[k - 1][1]
            if d1 * d2 < 0 and abs(d1) >= 3 and abs(d2) >= 3:
                rev.append(pts[k][0])
        worst = max_in_window(rev, 300)
        a_ok = worst < 4
        print(f'     (a) first charger command: {len(pts)} changes while charging, {len(rev)} reversals >= 3 A, '
              f'max {worst} in any 5 min -> {"settles" if a_ok else "SWINGS"}')
        if rev and not a_ok:
            print('         first reversals:', ', '.join(fmt(t) for t in rev[:5]))
        # (b) second charger not paused while there is room; Optimized: no 6/0 flicker
        need = 17 if m == 'Sequential - Optimized' else 12
        alloc2, conn2 = g(f'sensor.{second}_allocated_current'), g(f'sensor.{second}_status_connector')
        lj_flags, raw_flags = [], []
        for t in ticks(w0 + dt.timedelta(minutes=2), w1):
            paused = (alloc2.num(t) or 0) < 1 and conn2.at(t)[0] not in UNPLUGGED
            lj = min((g(f'sensor.site_load_management_available_current_{p}').num(t) or 0) for p in 'abc')
            rr = raw_room(t)
            lj_flags.append((t, paused and lj >= need))
            raw_flags.append((t, paused and rr is not None and rr >= need + 1))  # 1 A slack for the estimate
        lj_bad = [s for s in spans(lj_flags) if s[1] > 90]
        raw_bad = [s for s in spans(raw_flags) if s[1] > 180]
        on = [(t, float(s) >= 1) for t, s, _ in alloc2.between(w0, w1) if s not in ('unknown', 'unavailable')]
        flips = [on[k][0] for k in range(1, len(on)) if on[k][1] != on[k - 1][1]]
        flick = max_in_window(flips, 600)
        b_ok = not raw_bad and flick < 4
        print(f'     (b) second charger paused with room >= {need} A: LJ view {sum(s for _, s in lj_bad):.0f} s in '
              f'{len(lj_bad)} span(s) > 90 s; meters {sum(s for _, s in raw_bad):.0f} s in {len(raw_bad)} span(s) > 3 min; '
              f'on/off flips {len(flips)}, max {flick} in 10 min -> {"ok" if b_ok else "BAD"}')
        for t, s in raw_bad[:5]:
            print(f'         paused with room (meters): {fmt(t)} for {s:.0f} s')
        # (c) breaker at 20 A once settled (from 5 min after both charge)
        both_chg = overlap(overlap(intervals(g('sensor.charger_status_connector'), lambda s: s == 'Charging', w1),
                                   intervals(g('sensor.charger_nova_status_connector'), lambda s: s == 'Charging', w1)),
                           [(w0, w1)])
        over, peak = [], 0.0
        for c0, c1 in both_chg:
            fl = []
            for t in ticks(c0 + dt.timedelta(minutes=5), c1):
                gm = grid_max(t)
                peak = max(peak, gm or 0)
                fl.append((t, gm is not None and gm > 20.5))
            over += [s for s in spans(fl) if s[1] > 60] if fl else []
        c_ok = not over
        print(f'     (c) breaker: {len(both_chg)} span(s) both charging, '
              f'{sum((b - a).total_seconds() for a, b in both_chg) / 60:.0f} min; peak phase {peak:.1f} A; '
              f'{len(over)} span(s) > 20.5 A for > 60 s -> {"ok" if c_ok else "OVER"}')
        if not both_chg:
            c_ok = None
        prio_note = '' if m in ('Priority', 'Shared') else f' (Priority not exercised: mode was {m})'
        verdicts.append((a_ok, b_ok, c_ok, prio_note))
    # (d) status text at every session start, either charger, whole window
    starts, seen = [], []
    for c in C:
        conn, st = g(f'sensor.{c}_status_connector'), g(f'sensor.{c}_charging_status')
        for k in range(1, len(conn.t)):
            if conn.s[k] == 'Charging' and conn.s[k - 1] != 'Charging':
                t = conn.t[k]
                hit = any(ASSUMED in str(s) for _, s, _ in st.between(t, t + dt.timedelta(seconds=120)))
                starts.append((c, t)); seen.append(hit)
    miss = [s for s, h in zip(starts, seen) if not h]
    print(f'  (d) session starts: {len(starts)}; "{ASSUMED}" shown within 2 min at {sum(seen)}')
    for c, t in miss[:10]:
        print(f'      not shown: {c} at {fmt(t)}')
    if not both:
        v = 'NOT EXERCISED (never both plugged in)'
    elif any(a is False or b is False or c is False for a, b, c, _ in verdicts):
        v = 'FAIL'
    else:
        v = 'PASS' + verdicts[-1][3]
    if starts and miss:
        v += ' / status text: REVIEW the misses above'
    elif not starts:
        v += ' / status text: NOT EXERCISED'
    print('  VERDICT 1:', v)


# --------------------------------------------------------------------------------------------- item 2
def item2():
    """Leaving boost (ed155b7): Andrej 2026-10-04 ~20:30 local, Home 2026-10-04 ~18:36 local."""
    print('\n=== 2. Leaving boost: Andrej\'s water_heater.tc and Home\'s climate.workshop_boiler')
    tanks = [('andrejg', '2026-10-04T18:30:00Z', 'water_heater.tc', 'hot_water_tank'),
             ('home', '2026-10-04T16:36:00Z', 'climate.workshop_boiler', 'server_room_hot_water_tank')]
    out = []
    for site, start, dev, pre in tanks:
        ids = [dev, f'sensor.{pre}_tank_status', f'select.{pre}_operating_mode'] + \
              [f'number.{pre}_{k}_temperature' for k in ('away', 'normal', 'boost')]
        H = hist(site, ids, start, attrs=(dev,))
        d = H[dev]
        seq, prev = [], None   # deduped (t, setpoint, state, current)
        for t, s, a in zip(d.t, d.s, d.a):
            key = (a.get('temperature'), s)
            if key != prev:
                seq.append((t, a.get('temperature'), s, a.get('current_temperature'))); prev = key
        judged = []
        for k in range(1, len(seq)):
            t, sp, state, cur = seq[k]
            normal = H[f'number.{pre}_normal_temperature'].num(t)
            psp = seq[k - 1][1]
            if psp is None or normal is None or psp <= normal + 0.5 or (sp is not None and sp >= psp):
                continue      # not a drop out of boost
            mode = H[f'select.{pre}_operating_mode'].at(t)[0]
            floor = H[f'number.{pre}_away_temperature'].num(t) if mode == 'Freeze Protection' else normal
            curv = seq[k - 1][3]
            if curv is None or floor is None or float(curv) < floor - 0.05:
                print(f'  {dev} {fmt(t)}: {psp} -> {sp} {state}, tank {curv} below its floor {floor} - by design, not judged')
                continue
            ok = sp is not None and abs(sp - floor) < 0.06 and state != 'off'
            judged.append(ok)
            print(f'  {dev} {fmt(t)}: {psp} -> {sp} {state} (mode {mode}, floor {floor}, tank {curv}) -> {"ok" if ok else "BAD"}')
        v = 'NOT EXERCISED (no boost ended with the tank at/above its floor)' if not judged else \
            ('PASS' if all(judged) else 'FAIL')
        out.append(f'{dev}: {v} ({sum(judged)}/{len(judged)} judged exits ok)')
    print('  VERDICT 2:', '; '.join(out))


# --------------------------------------------------------------------------------------------- item 3
def item3():
    print('\n=== 3. Meter-only hybrid saturation latch')
    print('  VERDICT 3: N/A - no site has the topology (grid-tied + battery power read + solar from the meter alone):'
          ' Kozolec is off-grid; Home has no battery (battery_power None) and reads a solar sensor and output'
          ' sensors; Andrej has no battery. Checked 2026-10-12 from diagnostics and states.')


# --------------------------------------------------------------------------------------------- item 4
def item4():
    """Pond EVSE restart if it stops drawing (fed9c6f, on since 2026-10-05 ~12:00 local)."""
    print('\n=== 4. Restart if it stops drawing: Kozolec Pond EVSE')
    start = '2026-10-05T10:00:00Z'
    st_id = 'sensor.pond_evse_status'
    ids = [st_id, 'switch.pond_evse', 'sensor.pond_evse_power', 'sensor.pond_evse_allocated_current',
           'select.pond_evse_operating_mode']
    H = hist('kozolec', ids, start, attrs=(st_id,))
    st, sw, pw, al = H[st_id], H['switch.pond_evse'], H['sensor.pond_evse_power'], H['sensor.pond_evse_allocated_current']
    end = ts(END)
    # a restart = restart_count stepping up
    restarts, last = [], 0
    for t, a in zip(st.t, st.a):
        c = a.get('restart_count') or 0
        if c > last:
            restarts.append((t, c))
        last = c
    gave_up = [(st.t[k], st.s[k]) for k in range(len(st.t)) if 'gave up' in str(st.s[k]) and (k == 0 or 'gave up' not in str(st.s[k - 1]))]
    bad, resumed, cutouts, prev_t = [], 0, 0, None
    for t, c in restarts:
        quiet = [float(s) for _, s, _ in pw.between(t - dt.timedelta(seconds=570), t) if s not in ('unknown', 'unavailable')]
        permitted = (al.num(t - dt.timedelta(seconds=10)) or 0) > 0
        offs = [x for x in intervals(sw, lambda s: s == 'off', end) if abs((x[0] - t).total_seconds()) < 60]
        off_s = (offs[0][1] - offs[0][0]).total_seconds() if offs else None
        before = [float(s) for _, s, _ in pw.between(t - dt.timedelta(minutes=40), t - dt.timedelta(seconds=600)) if s not in ('unknown', 'unavailable')]
        cut = bool(before) and max(before) >= 1000   # it was charging, then stopped: an over-temperature cut-out
        after = [float(s) for _, s, _ in pw.between(t + dt.timedelta(seconds=20), t + dt.timedelta(minutes=15)) if s not in ('unknown', 'unavailable')]
        res = bool(after) and max(after) >= 100
        cutouts += cut; resumed += res and cut
        why = []
        if quiet and max(quiet) >= 10: why.append(f'drew {max(quiet):.0f} W in the 10 min before')
        if not permitted: why.append('not permitted')
        if off_s is None or not 20 <= off_s <= 90: why.append(f'off {off_s} s')
        if prev_t and c > 1 and (t - prev_t).total_seconds() < 1790: why.append(f'{(t - prev_t).total_seconds() / 60:.0f} min after the last')
        if c > 3: why.append(f'restart #{c} (> 3)')
        # recovery: 5 min of draw must bring the count back to 0
        if res:
            first_draw = next(tt for tt, s, _ in pw.between(t, t + dt.timedelta(minutes=15)) if s not in ('unknown', 'unavailable') and float(s) >= 10)
            drew = all(float(s) >= 10 for _, s, _ in pw.between(first_draw, first_draw + dt.timedelta(minutes=5)) if s not in ('unknown', 'unavailable'))
            if drew and (st.at(first_draw + dt.timedelta(minutes=6))[1].get('restart_count') or 0) != 0:
                why.append('count not reset after 5 min of draw')
        print(f'  restart #{c} {fmt(t)}: off {off_s} s, {"cut-out" if cut else "no draw before"}, '
              f'{"resumed" if res else "no draw after"}{" - BAD: " + "; ".join(why) if why else ""}')
        if why: bad.append(t)
        prev_t = t
    for t, s in gave_up:
        print(f'  gave up {fmt(t)}: "{s}"')
    log = ''
    try:
        log = _get('kozolec', '/api/error_log')
    except urllib.error.HTTPError as e:
        print(f'  error_log not readable ({e.code}) - check Settings > System > Logs by hand')
    warns = [l for l in log.splitlines() if 'still draws nothing after' in l and 'Pond' in l]
    print(f'  "still draws nothing" warnings in the current log (since the last restart only): {len(warns)}')
    if not restarts:
        v = 'NOT EXERCISED (no restart)'
    elif bad:
        v = f'FAIL ({len(bad)} bad restart(s))'
    elif not cutouts:
        v = 'REVIEW - restarts all correct, but none followed an over-temperature cut-out (no draw >= 1 kW before)'
    elif not resumed:
        v = f'FAIL - {cutouts} cut-out restart(s), the EVSE resumed after none'
    else:
        v = f'PASS ({resumed}/{cutouts} cut-outs resumed; {len(gave_up)} give-up(s) at 3)'
    print('  VERDICT 4:', v)


if __name__ == '__main__':
    for n in sys.argv[1:] or ['1', '2', '3', '4']:
        {'1': item1, '2': item2, '3': item3, '4': item4}[n]()
