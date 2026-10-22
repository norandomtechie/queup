"""Log analysis for the QueUp logview.

Pure standard library; no Redis, no web framework.  Run it directly for a
quick look at one room:   python3 analyze.py /path/to/logs/27000.log

Log line format (see roomd.wsgi):  epoch,user,action,room[,args...]

Time-to-service ("wait") is the time between a `uadd` (user adds self to a
queue) and the first of:
  * `qmrk`  - staff marked that user on that queue     -> outcome "mark"
  * `usdel` - staff removed that user                   -> outcome "staff"
  * `udel`  - the user removed themself                 -> outcome "self"
Other ways an open entry can end are *not* services and are counted
separately: `qclr` (queue cleared, "cleared"), `qdel`/`rdel` ("qdel"),
entries still open when the log ends ("unresolved"), and entries open longer
than the stale cap ("stale", default 4 h - almost always someone who forgot
to leave; they would otherwise wreck every max/avg).
"""
import bisect
import glob
import heapq
import json
import math
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

VERSION = 6
DEFAULT_CAP_HOURS = 4
# A cached analysis younger than this is reused even if the log has grown since
# (busy rooms append constantly, so exact-match caching would almost never hit).
CACHE_TTL = 300
CACHE_STATS = {"hit": 0, "fresh": 0, "miss": 0, "dir": None}
MIN_WAIT = 2  # seconds; shorter waits are likely mis-clicks and are ignored for "min" figures only
TZ_NAME = "America/Indiana/Indianapolis"

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # old Python / no tzdata: fixed UTC-5 (no DST)
    TZ = timezone(timedelta(hours=-5))
    TZ_NAME = "UTC-05:00 (fixed; zoneinfo unavailable)"

ACTIONS = ("rcreate rchk rdel rown rdelown rsub runlock rlock rcool qadd qdel "
           "qren qclr qmrk uadd udel usdel rperm r1q rtimer rbcast").split()

# Actions that only room owners / staff can perform; anyone doing one of
# these in a room is treated as staff for that room.
STAFF_ACTIONS = {"qmrk", "usdel", "qclr", "qadd", "qdel", "qren", "rown",
                 "rdelown", "rsub", "rlock", "runlock", "rcool", "rperm",
                 "r1q", "rtimer", "rbcast"}

# --- semester definitions -------------------------------------------------
# Purdue's calendar follows a fixed pattern (verified against the registrar's
# published calendars for 2017-2028 with tools/scrape_calendar.py; the only
# exceptions are the COVID-shortened Fall 2020 and Spring 2021):
#   Spring starts the first Monday on or after Jan 7;  ends 117 days later (Sat after finals)
#   Summer starts 9 days after Spring ends (a Monday); ends 81 days later (a Friday)
#   Fall   starts the first Monday on or after Aug 19; ends 117 days later (Sat after finals)
# Anything in ../semesters.json ({"terms": {"Fall 2020": {"start":..,"end":..}}})
# overrides the rule, so exceptions can be added without touching the code.
# A day belongs to a term from LEAD_DAYS before it starts (setup week = "week 0")
# through its end; days between terms (winter break etc.) belong to no semester
# and appear only in the all-time views.
TERM_ORDER = {"Spring": 0, "Summer": 1, "Fall": 2}
LEAD_DAYS = 7
CAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "semesters.json")


def _first_monday(y, m, d):
    x = date(y, m, d)
    return x + timedelta(days=(7 - x.weekday()) % 7)


def _load_overrides():
    try:
        with open(CAL_FILE) as f:
            t = json.load(f).get("terms", {})
        st = os.stat(CAL_FILE)
        return t, [st.st_mtime, st.st_size]
    except Exception:
        return {}, None


OVERRIDES, CAL_SIG = _load_overrides()


def term_dates(term, year):
    """(start, end) dates of a term: classes begin .. last day of finals."""
    ov = OVERRIDES.get("%s %d" % (term, year))
    if ov:
        return date.fromisoformat(ov["start"]), date.fromisoformat(ov["end"])
    if term == "Spring":
        st = _first_monday(year, 1, 7)
        return st, st + timedelta(117)
    if term == "Summer":
        st = _first_monday(year, 1, 7) + timedelta(117 + 9)
        return st, st + timedelta(81)
    st = _first_monday(year, 8, 19)
    return st, st + timedelta(117)


