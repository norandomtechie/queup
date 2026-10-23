"""End-to-end relay and offline-sync tests against a live deployment (see P2P.md, Testing)."""
import json
import os
import random
import re
import string
import sys
import time
import uuid

from playwright.sync_api import sync_playwright

BASE = os.environ.get("QUEUP_BASE", "https://engineering.purdue.edu/~menon18/queup-dev")
STAFF = ("tstaff1", os.environ["QUEUP_STAFF_PW"])
STUD = ("tstud1", os.environ["QUEUP_STUD_PW"])
Q = "default_queue"


def creds(c):
    # sent with the first request, instead of after a 401 (which the load balancer sometimes answers with a reset)
    return {"username": c[0], "password": c[1], "send": "always"}


def rand_room():
    return "Y" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4))


class Env:
    def __init__(self, pw):
        self.browser = getattr(pw, os.environ.get("QUEUP_BROWSER", "chromium")).launch()
        self.contexts = []
        self._api = self.browser.new_context(http_credentials=creds(STAFF)).request
        self.api = self
        self.room = rand_room()
        r = self.get(f"{BASE}/roomd.wsgi?setup=true&action=add&room={self.room}")
        assert r.ok, r.text()

    def get(self, url, tries=3):
        """API request as tstaff1, retrying a connection reset."""
        for i in range(tries):
            try:
                return self._api.get(url)
            except Exception as e:
                if i == tries - 1 or "ECONNRESET" not in str(e):
                    raise
                time.sleep(0.5)

    def page(self, account, init_script=None, ctx=None):
        if ctx is None:
            ctx = self.browser.new_context(http_credentials=creds(account))
            if init_script:
                ctx.add_init_script(init_script)
            self.contexts.append(ctx)
        page = ctx.new_page()
        page.dialogs = []
        page.errors = []
        page.syncs = 0
        page.rechecks = 0

        def on_dialog(d):
            page.dialogs.append(d.message)
            d.accept("e2e note") if d.type == "prompt" else d.accept()
        page.on("dialog", on_dialog)
        page.on("pageerror", lambda e: page.errors.append(str(e)))

        def on_request(r):
            if "action=offlinesync" in r.url:
                page.syncs += 1
            if "action=chk&full=1" in r.url:
                page.rechecks += 1
        page.on("request", on_request)
        page.goto(f"{BASE}/?room={self.room}", wait_until="load")
        return page

    def server_queue(self):
        r = self.api.get(f"{BASE}/roomd.wsgi?setup=true&action=chk&room={self.room}&queue={Q}")
        return [x[0] for x in json.loads(r.text())[self.room][Q]]

    def log(self):
        r = self.api.get(f"{BASE}/admin/admin.wsgi?room={self.room}&fulllog=true")
        return json.loads(r.text())

    def api_add(self, waitdata="api"):
        assert self.api.get(f"{BASE}/roomd.wsgi?action=add&room={self.room}&queue={Q}&waitdata={waitdata}").ok

    def close(self):
        for c in self.contexts:
            try:
                c.close()
            except Exception:
                pass
        try:
            self.get(f"{BASE}/roomd.wsgi?setup=true&action=del&room={self.room}")
        finally:
            self.browser.close()


def wait_for(fn, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.25)
    raise AssertionError("timed out waiting for " + what)


def block(page, notice=True):
    """Cut this tab off from engineering.purdue.edu; the relay on verilog stays reachable."""
    def handler(route):
        route.abort()
    page.context.route("**/roomd.wsgi**", handler)
    page.context.route("**/ping.txt**", handler)
    if notice:
        # a tab with its own SSE notices when the stream drops; one on the relay when it next checks
        page.evaluate("if (window.evtSource) { window.evtSource.close(); createEventSource(); } else { checkHintedOutage(); }")


def unblock(page):
    page.context.unroute("**/roomd.wsgi**")
    page.context.unroute("**/ping.txt**")


def relay_up(*pages):
    wait_for(lambda: all(p.evaluate("meshConnected()") for p in pages), 20, "relay to connect")


def relying(page):
    """Room updates come over the relay and the tab's own SSE is closed."""
    return page.evaluate("window.relyingOnRelay && !window.evtSource")


def outage_confirmed(*pages):
    wait_for(lambda: all(p.evaluate("window.confirmedOutage") for p in pages), 10, "outage confirmation")


def view(page, room):
    return page.evaluate(f"(window.lastJSON[{json.dumps(room)}]['{Q}'] || []).map(e => e[0])")


def journal_size(page):
    return page.evaluate("Object.keys(window.journal).length")


def act(page, action):
    page.evaluate(f"performOp('{action}', '{Q}')")