def term_bounds(term, year):
    """-> (start, end, from, to): `from` includes the week-0 lead-in."""
    st, en = term_dates(term, year)
    return st, en, st - timedelta(LEAD_DAYS), en


def term_of(d):
    """date -> (term, year, start date) or None if d falls between terms."""
    for term in ("Spring", "Summer", "Fall"):
        st, en, lo, hi = term_bounds(term, d.year)
        if lo <= d <= hi:
            return term, d.year, st
    return None


def monday(d):
    return d - timedelta(days=d.weekday())


HIST_EDGES = [30, 60, 120, 300, 600, 1200, 1800, 3600, 7200]  # 10 buckets
DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def stats(vals):
    n = len(vals)
    if not n:
        return None
    s = sorted(vals)
    mean = sum(s) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in s) / (n - 1)) if n > 1 else 0.0

    def pct(q):  # nearest rank
        return s[min(n - 1, max(0, int(math.ceil(q * n)) - 1))]
    ok = [x for x in s if x >= MIN_WAIT]  # "min" skips likely mis-clicks
    return {"n": n, "min": ok[0] if ok else None, "max": s[-1], "mean": mean, "sd": sd,
            "median": statistics.median(s), "p90": pct(0.90), "p95": pct(0.95)}


def fmt_when(ts):
    d = datetime.fromtimestamp(ts, TZ)
    return d.strftime("%a %b ") + str(d.day) + d.strftime(", %Y ") + _h12(d.hour, d.minute)


def _h12(h, m=None):
    s = "%d" % (h % 12 or 12)
    if m is not None:
        s += ":%02d" % m
    return s + (" AM" if h < 12 else " PM")


def fmt_day(day):  # iso 'YYYY-MM-DD'
    d = date.fromisoformat(day)
    return d.strftime("%a %b ") + str(d.day) + d.strftime(", %Y")


# --- reading ----------------------------------------------------------------
# Offline (peer-to-peer) rows: same layout as the online action plus a "synced by" field at index 5
OFFLINE_CODES = {"ouadd", "oudel", "ousdel", "oqmrk", "oqclr"}


def read_events(path):
    """-> (events sorted by time, line count, bad line count)"""
    ev, lines, bad = [], 0, 0
    with open(path, "r", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            lines += 1
            p = line.split(",")
            if len(p) > 2 and p[2] == "orej":  # rejected offline action: nothing happened
                continue
            if len(p) > 5 and p[2] in OFFLINE_CODES:
                p = p[:2] + [p[2][1:]] + p[3:5] + p[6:]
            if len(p) < 4 or p[2] not in ACTIONS:
                bad += 1
                continue
            try:
                t = float(p[0])
            except ValueError:
                bad += 1
                continue
            ev.append((t, p))
    ev.sort(key=lambda x: x[0])
    return ev, lines, bad


def simulate(events, cap):
    """Replay the log, pairing joins with outcomes.

    Returns dict with: sessions, joins, visits, lost, staff, peaks, queues,
    actions, subtitle
    """
    openq = {}            # (user, queue) -> start ts
    heap = []             # (start ts, key) for stale eviction
    stalekeys = {}        # keys evicted as stale, awaiting a late resolution
    queue_open = Counter()
    sessions, joins, visits, lost = [], [], [], []
    staff, queues, actions = set(), set(), Counter()
    peaks = []            # (ts, queue, queue_open, room_open) at each join
    subtitle = ""
    waitdata = {}

    def close(key, t, outcome, handler):
        t0 = openq.pop(key)
        queue_open[key[1]] -= 1
        sessions.append((t0, t - t0, outcome, key[1], key[0], handler, waitdata.pop(key, "")))

    for t, p in events:
        # evict stale open entries
        while heap and heap[0][0] + cap < t:
            t0, key = heapq.heappop(heap)
            if openq.get(key) == t0:
                del openq[key]
                queue_open[key[1]] -= 1
                stalekeys[key] = t0
                waitdata.pop(key, None)
                lost.append((t0, "stale"))
        a, u = p[2], p[1]
        actions[a] += 1
        if a in STAFF_ACTIONS:
            staff.add(u)
        if a == "rchk":
            visits.append((t, u))
        elif a == "uadd" and len(p) >= 5:
            key = (u, p[4])
            if key in openq:  # duplicate join in the log: treat the old one as stale
                queue_open[p[4]] -= 1
                lost.append((openq[key], "stale"))
            stalekeys.pop(key, None)
            openq[key] = t
            queue_open[p[4]] += 1
            waitdata[key] = ",".join(p[5:])
            heapq.heappush(heap, (t, key))
            queues.add(p[4])
            joins.append((t, u, p[4]))
            peaks.append((t, p[4], queue_open[p[4]], len(openq)))
        elif a in ("qmrk", "usdel") and len(p) >= 6:
            key = (p[5], p[4])
            if key in openq:
                close(key, t, "mark" if a == "qmrk" else "staff", u)
            else:
                stalekeys.pop(key, None)
        elif a == "udel" and len(p) >= 5:
            key = (u, p[4])
            if key in openq:
                close(key, t, "self", "")
            else:
                stalekeys.pop(key, None)
        elif a == "qclr" and len(p) >= 5:
            for key in [k for k in openq if k[1] == p[4]]:
                lost.append((openq.pop(key), "cleared"))
                queue_open[key[1]] -= 1
                waitdata.pop(key, None)
        elif a == "qdel" and len(p) >= 5:
            for key in [k for k in openq if k[1] == p[4]]:
                lost.append((openq.pop(key), "qdel"))
                queue_open[key[1]] -= 1
                waitdata.pop(key, None)
        elif a == "qren" and len(p) >= 6:
            for key in [k for k in openq if k[1] == p[4]]:
                nk = (key[0], p[5])
                openq[nk] = openq.pop(key)
                if key in waitdata:
                    waitdata[nk] = waitdata.pop(key)
                queue_open[p[4]] -= 1
                queue_open[p[5]] += 1
            heap = [(t0, (k[0], p[5]) if k[1] == p[4] else k) for t0, k in heap]
            heapq.heapify(heap)
            queues.add(p[5])
        elif a == "rdel":
            for key in list(openq):
                lost.append((openq.pop(key), "qdel"))
            queue_open.clear()
            waitdata.clear()
        elif a == "rsub":
            subtitle = ",".join(p[4:])
    for t0 in openq.values():
        lost.append((t0, "unresolved"))
    return {"sessions": sessions, "joins": joins, "visits": visits, "lost": lost,
            "staff": staff, "peaks": peaks, "queues": sorted(queues),
            "actions": dict(actions), "subtitle": subtitle}


# --- aggregation ------------------------------------------------------------
def _loc(ts):
    return datetime.fromtimestamp(ts, TZ)


class Rec:
    """A timestamped record with its local-time breakdown and semester."""
    __slots__ = ("t", "dow", "hour", "day", "mon", "sem", "week", "data")

    def __init__(self, t, data):
        d = _loc(t)
        self.t, self.data = t, data
        self.dow, self.hour = d.weekday(), d.hour
        dd = d.date()
        self.day, self.mon = dd.isoformat(), monday(dd).isoformat()
        tm = term_of(dd)
        if tm:
            self.sem = "%s %d" % tm[:2]
            self.week = (monday(dd) - monday(tm[2])).days // 7 + 1
        else:
            self.sem = self.week = None


def _top(counter, n):
    return counter.most_common(n)


def aggregate(sess, joins, visits, lost, staff, peak):
    """sess/joins/visits are lists of Rec; lost is list of kinds; peak is
    (value, label) or None."""
    waits = [r.data[0] for r in sess]
    out = Counter(r.data[1] for r in sess)
    for k in lost:
        out[k] += 1
    res = {
        "joins": len(joins), "visits": len(visits),
        "visitors": len({r.data[0] for r in visits}),
        "students": len({r.data[0] for r in joins if r.data[0] not in staff}),
        "days": len({r.day for r in joins}),
        "outcomes": {k: out.get(k, 0) for k in
                     ("mark", "staff", "self", "cleared", "qdel", "stale", "unresolved")},
        "stats": stats(waits),
        "stats_by_outcome": {o: stats([r.data[0] for r in sess if r.data[1] == o])
                             for o in ("mark", "staff", "self")},
    }
    # histogram
    hist = [0] * (len(HIST_EDGES) + 1)
    for w in waits:
        hist[bisect.bisect_right(HIST_EDGES, w)] += 1
    res["hist"] = hist

    # weeks (week-of-semester, pooled across semesters in the scope)
    wk_s, wk_j = defaultdict(list), Counter()
    for r in sess:
        if r.week is not None:
            wk_s[r.week].append(r.data[0])
    for r in joins:
        if r.week is not None:
            wk_j[r.week] += 1
    res["weeks"] = []
    for w in sorted(set(wk_s) | set(wk_j)):
        st = stats(wk_s.get(w, [])) or {}
        st["week"], st["joins"] = w, wk_j[w]
        st.setdefault("n", 0)
        res["weeks"].append(st)

    # hour x weekday heatmap
    heat = {"joins": [[0] * 24 for _ in range(7)],
            "n": [[0] * 24 for _ in range(7)],
            "sum": [[0.0] * 24 for _ in range(7)]}
    for r in joins:
        heat["joins"][r.dow][r.hour] += 1
    for r in sess:
        heat["n"][r.dow][r.hour] += 1
        heat["sum"][r.dow][r.hour] += r.data[0]
    res["heat"] = heat

    # per queue
    qs = defaultdict(list)
    for r in sess:
        qs[r.data[3]].append(r.data[0])
    qj = Counter(r.data[1] for r in joins)
    res["queues"] = sorted(
        [{"queue": q, "joins": qj[q], "stats": stats(qs.get(q, []))} for q in qj],
        key=lambda x: -x["joins"])[:30]

    # leaderboards
    sj = Counter(r.data[0] for r in joins if r.data[0] not in staff)
    sw = defaultdict(list)
    for r in sess:
        sw[r.data[4]].append(r.data[0])
    res["top_students"] = [{"user": u, "joins": n, "stats": stats(sw.get(u, []))}
                       for u, n in _top(sj, 25)]
    hs, hw = Counter(), defaultdict(list)
    for r in sess:
        h = r.data[5]
        if h:
            hs[(h, r.data[1])] += 1
            hw[h].append(r.data[0])
    handlers = Counter()
    for (h, o), n in hs.items():
        handlers[h] += n
    res["top_staff"] = [{"user": h, "handled": n,
                     "marks": hs.get((h, "mark"), 0), "removals": hs.get((h, "staff"), 0),
                     "stats": stats(hw[h])} for h, n in _top(handlers, 25)]

    # superlatives
    sup = []

    def add(id_, label, kind, value, detail):
        sup.append({"id": id_, "label": label, "kind": kind, "value": value, "detail": detail})

    if sess:
        mx = max(sess, key=lambda r: r.data[0])
        mn = min((r for r in sess if r.data[0] >= MIN_WAIT), key=lambda r: r.data[0], default=None)
        who = lambda r: "%s in %s" % (r.data[4], r.data[3])
        how = {"mark": "marked by %s", "staff": "removed by %s", "self": "left on their own"}
        add("max_wait", "Max time-to-service", "dur", mx.data[0],
            "%s, joined %s, %s" % (who(mx), fmt_when(mx.t),
                                  (how[mx.data[1]] % mx.data[5]) if mx.data[5] else how["self"]))
        instant = sum(1 for w in waits if w < MIN_WAIT)
        if mn:
            add("min_wait", "Min time-to-service", "dur", mn.data[0],
                "%s, %s; ignoring %d sessions under %d s (likely mis-clicks)"
                % (who(mn), fmt_when(mn.t), instant, MIN_WAIT))
    if joins:
        dayc = Counter(r.day for r in joins)
        d, n = dayc.most_common(1)[0]
        add("busiest_day", "Busiest day", "count", n, "%s (joins)" % fmt_day(d))
        hc = Counter((r.day, r.hour) for r in joins)
        (d, h), n = hc.most_common(1)[0]
        add("busiest_hour", "Busiest single hour", "count", n,
            "%s, %s" % (fmt_day(d), _h12(h)))
        sc = Counter((r.dow, r.hour) for r in joins)
        (dw, h), n = sc.most_common(1)[0]
        add("busiest_slot", "Busiest weekly slot", "count", n,
            "%ss at %s, summed over all weeks in view" % (DOW[dw], _h12(h)))
        wc = Counter(r.mon for r in joins)
        m, n = wc.most_common(1)[0]
        add("busiest_week", "Busiest week", "count", n, "week of %s" % fmt_day(m))
        spans = {}
        for r in joins:
            lo, hi = spans.get(r.day, (r.t, r.t))
            spans[r.day] = (min(lo, r.t), max(hi, r.t))
        d, (lo, hi) = max(spans.items(), key=lambda kv: kv[1][1] - kv[1][0])
        add("longest_day", "Longest day of activity", "dur", hi - lo,
            "%s, first join %s to last %s" % (fmt_day(d), _h12(_loc(lo).hour, _loc(lo).minute),
                                             _h12(_loc(hi).hour, _loc(hi).minute)))
        u, n = Counter(r.data[0] for r in joins if r.data[0] not in staff).most_common(1)[0] \
            if any(r.data[0] not in staff for r in joins) else (None, 0)
        if u:
            add("top_student", "Most frequent student", "count", n, "%s (joins)" % u)
        ud = Counter((r.data[0], r.day) for r in joins if r.data[0] not in staff)
        if ud:
            (u, d), n = ud.most_common(1)[0]
            add("top_repeat", "Most joins by one student in a day", "count", n,
                "%s on %s" % (u, fmt_day(d)))
    if peak:
        add("peak_queue", "Peak queue length", "count", peak[0], peak[1])
    if sess:
        dsum, dn = defaultdict(float), Counter()
        for r in sess:
            dsum[r.day] += r.data[0]
            dn[r.day] += 1
        c = [(dsum[d] / dn[d], d, dn[d]) for d in dn if dn[d] >= 20]
        if c:
            v, d, n = max(c)
            add("slowest_day", "Slowest day (avg time-to-service)", "dur", v,
                "%s, over %d sessions (days with 20+ sessions)" % (fmt_day(d), n))
        ssum, sn = defaultdict(float), Counter()
        for r in sess:
            ssum[(r.dow, r.hour)] += r.data[0]
            sn[(r.dow, r.hour)] += 1
        c = [(ssum[k] / sn[k], k, sn[k]) for k in sn if sn[k] >= 20]
        if c:
            v, (dw, h), n = max(c)
            add("slowest_slot", "Slowest weekly slot (avg)", "dur", v,
                "%ss at %s, over %d sessions" % (DOW[dw], _h12(h), n))
        if hs:
            h, n = handlers.most_common(1)[0]
            add("top_staff", "Most active staff member", "count", n,
                "%s (marks + removals)" % h)
            c = [(statistics.mean(hw[x]), x, len(hw[x])) for x in hw if len(hw[x]) >= 30]
            if c:
                v, x, n = min(c)
                add("fastest_staff", "Fastest staff (avg time-to-service)", "dur", v,
                    "%s, over %d sessions (staff with 30+)" % (x, n))
    res["superlatives"] = sup
    return res


def analyze_log(path, room, cap_hours=DEFAULT_CAP_HOURS):
    cap = cap_hours * 3600
    events, lines, bad = read_events(path)
    sim = simulate(events, cap)
    sess = [Rec(s[0], (s[1], s[2], s[3], s[3], s[4], s[5], s[6])) for s in sim["sessions"]]
    # session data: (wait, outcome, queue, queue, student, handler, waitdata)
    # (queue is duplicated so [3]=queue,[4]=student,[5]=handler line up with
    # the join tuples' naming in aggregate()); join data: (user, queue)
    joins = [Rec(j[0], (j[1], j[2])) for j in sim["joins"]]
    visits = [Rec(v[0], (v[1],)) for v in sim["visits"]]
    lost = [(Rec(t, None), k) for t, k in sim["lost"]]
    staff = sim["staff"]

    # peaks per semester in one pass
    peak_by_sem = {}
    for t, q, qn, rn in sim["peaks"]:
        s = Rec(t, None).sem
        if s not in peak_by_sem or qn > peak_by_sem[s][0]:
            peak_by_sem[s] = (qn, t, q, rn)
    fmtp = lambda b: (b[0], "in %s, %s (%d people across all queues in the room)"
                      % (b[2], fmt_when(b[1]), b[3]))
    all_peak = max(peak_by_sem.values(), key=lambda b: b[0]) if peak_by_sem else None

    result = {
        "version": VERSION, "room": room, "generated": datetime.now().timestamp(),
        "tz": TZ_NAME, "cap_hours": cap_hours,
        "lines": lines, "bad_lines": bad,
        "first": events[0][0] if events else None,
        "last": events[-1][0] if events else None,
        "actions": sim["actions"], "queues": sim["queues"],
        "subtitle": sim["subtitle"], "staff_count": len(staff),
        "all": aggregate(sess, joins, visits, [k for _, k in lost], staff,
                         fmtp(all_peak) if all_peak else None),
    }

    sems = sorted(({r.sem for r in sess} | {r.sem for r in joins} | {r.sem for r in visits}) - {None},
                  key=lambda k: (int(k.split()[1]), TERM_ORDER[k.split()[0]]))
    result["semesters"] = []
    for s in sems:
        sub = lambda L: [r for r in L if r.sem == s]
        sj, ss, sv = sub(joins), sub(sess), sub(visits)
        sl = [k for r, k in lost if r.sem == s]
        agg = aggregate(ss, sj, sv, sl, staff, fmtp(peak_by_sem[s]) if s in peak_by_sem else None)
        term, year = s.split()
        st, en, lo, hi = term_bounds(term, int(year))
        agg.update({"key": s, "term": term, "year": int(year),
                    "start": st.isoformat(), "end": en.isoformat(),
                    "from": lo.isoformat(), "to": hi.isoformat(),
                    "first": min([r.t for r in sj + sv + ss], default=None),
                    "last": max([r.t for r in sj + sv + ss], default=None)})
        result["semesters"].append(agg)

    # daily series (for timelines)
    day = defaultdict(lambda: [0, 0, 0, 0.0, 0.0])  # visits, joins, served, sum, max
    for r in visits:
        day[r.day][0] += 1
    for r in joins:
        day[r.day][1] += 1
    for r in sess:
        d = day[r.day]
        d[2] += 1
        d[3] += r.data[0]
        d[4] = max(d[4], r.data[0])
    result["daily"] = [[d] + v for d, v in sorted(day.items())]
    return result


# --- caching ------------------------------------------------------------------
def cache_dir(private_dir=None):
    cands = []
    if os.environ.get("QUEUP_LV_CACHE"):
        cands.append(os.environ["QUEUP_LV_CACHE"])
    if private_dir:
        cands.append(os.path.join(private_dir, "logview_cache"))
    # no shared /tmp fallback: other accounts on the host could read or plant cache files
    for c in cands:
        try:
            os.makedirs(c, exist_ok=True)
            if os.access(c, os.W_OK):
                return c
        except OSError:
            pass
    return None


def room_json(logdir, room, cap_hours=DEFAULT_CAP_HOURS, private_dir=None):
    """JSON text for one room's analysis, cached on log mtime/size."""
    path = os.path.join(logdir, room + ".log")
    st = os.stat(path)
    sig = [VERSION, st.st_mtime, st.st_size, CAL_SIG]
    cdir = cache_dir(private_dir)
    CACHE_STATS["dir"] = cdir
    cpath = os.path.join(cdir, "%s.cap%d.json" % (room, cap_hours)) if cdir else None
    if cpath and os.path.exists(cpath):
        try:
            with open(cpath) as f:
                head = json.loads(f.readline())
                if head["sig"] == sig:
                    CACHE_STATS["hit"] += 1
                    return f.read()
                if time.time() - head["t"] < CACHE_TTL:
                    CACHE_STATS["fresh"] += 1
                    return f.read()
        except Exception:
            pass
    CACHE_STATS["miss"] += 1
    text = json.dumps(analyze_log(path, room, cap_hours), separators=(",", ":"))
    if cpath:
        try:
            tmp = cpath + ".%d.tmp" % os.getpid()
            with open(tmp, "w") as f:
                f.write(json.dumps({"sig": sig, "t": time.time()}) + "\n" + text)
            os.replace(tmp, cpath)
        except OSError:
            pass
    return text


def rooms_overview(logdir, cap_hours=DEFAULT_CAP_HOURS, private_dir=None):
    out = []
    for p in sorted(glob.glob(os.path.join(logdir, "*.log"))):
        room = os.path.basename(p)[:-4]
        if len(room) != 5 or not room.isalnum():
            continue
        try:
            d = json.loads(room_json(logdir, room, cap_hours, private_dir))
        except Exception:
            continue
        a = d["all"]
        out.append({"room": room, "first": d["first"], "last": d["last"],
                    "subtitle": d["subtitle"], "events": d["lines"],
                    "joins": a["joins"], "visits": a["visits"],
                    "semesters": [s["key"] for s in d["semesters"]],
                    "stats": a["stats"], "outcomes": a["outcomes"]})
    return out


if __name__ == "__main__":
    p = sys.argv[1]
    r = analyze_log(p, os.path.basename(p)[:-4])
    print(json.dumps(r, indent=1)[:6000])