def pump(page, seconds):
    end = time.time() + seconds
    while time.time() < end:
        page.evaluate("1")
        time.sleep(0.1)


def count_sse_requests(page):
    page.sse = 0

    def on_request(r):
        if "sseupdate=true" in r.url:
            page.sse += 1
    page.on("request", on_request)


def envelope(page, **fields):
    """A hand-made offline action from this tab (unsigned: the server would reject it at sync)."""
    return page.evaluate("""(f) => Object.assign({type: "offlineaction", id: crypto.randomUUID(), peerid: window.peerid,
        room: window.roomname, user: window.username, action: "uadd", queue: "%s", target: "", waitdata: "",
        value: "", t: Math.round(serverNow()), seq: 99, sig: "x"}, f)""" % Q, fields)


def badge(page):
    return page.evaluate("[document.documentElement.getAttribute('offlinemode'), document.getElementById('offlinebadge').textContent]")


# --- scenarios --------------------------------------------------------------

def full_cycle(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        block(staff)
        block(stud)
        outage_confirmed(staff, stud)

        act(stud, "add")
        time.sleep(0.4)
        act(staff, "add")
        time.sleep(0.4)
        act(staff, "del")
        time.sleep(1.5)
        assert view(staff, env.room) == view(stud, env.room) == ["tstud1"], (view(staff, env.room), view(stud, env.room))
        assert journal_size(staff) == journal_size(stud) == 3
        stored = stud.evaluate("Object.keys(localStorage).filter(k => k.startsWith('queup-offline:')).map(k => Object.keys(JSON.parse(localStorage.getItem(k))).length)")
        assert stored == [3], stored

        # sample the student's DOM through recovery: tstud1 must never vanish
        stud.evaluate("""window.__samples = []; window.__sampler = setInterval(() => {
            window.__samples.push(!!document.querySelector('#queue%s .is-self')); }, 100);""" % Q)
        unblock(staff)
        unblock(stud)
        wait_for(lambda: env.server_queue() == ["tstud1"], 60, "server to reflect offline actions")
        time.sleep(16)   # past the 5-15s delay for actions relayed from others
        samples = stud.evaluate("clearInterval(window.__sampler); window.__samples")
        assert samples and all(samples), "tstud1 flickered out of the student's view during recovery"
        # each tab syncs once on recovery; relayed copies the server already has are dropped, not resent
        assert (staff.syncs, stud.syncs) == (1, 1), (staff.syncs, stud.syncs)
        wait_for(lambda: journal_size(staff) == journal_size(stud) == 0, 15, "journals to empty after sync")
        assert staff.evaluate("document.documentElement.getAttribute('offlinemode')") == "live"
        codes = [(row[1], row[3]) for row in env.log() if row[3].startswith("o")]
        assert ("tstud1", "ouadd") in codes and ("tstaff1", "oudel") in codes, codes
        assert not staff.errors and not stud.errors, (staff.errors, stud.errors)
    finally:
        env.close()


def originator_gone(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        block(staff)
        block(stud)
        outage_confirmed(staff, stud)
        act(stud, "add")
        wait_for(lambda: journal_size(staff) == 1, 5, "staff to receive the relayed envelope")
        stud.context.close()
        unblock(staff)
        wait_for(lambda: env.server_queue() == ["tstud1"], 60, "staff to sync the departed student's add")
    finally:
        env.close()


def reopened_tab_syncs(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        block(staff)
        block(stud)
        outage_confirmed(staff, stud)
        act(stud, "add")
        time.sleep(1)
        ctx = stud.context
        stud.close()
        unblock(stud)          # routes belong to the context
        stud2 = env.page(STUD, ctx=ctx)
        wait_for(lambda: stud2.evaluate("window.roomname") == env.room, 10, "reopened tab to join the room")
        assert journal_size(stud2) == 1, "reopened tab did not adopt the earlier tab's journal"
        wait_for(lambda: env.server_queue() == ["tstud1"], 30, "reopened tab to sync")
        assert stud2.syncs == 1
        wait_for(lambda: stud2.evaluate("Object.keys(localStorage).filter(k => k.startsWith('queup-offline:')).length") == 0,
                 15, "the stored journal to clear")
    finally:
        env.close()


def rejection_alert_only_for_originator(pw):
    env = Env(pw)
    try:
        assert env.api.get(f"{BASE}/roomd.wsgi?setup=true&action=lock&room={env.room}").ok
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        block(staff)
        block(stud)
        outage_confirmed(staff, stud)
        act(stud, "add")
        time.sleep(1)
        assert view(staff, env.room) == ["tstud1"]
        unblock(staff)
        unblock(stud)
        wait_for(lambda: journal_size(staff) == 0 and journal_size(stud) == 0, 60, "sync to finish")
        assert env.server_queue() == []
        time.sleep(3.5)
        assert view(staff, env.room) == view(stud, env.room) == []
        assert any("could not be applied" in d and "locked" in d for d in stud.dialogs), stud.dialogs
        assert not any("could not be applied" in d for d in staff.dialogs), staff.dialogs
    finally:
        env.close()


# Storage that is present but refuses reads/writes (private mode, quota exceeded)
NO_STORAGE = """(() => {
    const fail = () => { throw new DOMException('storage unavailable', 'QuotaExceededError'); };
    Storage.prototype.setItem = fail;
    Storage.prototype.getItem = fail;
})();"""


# Storage that throws on any access at all (site data blocked)
STORAGE_BLOCKED = """Object.defineProperty(window, 'localStorage', {get() { throw new DOMException('blocked', 'SecurityError'); }});"""


def one_peer_offline_no_storage(pw, init_script=NO_STORAGE):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD, init_script=init_script)
        relay_up(staff, stud)
        wait_for(lambda: relying(stud), 15, "student to take updates from the relay")
        block(stud, notice=False)   # only the student loses the server
        act(stud, "add")            # its request fails -> the action goes over the relay
        # staff's own connection is fine, so it submits on the student's behalf
        wait_for(lambda: env.server_queue() == ["tstud1"], 25, "staff to submit for the offline student")
        assert staff.syncs == 1
        assert not stud.errors, stud.errors
        # still offline, the student keeps its own action pending: it confirms with the server itself
        pump(stud, 3)
        assert stud.evaluate("Object.values(window.journal).some(x => x.origin === 'self' && !x.acked)")
        unblock(stud)
        wait_for(lambda: stud.evaluate("Object.keys(window.journal).length") == 0, 60, "student to confirm its own action")
        assert env.server_queue() == ["tstud1"]
    finally:
        env.close()


def storage_blocked_entirely(pw):
    one_peer_offline_no_storage(pw, init_script=STORAGE_BLOCKED)


def staff_button(page, username, index):
    """Click the staff Mark (0) or Delete (1) button on username's queue entry."""
    page.evaluate("""([u, i]) => [...document.querySelectorAll('#queue%s .waiter')]
        .find(e => e.querySelector('#username_text').innerHTML == u)
        .querySelectorAll('.owner-buttons button')[i].click()""" % Q, [username, index])


def marked(page, room, username):
    return page.evaluate(f"(window.lastJSON[{json.dumps(room)}]['{Q}'].find(e => e[0] == {json.dumps(username)}) || [])[3]")


def staff_actions_offline(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        act(stud, "add")                       # online, before the outage
        wait_for(lambda: env.server_queue() == ["tstud1"], 10, "online add")
        wait_for(lambda: view(staff, env.room) == ["tstud1"], 10, "staff to see the student")
        block(staff)
        block(stud)
        outage_confirmed(staff, stud)

        staff_button(staff, "tstud1", 0)       # Mark
        wait_for(lambda: str(marked(stud, env.room, "tstud1")) == "1", 5, "mark to reach the student")
        staff_button(staff, "tstud1", 1)       # Delete
        wait_for(lambda: view(stud, env.room) == [] and view(staff, env.room) == [], 5, "removal to reach both")
        act(stud, "add")                       # rejoin offline, then staff clears
        wait_for(lambda: view(staff, env.room) == ["tstud1"], 5, "offline rejoin")
        time.sleep(0.2)
        staff.evaluate("clearQueue('%s')" % Q)
        wait_for(lambda: view(stud, env.room) == [] and view(staff, env.room) == [], 5, "clear to reach both")

        unblock(staff)
        unblock(stud)
        wait_for(lambda: journal_size(staff) == 0 and journal_size(stud) == 0, 60, "sync to finish")
        assert env.server_queue() == []
        codes = [row[3] for row in env.log() if row[3].startswith("o")]
        assert codes == ["oqmrk", "ousdel", "ouadd", "oqclr"], codes
        assert not staff.errors and not stud.errors, (staff.errors, stud.errors)
    finally:
        env.close()


def forged_actions_dropped_by_relay(pw):
    """The relay only passes on a tab's own actions, and staff actions only from owners."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        act(staff, "add")
        wait_for(lambda: view(stud, env.room) == ["tstaff1"], 10, "online add")
        block(staff)
        block(stud)
        outage_confirmed(staff, stud)
        for e in (envelope(stud, action="usdel", target="tstaff1"),             # a staff action from a student
                  envelope(stud, user="tstaff1", action="udel")):               # claiming to be the staff user
            stud.evaluate("(e) => window.relay.send(JSON.stringify({type: 'envelope', env: e}))", e)
        pump(staff, 1.5)
        assert view(staff, env.room) == ["tstaff1"], "a forged action was applied"
        assert journal_size(staff) == 0
    finally:
        env.close()


def live_updates_over_relay(pw):
    """With the relay up no tab opens an SSE stream; changes from anywhere reach every tab over the relay."""
    env = Env(pw)
    try:
        opened = []
        staff, s1, s2 = env.page(STAFF), env.page(STUD), env.page(STUD)
        for p in (staff, s1, s2):
            p.on("request", lambda r: opened.append(r.url) if "sseupdate=true" in r.url else None)
        relay_up(staff, s1, s2)
        wait_for(lambda: all(relying(p) for p in (staff, s1, s2)), 15, "every tab to close its own SSE")
        pump(staff, 2.5)                                 # past the one check each tab makes on connecting
        for p in (staff, s1, s2):
            count_sse_requests(p)
            p.rechecks = 0
        act(s1, "add")                                   # a tab's own action, over HTTP
        wait_for(lambda: all(view(p, env.room) == ["tstud1"] for p in (staff, s1, s2)), 10,
                 "the action to reach every view")
        env.api_add()                                    # a change made outside these tabs
        wait_for(lambda: all(view(p, env.room) == ["tstud1", "tstaff1"] for p in (staff, s1, s2)), 10,
                 "a change made elsewhere to reach every tab")
        pump(s1, 3)
        assert [p.sse for p in (staff, s1, s2)] == [0, 0, 0], [p.sse for p in (staff, s1, s2)]
        assert opened == [], "a tab opened SSE although the relay was up"
        assert all(p.evaluate("getComputedStyle(document.querySelector('.queueoverlay')).display") == "none" for p in (staff, s1, s2)), \
            "the join panel still covers the room"
        assert [p.rechecks for p in (staff, s1, s2)] == [0, 0, 0], "a delivered update was re-fetched anyway"
        assert not any(p.errors for p in (staff, s1, s2))
    finally:
        env.close()


def relay_drop_falls_back_to_sse(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        wait_for(lambda: relying(stud), 15, "student to take updates from the relay")
        stud.evaluate("window.relay.close()")
        wait_for(lambda: stud.evaluate("!window.relyingOnRelay && !!window.evtSource"), 5, "student to reopen its own SSE")
        env.api_add()
        wait_for(lambda: view(stud, env.room) == ["tstaff1"], 15, "updates over the student's own SSE")
        wait_for(lambda: stud.evaluate("meshConnected()"), 15, "the relay to reconnect")
        env.api.get(f"{BASE}/roomd.wsgi?action=del&room={env.room}&queue={Q}")
        wait_for(lambda: relying(stud) and view(stud, env.room) == [], 15, "the student to go back to the relay")
    finally:
        env.close()


# This browser can't reach the relay (e.g. a network that blocks verilog)
RELAY_UNREACHABLE = """(() => {
    const Real = window.WebSocket;
    window.__relayAttempts = [];
    window.WebSocket = function (url, protocols) {
        window.__relayAttempts.push(Date.now());
        return new Real("wss://127.0.0.1:9/relay", protocols);   // refused at once
    };
    window.WebSocket.prototype = Real.prototype;
    ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(k => window.WebSocket[k] = Real[k]);
})();"""


def unreachable_relay_keeps_sse(pw):
    """Without the relay a tab just keeps its own SSE, retrying the relay less and less often."""
    env = Env(pw)
    try:
        stud = env.page(STUD, init_script=RELAY_UNREACHABLE)
        wait_for(lambda: stud.evaluate("window.__relayAttempts.length") >= 1, 15, "a first relay attempt")
        env.api_add()
        wait_for(lambda: view(stud, env.room) == ["tstaff1"], 15, "updates over the tab's own SSE")
        pump(stud, 40)
        attempts = stud.evaluate("window.__relayAttempts")
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        assert 2 <= len(attempts) <= 7 and all(b > a * 1.1 for a, b in zip(gaps, gaps[1:])), gaps
        assert stud.evaluate("!!window.evtSource && !window.relyingOnRelay")
        assert not stud.dialogs and not stud.errors, (stud.dialogs, stud.errors)
    finally:
        env.close()


def missed_update_is_recovered(pw):
    """roomd's push to the relay can be lost; a tab on the relay re-checks the server instead of staying stale."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        wait_for(lambda: relying(stud), 15, "student to take updates from the relay")
        stud.evaluate("""window.__realHandle = handleRelayMessage;
            handleRelayMessage = (m) => { if (m.type !== 'room') window.__realHandle(m); }; 0""")
        pump(stud, 2.5)                                  # past the one check each tab makes on connecting
        stud.rechecks = 0
        # 1. its own action: no update follows, so it fetches the room itself
        act(stud, "add")
        wait_for(lambda: view(stud, env.room) == ["tstud1"], 8, "the student's own add to show without the relay")
        assert stud.rechecks == 1, stud.rechecks
        # 2. someone else removes it unseen; acting on the stale view gets a 400 and a refresh
        env.api.get(f"{BASE}/roomd.wsgi?action=del&room={env.room}&queue={Q}&username=tstud1")
        pump(stud, 1)
        assert view(stud, env.room) == ["tstud1"]
        act(stud, "del")
        wait_for(lambda: view(stud, env.room) == [], 8, "a rejected action to refresh the stale view")
    finally:
        env.close()


def outage_hint_reaches_idle_tabs(pw):
    """Tabs on the relay have no SSE to notice an outage; the first tab that does tells the others to check."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        wait_for(lambda: relying(staff) and relying(stud), 15, "both tabs on the relay")
        block(staff, notice=False)
        block(stud, notice=False)
        stud.evaluate("confirmOutageNow()")              # e.g. its request just failed
        wait_for(lambda: staff.evaluate("window.confirmedOutage"), 8, "the idle staff tab to confirm the outage itself")
        wait_for(lambda: badge(staff)[0] == "mesh", 5, "the staff badge to show the outage")
        unblock(staff)
        unblock(stud)
    finally:
        env.close()


def forged_outage_hint_is_checked(pw):
    """An outage hint is unauthenticated: a tab that can still reach the server stays online."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        pings = []
        staff.on("request", lambda r: pings.append(r.url) if "ping.txt" in r.url else None)
        stud.evaluate("window.relay.send(JSON.stringify({type: 'outage'}))")
        pump(staff, 3)
        assert len(pings) == 1, pings
        assert not staff.evaluate("window.confirmedOutage") and badge(staff)[0] == "live"
    finally:
        env.close()


class Holder:
    """Holds matching requests and releases them later, like a hung server working through its backlog."""
    def __init__(self, ctx, predicate):
        self.held, self.predicate, self.ctx = [], predicate, ctx
        ctx.route("**/roomd.wsgi**", self._handle)

    def _handle(self, route):
        if self.predicate(route.request.url):
            self.held.append(route)
        else:
            route.continue_()

    def release(self):
        for route in self.held:
            try:
                route.continue_()
            except Exception:
                pass            # the page already gave up on it
        self.held = []
        self.ctx.unroute("**/roomd.wsgi**")


def slow_server_backlog_cannot_undo_later_actions(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        wait_for(lambda: relying(stud), 15, "student to take updates from the relay")
        hold = Holder(stud.context, lambda u: "action=add&" in u or "action=del&" in u)
        t0 = time.time()
        act(stud, "add")                           # stuck on the "server"
        pump(stud, 0.7)
        act(stud, "del")                           # also stuck
        # each action goes over the relay after ~2s, but only the request timing out (8s) counts as an outage
        wait_for(lambda: stud.evaluate("Object.keys(window.journal).length") >= 1, 4, "the slow action to go over the relay")
        assert time.time() - t0 < 4.5, "fallback took longer than ~2s"
        assert not stud.evaluate("window.confirmedOutage"), "a merely slow request switched the tab to offline mode"
        wait_for(lambda: stud.evaluate("window.confirmedOutage"), 12, "the timed-out request to count as an outage")
        assert len(hold.held) == 2
        act(stud, "add")                           # rejoin: goes straight over the relay
        wait_for(lambda: view(staff, env.room) == ["tstud1"], 5, "rejoin to reach the staff tab")
        wait_for(lambda: env.server_queue() == ["tstud1"], 25, "the staff tab to sync the offline actions")
        hold.release()                             # the backlog finally reaches the server
        pump(stud, 3)
        assert env.server_queue() == ["tstud1"], "a stale request undid the user's later rejoin"
        assert not any("Could not reach" in d for d in stud.dialogs), stud.dialogs
    finally:
        env.close()


def slow_sync_falls_back_to_offline(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        block(staff)
        block(stud)
        outage_confirmed(staff, stud)
        act(stud, "add")
        wait_for(lambda: journal_size(staff) == 1, 5, "the envelope to reach the staff tab")
        stud.context.close()                       # only the staff tab is left to sync it
        unblock(staff)
        hold = Holder(staff.context, lambda u: "action=offlinesync" in u)
        wait_for(lambda: staff.evaluate("1") and len(hold.held) >= 1, 30, "the staff tab to attempt a sync")
        t_sync = time.time()
        wait_for(lambda: staff.evaluate("window.confirmedOutage"), 8, "an unanswered sync to count as an outage")
        assert time.time() - t_sync < 7
        assert staff.evaluate("document.documentElement.getAttribute('offlinemode')") in ("mesh", "nomesh")
        hold.release()                             # server "recovers"
        wait_for(lambda: env.server_queue() == ["tstud1"], 60, "the retried sync to land")
    finally:
        env.close()


def stale_pending_action_does_not_hide_newer_state(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        act(stud, "add")
        wait_for(lambda: view(staff, env.room) == ["tstud1"], 10, "online add")
        # the staff tab still holds an older, never-synced "leave" for this user
        staff.evaluate("""() => {
            const e = {type: "offlineaction", id: crypto.randomUUID(), peerid: crypto.randomUUID(),
                room: window.roomname, user: "tstud1", action: "udel", queue: "%s", target: "",
                waitdata: "", value: "", t: Math.round(serverNow()) - 20000, seq: 1, sig: "x"};
            window.journal[e.id] = {env: e, origin: "relayed", acked: false};
            renderRoom();
        }""" % Q)
        assert view(staff, env.room) == ["tstud1"], "a stale pending leave hid the user"
        # and the next update shows the server already moved past it, so it's dropped rather than sent
        env.api_add()
        wait_for(lambda: journal_size(staff) == 0, 10, "the superseded relayed action to be dropped")
    finally:
        env.close()


def outage_switch_and_status_badges(pw):
    env = Env(pw)
    try:
        reveal = "document.getElementById('outageswitch').style.display = 'unset'"
        staff, s1, s2 = env.page(STAFF), env.page(STUD), env.page(STUD)
        relay_up(staff, s1, s2)
        wait_for(lambda: all(badge(p) == ["live", "🟢 Syncing with 2 users"] for p in (staff, s1, s2)), 10,
                 "every tab to show it syncs with the other two")
        s1.evaluate(reveal); s1.click("#outageswitch .switchCylinder")          # the tester's switch
        wait_for(lambda: badge(s1) == ["mesh", "🟠 Syncing with 2 users, offline."], 5, "s1 to go offline over the relay")
        assert badge(staff)[0] == "live"                   # only s1 lost the server
        act(s1, "add")
        wait_for(lambda: env.server_queue() == ["tstud1"], 25, "s1's offline action to reach the server via another tab")
        pump(s1, 1)
        assert badge(s1)[0] == "mesh", "a relayed update ended s1's outage although it still can't reach the server"
        s1.click("#offlinebadge")
        assert s1.dialogs[-1].startswith("The server that hosts QueUp is down, so we've switched to the backup relay")
        s1.evaluate(reveal); s1.click("#outageswitch .switchCylinder")
        wait_for(lambda: badge(s1)[0] in ("live", "syncing"), 10, "s1 to come back when the switch is turned off")
        # the other tabs go away, then s2 loses the server: alone and offline
        staff.context.close()
        s1.context.close()
        wait_for(lambda: s2.evaluate("meshUserCount()") == 0, 10, "s2 to see it is alone on the relay")
        s2.evaluate(reveal); s2.click("#outageswitch .switchCylinder")
        wait_for(lambda: badge(s2) == ["nomesh", "🔴 Server disconnected"], 10, "a tab alone and offline to say so")
        s2.click("#offlinebadge")
        assert s2.dialogs[-1].startswith("You are currently disconnected from the queue")
        assert not s2.errors, s2.errors
    finally:
        env.close()


def rename_queue_while_on_relay(pw):
    env = Env(pw)
    try:
        a, b = env.page(STAFF), env.page(STAFF)
        relay_up(a, b)
        wait_for(lambda: relying(a) and relying(b), 15, "both staff tabs on the relay")
        a.evaluate("""() => {
            const t = document.querySelector('[queue=%s]');
            t.innerHTML = 'renamed_q';
            t.dispatchEvent(new Event('blur'));
        }""" % Q)
        for p in (a, b):
            wait_for(lambda: p.evaluate("!!document.querySelector('#queuerenamed_q') && !document.querySelector('#queue%s')" % Q),
                     10, "the renamed queue to show up (and the old one to go)")
        assert not a.errors and not b.errors, (a.errors, b.errors)
    finally:
        env.close()


def crowded_join_is_not_rate_limited(pw):
    """Several tabs of one user joining at once must not be rate-limited."""
    env = Env(pw)
    try:
        limited = []
        staff = env.page(STAFF)
        tabs = [env.page(STUD) for _ in range(3)]
        for p in [staff] + tabs:
            p.on("response", lambda r: limited.append(r.url) if r.status == 429 else None)
        relay_up(staff, *tabs)
        pump(staff, 3)
        assert limited == [], limited
    finally:
        env.close()


def signed_out_tab_is_synced_by_others(pw):
    """A tab that loses its login (every request 401) keeps working: other tabs sync its signed actions."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        staff.context.route("**/roomd.wsgi**", lambda r: r.fulfill(status=401, body="Unauthorized"))
        staff.context.route("**/ping.txt**", lambda r: r.fulfill(status=401, body="Unauthorized"))
        act(staff, "add")                                  # discovers the 401
        wait_for(lambda: badge(staff)[0] == "signedout", 10, "the tab to say it is signed out")
        assert any("signed out" in d for d in staff.dialogs), staff.dialogs
        # the other tab must not be dragged into offline mode
        pump(stud, 2)
        assert badge(stud)[0] != "mesh" and not stud.evaluate("window.confirmedOutage")
        wait_for(lambda: env.server_queue() == ["tstaff1"], 30, "the signed-out tab's action to be synced by the other")
    finally:
        env.close()


def tabs_cannot_send_room_state(pw):
    """Only roomd can publish room state; a tab sending a fake update over the relay is ignored."""
    env = Env(pw)
    try:
        a, b = env.page(STUD), env.page(STUD)
        relay_up(a, b)
        a.evaluate("""() => {
            const d = JSON.parse(JSON.stringify(window.serverJSON));
            d.meta.broadcast = {message: "Go to evil.example to keep your spot", id: "fake"};
            d.servertime = (d.servertime || 0) + 1;
            window.relay.send(JSON.stringify({type: "room", data: d}));
        }""")
        pump(b, 2)
        assert not any("evil.example" in d for d in b.dialogs), b.dialogs
    finally:
        env.close()


def idle_tab_pauses_and_keeps_its_place(pw):
    """After an hour idle a tab stops all server and relay traffic, stays in its queue, and can reconnect."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        act(stud, "add")
        wait_for(lambda: env.server_queue() == ["tstud1"], 10, "student to join the queue")
        wait_for(lambda: staff.evaluate("meshUserCount()") == 1, 10, "staff to see one other tab")
        stud.evaluate("window.lastActivity = Date.now() - 2 * 3600 * 1000; checkIdle()")
        assert stud.is_visible("#idlebanner")
        sent = []
        stud.on("request", lambda r: sent.append(r.url) if "roomd.wsgi" in r.url else None)
        wait_for(lambda: staff.evaluate("meshUserCount()") == 0, 10, "the relay to see the paused tab leave")
        pump(stud, 20)
        assert sent == [], sent
        assert stud.evaluate("!window.evtSource && !window.relay")
        assert env.server_queue() == ["tstud1"], "pausing must not remove anyone from a queue"
        stud.click("#idlebanner button")        # Reconnect
        wait_for(lambda: stud.evaluate("window.roomname") == env.room and not stud.is_visible("#idlebanner"), 15, "the tab to rejoin")
        relay_up(staff, stud)
    finally:
        env.close()


def relay_toggle_falls_back_to_server(pw):
    """With the backup relay turned off a tab only talks to the server; turning it back on rejoins the relay."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        sent = []
        stud.on("request", lambda r: sent.append(r.url))
        with stud.expect_navigation():
            stud.evaluate("toggleP2P()")
        wait_for(lambda: stud.evaluate("window.roomname") == env.room, 15, "the tab to reload into the room")
        assert stud.evaluate("document.getElementById('p2ptoggle').textContent") == "Backup relay: Off"
        pump(stud, 6)
        assert not [u for u in sent if "action=peerjoin" in u], "peerjoin sent with the relay off"
        assert not stud.evaluate("meshConnected()") and stud.evaluate("!!window.evtSource")
        act(stud, "add")                                   # straight to the server
        wait_for(lambda: env.server_queue() == ["tstud1"], 10, "the direct request to land")
        stud.evaluate("document.getElementById('outageswitch').style.display = 'unset'")
        stud.click("#outageswitch .switchCylinder")
        act(stud, "del")                                   # no relay to fall back to
        wait_for(lambda: stud.evaluate("1") and any("Could not reach the server" in d for d in stud.dialogs), 15,
                 "the normal failure alert")
        assert env.server_queue() == ["tstud1"] and journal_size(stud) == 0
        stud.click("#outageswitch .switchCylinder")
        with stud.expect_navigation():
            stud.evaluate("toggleP2P()")
        wait_for(lambda: stud.evaluate("window.roomname") == env.room, 15, "the tab to reload into the room")
        relay_up(staff, stud)
    finally:
        env.close()


def malicious_relayed_data_is_not_rendered(pw):
    """HTML smuggled in through the relay (an envelope's waitdata, or a doctored room update) must never run."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        wait_for(lambda: relying(stud), 15, "student to take updates from the relay")
        evil = "<img src=x onerror=window.__pwned=1>"
        # 1. a student tab sends an envelope with HTML in waitdata
        e = envelope(stud, waitdata=evil)
        stud.evaluate("(e) => window.relay.send(JSON.stringify({type: 'envelope', env: e}))", e)
        # 2. a doctored room update, as if the relay itself were compromised
        stud.evaluate("""(evil) => {
            const d = JSON.parse(JSON.stringify(window.serverJSON));
            d[window.roomname]["%s"] = [["tstud2", Date.now() / 1000, evil, 0, evil], [evil, 1, "x", 0, ""]];
            d.names = {"tstud2": evil}; d.servertime = (d.servertime || 0) + 1;
            handleRelayMessage({type: "room", data: d});
        }""" % Q, evil)
        pump(stud, 2)
        for p in (staff, stud):
            assert p.evaluate("window.__pwned") is None, "injected script ran"
            assert p.evaluate("document.querySelectorAll('#rooms img').length") == 0, "injected element was rendered"
        assert journal_size(staff) == 0, "an invalid envelope was accepted"
        cleaned = stud.evaluate("""(evil) => sanitizeRelayedPayload({[window.roomname]: {"%s": [["tstud2", 1, evil, 0, evil], [evil, 1, "x", 0, ""], ["tstud3", 2, "ok", 1, "3"]],
                                                                  "<b>": [["tstud4", 1, "x", 0, ""]]},
                                                         names: {"tstud2": evil}, meta: {}, servertime: 5})""" % Q, evil)
        assert cleaned[env.room] == {Q: [["tstud3", 2, "ok", 1, "3"]]}, cleaned[env.room]
        assert "<" not in cleaned["names"]["tstud2"]
    finally:
        env.close()


# The deployment under test, on disk (the suite runs on a host that shares the web tree)
PAGE = os.environ.get("QUEUP_PAGE", "/home/min/a/menon18/web/queup-dev/app.html")


def deploy_makes_open_pages_update(pw):
    """A deploy that changes the page reaches open tabs: they reload themselves, but not while someone is typing."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        relay_up(staff, stud)
        wait_for(lambda: stud.evaluate("window.pageBuild") is not None, 10, "the tab to note its own build")
        stud.evaluate("""window.__beforeUpdate = true;
            const i = document.createElement('input'); i.id = '__typing'; document.body.appendChild(i); i.focus(); 0""")
        os.utime(PAGE, None)                  # a deploy: the page's file changes
        env.api_add()                          # the next update carries the new build
        wait_for(lambda: stud.is_visible("#updatebanner"), 70, "the update banner while the student is typing")
        assert stud.evaluate("window.__beforeUpdate") is True, "the page reloaded while someone was typing"
        stud.evaluate("document.getElementById('__typing').blur(); 0")

        def reloaded():
            try:
                return stud.evaluate("window.__beforeUpdate === undefined && window.roomname") == env.room
            except Exception:
                return False                   # mid-navigation
        wait_for(reloaded, 40, "the page to reload itself once nobody is typing")
        relay_up(stud)
        assert not stud.is_visible("#updatebanner")
        assert not stud.errors, stud.errors
    finally:
        env.close()


SCENARIOS = [full_cycle, originator_gone, reopened_tab_syncs,
             rejection_alert_only_for_originator, one_peer_offline_no_storage, storage_blocked_entirely,
             staff_actions_offline, forged_actions_dropped_by_relay,
             live_updates_over_relay, relay_drop_falls_back_to_sse, unreachable_relay_keeps_sse,
             missed_update_is_recovered, outage_hint_reaches_idle_tabs, forged_outage_hint_is_checked,
             slow_server_backlog_cannot_undo_later_actions, slow_sync_falls_back_to_offline,
             stale_pending_action_does_not_hide_newer_state, outage_switch_and_status_badges,
             rename_queue_while_on_relay, crowded_join_is_not_rate_limited, signed_out_tab_is_synced_by_others,
             tabs_cannot_send_room_state, idle_tab_pauses_and_keeps_its_place, relay_toggle_falls_back_to_server,
             malicious_relayed_data_is_not_rendered, deploy_makes_open_pages_update]

if __name__ == "__main__":
    wanted = sys.argv[1:]
    failed = 0
    with sync_playwright() as pw:
        for fn in SCENARIOS:
            if wanted and fn.__name__ not in wanted:
                continue
            t0 = time.time()
            try:
                fn(pw)
                print(f"PASS {fn.__name__} ({time.time() - t0:.1f}s)", flush=True)
            except Exception as e:
                failed += 1
                # first line only: Playwright's call logs include request headers, logins among them
                print(f"FAIL {fn.__name__} ({time.time() - t0:.1f}s): {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}", flush=True)
    sys.exit(1 if failed else 0)
