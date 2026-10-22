"""End-to-end peer-to-peer tests against a live deployment (see P2P.md, Testing)."""
import json
import os
import random
import re
import string
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("QUEUP_BASE", "https://engineering.purdue.edu/~menon18/queup-dev")
STAFF = ("tstaff1", os.environ["QUEUP_STAFF_PW"])
STUD = ("tstud1", os.environ["QUEUP_STUD_PW"])
Q = "default_queue"
MESH_VERSION = 5   # keep in sync with app.html


def rand_room():
    return "Y" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4))


class Env:
    def __init__(self, pw):
        self.browser = getattr(pw, os.environ.get("QUEUP_BROWSER", "chromium")).launch()
        self.contexts = []
        self.api = self.browser.new_context(http_credentials={"username": STAFF[0], "password": STAFF[1]}).request
        self.room = rand_room()
        r = self.api.get(f"{BASE}/roomd.wsgi?setup=true&action=add&room={self.room}")
        assert r.ok, r.text()

    def page(self, creds, init_script=None, ctx=None):
        if ctx is None:
            ctx = self.browser.new_context(http_credentials={"username": creds[0], "password": creds[1]})
            if init_script:
                ctx.add_init_script(init_script)
            self.contexts.append(ctx)
        page = ctx.new_page()
        page.dialogs = []
        page.errors = []
        page.syncs = 0

        def on_dialog(d):
            page.dialogs.append(d.message)
            d.accept("e2e note") if d.type == "prompt" else d.accept()
        page.on("dialog", on_dialog)
        page.on("pageerror", lambda e: page.errors.append(str(e)))

        def on_request(r):
            if "action=offlinesync" in r.url:
                page.syncs += 1
        page.on("request", on_request)
        page.goto(f"{BASE}/?room={self.room}", wait_until="load")
        return page

    def server_queue(self):
        r = self.api.get(f"{BASE}/roomd.wsgi?setup=true&action=chk&room={self.room}&queue={Q}")
        return [x[0] for x in json.loads(r.text())[self.room][Q]]

    def log(self):
        r = self.api.get(f"{BASE}/admin/admin.wsgi?room={self.room}&fulllog=true")
        return json.loads(r.text())

    def close(self):
        for c in self.contexts:
            try:
                c.close()
            except Exception:
                pass
        self.api.get(f"{BASE}/roomd.wsgi?setup=true&action=del&room={self.room}")
        self.browser.close()


def wait_for(fn, timeout, what):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.25)
    raise AssertionError("timed out waiting for " + what)


def block(page):
    def handler(route):
        route.abort()
    page.context.route("**/roomd.wsgi**", handler)
    page.context.route("**/ping.txt**", handler)
    # notice the loss now; a tab relying on the host learns of it from the host
    page.evaluate("if (window.evtSource) { window.evtSource.close(); createEventSource(); }")


def unblock(page):
    page.context.unroute("**/roomd.wsgi**")
    page.context.unroute("**/ping.txt**")


def mesh_up(*pages):
    wait_for(lambda: all(p.evaluate("meshConnected()") for p in pages), 20, "mesh to connect")


def outage_confirmed(*pages):
    wait_for(lambda: all(p.evaluate("window.confirmedOutage") for p in pages), 10, "outage confirmation")


def view(page, room):
    return page.evaluate(f"(window.lastJSON[{json.dumps(room)}]['{Q}'] || []).map(e => e[0])")


def journal_size(page):
    return page.evaluate("Object.keys(window.journal).length")


def act(page, action):
    page.evaluate(f"performOp('{action}', '{Q}')")


# --- scenarios --------------------------------------------------------------

def full_cycle(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
        assert staff.evaluate("window.amIHost"), "owner should be host"
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
        time.sleep(9)   # past the non-host's 4-7s sync delay
        samples = stud.evaluate("clearInterval(window.__sampler); window.__samples")
        assert samples and all(samples), "tstud1 flickered out of the student's view during recovery"
        # the host syncs everything; the student also confirms its own action with the server (doesn't trust the host)
        assert (staff.syncs, stud.syncs) == (1, 1), (staff.syncs, stud.syncs)
        wait_for(lambda: journal_size(staff) == journal_size(stud) == 0, 15, "journals to empty after sync")
        assert staff.evaluate("document.documentElement.getAttribute('offlinemode')") == "live"
        codes = [(row[1], row[3], row[6] if len(row) > 6 else None) for row in env.log() if row[3].startswith("o")]
        assert ("tstud1", "ouadd", "tstaff1") in codes and ("tstaff1", "oudel", "tstaff1") in codes, codes
        assert not staff.errors and not stud.errors, (staff.errors, stud.errors)
    finally:
        env.close()


def originator_gone(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
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
        mesh_up(staff, stud)
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
        assert stud2.evaluate("Object.keys(localStorage).filter(k => k.startsWith('queup-offline:')).length") == 0
    finally:
        env.close()


def rejection_alert_only_for_originator(pw):
    env = Env(pw)
    try:
        assert env.api.get(f"{BASE}/roomd.wsgi?setup=true&action=lock&room={env.room}").ok
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
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
        mesh_up(staff, stud)
        wait_for(lambda: stud.evaluate("window.relyingOnHost"), 15, "student to hand off its SSE")
        block(stud)            # only the student loses the server
        act(stud, "add")       # its request fails -> falls back to the mesh
        # staff's own connection is fine, so it submits on the student's behalf
        wait_for(lambda: env.server_queue() == ["tstud1"], 20, "staff to submit for the offline student")
        assert staff.syncs == 1
        assert not stud.errors, stud.errors
        # still offline, the student keeps its own action pending: it doesn't take the host's word for it
        pump(stud, 3)
        assert stud.evaluate("Object.values(window.journal).some(x => x.origin === 'self' && !x.acked)")
        # back online, it confirms with the server itself, and the entry clears
        unblock(stud)
        wait_for(lambda: stud.evaluate("Object.keys(window.journal).length") == 0, 60, "student to confirm its own action")
        assert env.server_queue() == ["tstud1"]
    finally:
        env.close()


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
        mesh_up(staff, stud)
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


def forged_staff_actions_dropped_by_host(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
        act(staff, "add")
        wait_for(lambda: view(stud, env.room) == ["tstaff1"], 10, "online add")
        block(staff)
        block(stud)
        outage_confirmed(staff, stud)
        # a student's tab sends staff actions: as itself, and claiming the staff tab
        stud.evaluate("""() => {
            const base = {type: "offlineaction", room: window.roomname, queue: "%s", waitdata: "",
                          value: "", t: Math.round(serverNow()), seq: 99, sig: "x"};
            sendMeshAction(Object.assign({}, base, {id: crypto.randomUUID(), peerid: window.peerid,
                user: "tstud1", action: "usdel", target: "tstaff1"}));
            sendMeshAction(Object.assign({}, base, {id: crypto.randomUUID(), peerid: window.currentHost.peerid,
                user: "tstaff1", action: "usdel", target: "tstaff1"}));
        }""" % Q)
        time.sleep(1.5)
        assert view(staff, env.room) == ["tstaff1"], "host applied a forged staff action"
        assert journal_size(staff) == 0
    finally:
        env.close()


def relying(page):
    return page.evaluate("window.relyingOnHost && !window.evtSource")


def count_sse_requests(page):
    page.sse = 0

    def on_request(r):
        if "sseupdate=true" in r.url:
            page.sse += 1
    page.on("request", on_request)


def sse_consolidation(pw):
    env = Env(pw)
    try:
        staff, s1, s2 = env.page(STAFF), env.page(STUD), env.page(STUD)
        mesh_up(staff, s1, s2)
        wait_for(lambda: relying(s1) and relying(s2), 15, "non-hosts to hand off their SSE")
        assert staff.evaluate("window.amIHost && !!window.evtSource")
        count_sse_requests(s1)
        count_sse_requests(s2)
        act(s1, "add")                                   # a non-host's own action, over HTTP
        wait_for(lambda: all(view(p, env.room) == ["tstud1"] for p in (staff, s1, s2)), 10,
                 "the non-host's own action to reach every view")
        env.api.get(f"{BASE}/roomd.wsgi?action=add&room={env.room}&queue={Q}&waitdata=api")
        wait_for(lambda: all(view(p, env.room) == ["tstud1", "tstaff1"] for p in (s1, s2)), 10,
                 "a change made elsewhere to reach the non-hosts")
        assert (s1.sse, s2.sse) == (0, 0), (s1.sse, s2.sse)
        assert not any(p.errors for p in (staff, s1, s2))
    finally:
        env.close()


def host_leaves(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
        wait_for(lambda: relying(stud), 15, "student to hand off its SSE")
        staff.context.close()
        wait_for(lambda: stud.evaluate("!window.relyingOnHost && !!window.evtSource"), 30,
                 "student to reopen its own SSE")
        env.api.get(f"{BASE}/roomd.wsgi?action=add&room={env.room}&queue={Q}&waitdata=api")
        wait_for(lambda: view(stud, env.room) == ["tstaff1"], 15, "updates over the student's own SSE")
    finally:
        env.close()


def host_goes_quiet(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
        wait_for(lambda: relying(stud), 15, "student to hand off its SSE")
        stud.evaluate("window.RELAY_STALE_MS = 3000")
        staff.evaluate("window.sendPayloadTo = () => {}")     # host keeps its link but stops relaying
        wait_for(lambda: stud.evaluate("!window.relyingOnHost && !!window.evtSource"), 20,
                 "student to notice and reopen its own SSE")
        env.api.get(f"{BASE}/roomd.wsgi?action=add&room={env.room}&queue={Q}&waitdata=api")
        wait_for(lambda: view(stud, env.room) == ["tstaff1"], 15, "updates over the student's own SSE")
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


def pump(page, seconds):
    end = time.time() + seconds
    while time.time() < end:
        page.evaluate("1")
        time.sleep(0.1)


def slow_server_backlog_cannot_undo_later_actions(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
        wait_for(lambda: relying(stud), 15, "student to hand off its SSE")
        hold = Holder(stud.context, lambda u: "action=add&" in u or "action=del&" in u)
        t0 = time.time()
        act(stud, "add")                           # stuck on the "server"
        pump(stud, 0.7)
        act(stud, "del")                           # also stuck
        # each action goes over the mesh after ~2s, but only the request timing out (8s) counts as an outage
        wait_for(lambda: stud.evaluate("Object.keys(window.journal).length") >= 1, 4, "the slow action to go over the mesh")
        assert time.time() - t0 < 4.5, "fallback took longer than ~2s"
        assert not stud.evaluate("window.confirmedOutage"), "a merely slow request switched the tab to offline mode"
        wait_for(lambda: stud.evaluate("window.confirmedOutage"), 12, "the timed-out request to count as an outage")
        assert len(hold.held) == 2
        act(stud, "add")                           # rejoin: goes straight over the mesh
        wait_for(lambda: view(staff, env.room) == ["tstud1"], 5, "rejoin to reach the host")
        wait_for(lambda: env.server_queue() == ["tstud1"], 20, "the host to sync the offline actions")
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
        mesh_up(staff, stud)
        block(staff)
        block(stud)
        outage_confirmed(staff, stud)
        act(stud, "add")
        wait_for(lambda: journal_size(staff) == 1, 5, "the envelope to reach the host")
        unblock(staff)
        unblock(stud)
        hold = Holder(staff.context, lambda u: "action=offlinesync" in u)
        wait_for(lambda: staff.evaluate("1") and len(hold.held) >= 1, 30, "the host to attempt a sync")
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
        mesh_up(staff, stud)
        act(stud, "add")
        wait_for(lambda: view(staff, env.room) == ["tstud1"], 10, "online add")
        # the host still holds an older, never-acknowledged "leave" for this user
        staff.evaluate("""() => {
            const e = {type: "offlineaction", id: crypto.randomUUID(), peerid: crypto.randomUUID(),
                room: window.roomname, user: "tstud1", action: "udel", queue: "%s", target: "",
                waitdata: "", value: "", t: Math.round(serverNow()) - 20000, seq: 1, sig: "x"};
            window.journal[e.id] = {env: e, origin: "relayed", acked: false};
            renderRoom();
        }""" % Q)
        assert view(staff, env.room) == ["tstud1"], "a stale pending leave hid the user"
    finally:
        env.close()


def owner_host_heartbeats_for_its_peers(pw):
    env = Env(pw)
    try:
        staff, s1, s2 = env.page(STAFF), env.page(STUD), env.page(STUD)
        mesh_up(staff, s1, s2)
        wait_for(lambda: relying(s1) and relying(s2), 15, "non-hosts to hand off their SSE")
        calls = {id(p): [] for p in (staff, s1, s2)}
        for p in (staff, s1, s2):
            def on_request(r, p=p):
                for action in ("peerjoin", "peerheartbeat"):
                    if f"action={action}&" in r.url:
                        calls[id(p)].append((action, r.post_data))
            p.on("request", on_request)
        pump(staff, 100)                       # past the 90s presence window
        assert calls[id(s1)] == [] and calls[id(s2)] == [], (calls[id(s1)], calls[id(s2)])
        beats = [body for action, body in calls[id(staff)] if action == "peerheartbeat"]
        assert 2 <= len(beats) <= 3 and all(b and len(json.loads(b)["peers"]) == 2 for b in beats), beats
        assert not any(a == "peerjoin" for a, _ in calls[id(staff)])
        # a fresh payload must still list both students, who sent nothing themselves
        env.api.get(f"{BASE}/roomd.wsgi?action=add&room={env.room}&queue={Q}&waitdata=api")
        wait_for(lambda: staff.evaluate("window.knownPeers.length") == 3 and view(staff, env.room) == ["tstaff1"], 10,
                 "fresh payload listing all three peers")
    finally:
        env.close()


def student_host_peers_heartbeat_themselves(pw):
    env = Env(pw)
    try:
        a, b = env.page(STUD), env.page(STUD)      # no staff tab: a student is host
        mesh_up(a, b)
        other = b if a.evaluate("window.amIHost") else a
        # a student host isn't trusted to relay room state: the peer keeps its own SSE
        pump(other, 8)
        assert not other.evaluate("window.relyingOnHost") and other.evaluate("!!window.evtSource")
        beats = []
        other.on("request", lambda r: beats.append(r.url) if "action=peerheartbeat&" in r.url else None)
        pump(other, 50)
        assert len(beats) >= 1, "a peer of a student host stopped heartbeating"
    finally:
        env.close()


def refreshed_host_leaves_cleanly(pw):
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
        old_id = staff.evaluate("window.peerid")
        staff.reload(wait_until="load")          # leave beacon on pagehide
        wait_for(lambda: staff.evaluate("window.roomname") == env.room, 10, "reloaded tab to rejoin")
        wait_for(lambda: all(p.evaluate("!window.knownPeers.some(x => x.peerid === %s)" % json.dumps(old_id))
                             for p in (staff, stud)), 10, "the old tab to leave the peers list")
        t0 = time.time()
        mesh_up(staff, stud)
        assert time.time() - t0 < 15, "mesh took too long to re-form after a refresh"
    finally:
        env.close()


def crashed_host_is_waited_out(pw):
    """A crashed early peer (no leave notice) is waited out and the mesh forms around a live host."""
    import uuid
    env = Env(pw)
    try:
        ghost = str(uuid.uuid4())
        v = MESH_VERSION   # a current-version tab that crashed
        assert env.api.get(f"{BASE}/roomd.wsgi?setup=true&action=peerjoin&room={env.room}&peerid={ghost}&v={v}").ok
        time.sleep(1.1)
        staff, stud = env.page(STAFF), env.page(STUD)
        wait_for(lambda: all(p.evaluate("window.currentHost && window.currentHost.peerid") == ghost for p in (staff, stud)),
                 15, "both tabs to elect the (dead) early peer at first")
        end = time.time() + 130
        while time.time() < end and not (staff.evaluate("meshConnected()") and stud.evaluate("meshConnected()")):
            pump(staff, 1)
        assert staff.evaluate("meshConnected()") and stud.evaluate("meshConnected()"), "mesh never formed"
        assert staff.evaluate("window.currentHost.peerid") == stud.evaluate("window.currentHost.peerid") != ghost
    finally:
        env.close()


def old_build_tab_is_never_elected(pw):
    """A live tab from an older page version must not host the room."""
    import uuid
    env = Env(pw)
    try:
        old = str(uuid.uuid4())
        assert env.api.get(f"{BASE}/roomd.wsgi?setup=true&action=peerjoin&room={env.room}&peerid={old}").ok
        time.sleep(1.1)
        staff, stud = env.page(STAFF), env.page(STUD)
        t0 = time.time()
        mesh_up(staff, stud)
        assert time.time() - t0 < 15
        assert staff.evaluate("window.currentHost.peerid") == stud.evaluate("window.currentHost.peerid") != old
        assert staff.evaluate("window.knownPeers.some(p => p.peerid === %s)" % json.dumps(old)), "old tab should still be listed"
    finally:
        env.close()


def badge(page):
    return page.evaluate("[document.documentElement.getAttribute('offlinemode'), document.getElementById('offlinebadge').textContent]")


def outage_switch_and_status_badges(pw):
    env = Env(pw)
    try:
        reveal = "document.getElementById('outageswitch').style.display = 'unset'"
        staff, s1, s2 = env.page(STAFF), env.page(STUD), env.page(STUD)
        mesh_up(staff, s1, s2)
        wait_for(lambda: all(badge(p) == ["live", "🟢 Syncing with 2 users"] for p in (staff, s1, s2)), 10,
                 "every tab to show it syncs with the other two")
        s1.evaluate(reveal); s1.click("#outageswitch .switchCylinder")          # the tester's switch
        wait_for(lambda: badge(s1) == ["mesh", "🟠 Syncing with 2 users, offline."], 5, "s1 to go offline over the mesh")
        assert badge(staff)[0] == "live"                   # only s1 lost the server
        act(s1, "add")
        wait_for(lambda: env.server_queue() == ["tstud1"], 20, "s1's offline action to reach the server via the host")
        s1.click("#offlinebadge")
        assert s1.dialogs[-1].startswith("The server that hosts QueUp is down, so we've switched to WebRTC")
        s1.evaluate(reveal); s1.click("#outageswitch .switchCylinder")
        wait_for(lambda: badge(s1)[0] in ("live", "syncing"), 10, "s1 to come back when the switch is turned off")
        # everyone offline except s2, then s2's only link (the host) goes away
        for p in (staff, s1):
            p.evaluate(reveal); p.click("#outageswitch .switchCylinder")
        staff.context.close()
        s1.context.close()
        s2.evaluate(reveal); s2.click("#outageswitch .switchCylinder")
        wait_for(lambda: badge(s2) == ["nomesh", "🔴 Server disconnected"], 30, "a tab alone and offline to say so")
        s2.click("#offlinebadge")
        assert s2.dialogs[-1].startswith("You are currently disconnected from the queue")
        assert not any(p.errors for p in (s2,))
    finally:
        env.close()


def mesh_survives_host_change_while_writes_fail(pw):
    """New requests fail while the open SSE stream lives on, and the election wants a new host."""
    env = Env(pw)
    try:
        a, b = env.page(STUD), env.page(STUD)          # a student host
        mesh_up(a, b)
        for p in (a, b):
            p.context.route("**/*", lambda r: r.abort())  # new requests only; open SSE survives
        staff = env.page(STAFF)                          # unblocked: joins and becomes the preferred host
        wait_for(lambda: all(p.evaluate("window.currentHost && window.currentHost.owner") for p in (a, b)), 20,
                 "both tabs to elect the staff tab")
        pump(a, 25)                                      # past the 15s connect timeout
        assert a.evaluate("meshConnected()") and b.evaluate("meshConnected()"), "the working a-b link was torn down"
        wait_for(lambda: a.evaluate("window.confirmedOutage") and b.evaluate("window.confirmedOutage"), 20,
                 "failed signaling to count as an outage")
        act(b, "add")
        wait_for(lambda: view(a, env.room) == ["tstud1"], 10, "an action to cross the surviving link")
        for p in (a, b):
            p.context.unroute("**/*")
    finally:
        env.close()


def rename_queue_while_relying_on_host(pw):
    env = Env(pw)
    try:
        host, other = env.page(STAFF), env.page(STAFF)
        mesh_up(host, other)
        tab = other if relying(other) or not other.evaluate("window.amIHost") else host
        wait_for(lambda: relying(tab), 15, "the non-host staff tab to hand off its SSE")
        tab.evaluate("""() => {
            const t = document.querySelector('[queue=%s]');
            t.innerHTML = 'renamed_q';
            t.dispatchEvent(new Event('blur'));
        }""" % Q)
        wait_for(lambda: tab.evaluate("!!document.querySelector('#queuerenamed_q') && !document.querySelector('#queue%s')" % Q),
                 10, "the renamed queue to show up (and the old one to go)")
        assert not tab.errors, tab.errors
    finally:
        env.close()


def storage_blocked_entirely(pw):
    one_peer_offline_no_storage(pw, init_script=STORAGE_BLOCKED)


def crowded_join_is_not_rate_limited(pw):
    """Several tabs of one user joining at once must not be rate-limited."""
    env = Env(pw)
    try:
        limited = []
        staff = env.page(STAFF)
        tabs = [env.page(STUD) for _ in range(3)]
        for p in [staff] + tabs:
            p.on("response", lambda r: limited.append(r.url) if r.status == 429 else None)
        mesh_up(staff, *tabs)
        pump(staff, 3)
        assert limited == [], limited
    finally:
        env.close()


def host_signed_out(pw):
    """The host tab loses its login (every request 401)."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
        wait_for(lambda: relying(stud), 15, "student to rely on the staff host")
        staff.context.route("**/roomd.wsgi**", lambda r: r.fulfill(status=401, body="Unauthorized"))
        staff.context.route("**/ping.txt**", lambda r: r.fulfill(status=401, body="Unauthorized"))
        act(staff, "add")                                  # discovers the 401
        wait_for(lambda: badge(staff)[0] == "signedout", 10, "the host to say it is signed out")
        assert any("signed out" in d for d in staff.dialogs), staff.dialogs
        # the student must not be dragged into offline mode, and takes its own connection back
        wait_for(lambda: stud.evaluate("!window.relyingOnHost && !!window.evtSource && window.evtSource.readyState === 1"), 15,
                 "the student to reopen its own SSE")
        assert badge(stud)[0] != "mesh" and not stud.evaluate("window.confirmedOutage")
        # the host's click still reaches the server, synced by the student
        wait_for(lambda: env.server_queue() == ["tstaff1"], 30, "the signed-out host's action to be synced by its peer")
    finally:
        env.close()


def student_host_cannot_relay_room_state(pw):
    """With no staff present a student's tab hosts; its relayed room state (e.g. a fake broadcast) is ignored."""
    env = Env(pw)
    try:
        a, b = env.page(STUD), env.page(STUD)
        mesh_up(a, b)
        host, peer = (a, b) if a.evaluate("window.amIHost") else (b, a)
        host.evaluate("""() => {
            const d = JSON.parse(JSON.stringify(window.serverJSON));
            d.meta.broadcast = {message: "Go to evil.example to keep your spot", id: "fake"};
            d.servertime = (d.servertime || 0) + 1;
            Object.values(window.hostChannels).forEach(dc => dc.send(JSON.stringify({type: "payload", room: window.roomname, data: d})));
        }""")
        pump(peer, 2)
        assert not any("evil.example" in d for d in peer.dialogs), peer.dialogs
        assert not peer.evaluate("window.relyingOnHost")
    finally:
        env.close()


def signals_bound_to_their_owner(pw):
    """Another user can't read, consume or send WebRTC signals as someone else's peer id."""
    env = Env(pw)
    try:
        staff = env.page(STAFF)
        wait_for(lambda: staff.evaluate("!!window.iceServers"), 10, "staff tab to join")
        victim = staff.evaluate("window.peerid")
        attacker = env.page(STUD)
        wait_for(lambda: attacker.evaluate("!!window.iceServers"), 10, "attacker tab to join")
        attacker.evaluate("""(victim) => {
            window.__stolen = [];
            const es = new EventSource(`roomd.wsgi?sseupdate=true&room=${window.roomname}&peerid=${victim}`);
            es.onmessage = (m) => { const d = JSON.parse(m.data); if (d.signals && d.signals.length) window.__stolen.push(...d.signals); };
        }""", victim)
        status = attacker.evaluate("""(victim) => fetch(`roomd.wsgi?setup=true&action=signalsend&room=${window.roomname}&peerid=${victim}`,
            {method: "POST", headers: {"Content-Type": "application/json"},
             body: JSON.stringify({messages: [{to: victim, type: "ice", payload: {}}]})}).then(r => r.status)""", victim)
        assert status == 400, status
        stud = env.page(STUD)                     # a legitimate peer signals the staff host
        mesh_up(staff, stud)
        assert attacker.evaluate("window.__stolen.length") == 0, "another user's signals were delivered"
    finally:
        env.close()


def student_host_cannot_fake_staff_actions_downstream(pw):
    env = Env(pw)
    try:
        a, b = env.page(STUD), env.page(STUD)
        mesh_up(a, b)
        host, peer = (a, b) if a.evaluate("window.amIHost") else (b, a)
        host.evaluate("""() => sendMeshAction({type: "offlineaction", id: crypto.randomUUID(), peerid: window.peerid,
            room: window.roomname, user: window.username, action: "qclr", queue: "%s", target: "", waitdata: "",
            value: "", t: Math.round(serverNow()), seq: 1, sig: "x"})""" % Q)
        pump(peer, 2)
        assert journal_size(peer) == 0, "a student's staff action was accepted"
    finally:
        env.close()


def idle_tab_pauses_and_keeps_its_place(pw):
    """After an hour idle a tab stops all server and mesh traffic, stays in its queue, and can reconnect."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
        act(stud, "add")
        wait_for(lambda: env.server_queue() == ["tstud1"], 10, "student to join the queue")
        stud_peer = stud.evaluate("window.peerid")
        stud.evaluate("window.lastActivity = Date.now() - 2 * 3600 * 1000; checkIdle()")
        assert stud.is_visible("#idlebanner")
        sent = []
        stud.on("request", lambda r: sent.append(r.url) if "roomd.wsgi" in r.url else None)
        wait_for(lambda: staff.evaluate("!window.knownPeers.some(p => p.peerid === %s)" % json.dumps(stud_peer)), 15,
                 "the host to see the paused tab leave")
        pump(stud, 50)                          # past a heartbeat interval
        assert sent == [], sent
        assert stud.evaluate("!window.evtSource && openLinks().length === 0")
        assert env.server_queue() == ["tstud1"], "pausing must not remove anyone from a queue"
        stud.click("#idlebanner button")        # Reconnect
        wait_for(lambda: stud.evaluate("window.roomname") == env.room and not stud.is_visible("#idlebanner"), 15, "the tab to rejoin")
        mesh_up(staff, stud)
    finally:
        env.close()


def p2p_toggle_falls_back_to_server(pw):
    """With peer-to-peer turned off a tab only talks to the server; turning it back on rejoins the mesh."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
        sent = []
        stud.on("request", lambda r: sent.append(r.url))
        with stud.expect_navigation():
            stud.evaluate("toggleP2P()")
        wait_for(lambda: stud.evaluate("window.roomname") == env.room, 15, "the tab to reload into the room")
        assert stud.evaluate("document.getElementById('p2ptoggle').textContent") == "Peer-to-peer: Off"
        pump(stud, 6)
        mesh_calls = [u for u in sent if re.search(r"action=(peerjoin|peerheartbeat|signalsend)", u)]
        assert mesh_calls == [] or all("peerleave" in u for u in mesh_calls), mesh_calls
        assert not stud.evaluate("meshConnected()") and stud.evaluate("!!window.evtSource")
        act(stud, "add")                                   # straight to the server
        wait_for(lambda: env.server_queue() == ["tstud1"], 10, "the direct request to land")
        stud.evaluate("document.getElementById('outageswitch').style.display = 'unset'")
        stud.click("#outageswitch .switchCylinder")
        act(stud, "del")                                   # no mesh to fall back to
        wait_for(lambda: stud.evaluate("1") and any("Could not reach the server" in d for d in stud.dialogs), 15,
                 "the normal failure alert")
        assert env.server_queue() == ["tstud1"] and journal_size(stud) == 0
        stud.click("#outageswitch .switchCylinder")
        with stud.expect_navigation():
            stud.evaluate("toggleP2P()")
        wait_for(lambda: stud.evaluate("window.roomname") == env.room, 15, "the tab to reload into the room")
        mesh_up(staff, stud)
    finally:
        env.close()


# Every connection this browser makes fails (relay-only through a TURN server that doesn't exist)
UNREACHABLE = """(() => {
    const Orig = window.RTCPeerConnection;
    window.RTCPeerConnection = function (cfg) {
        return new Orig({iceServers: [{urls: "turn:192.0.2.1:3478", username: "x", credential: "y"}], iceTransportPolicy: "relay"});
    };
    window.RTCPeerConnection.prototype = Orig.prototype;
})();"""


def unreachable_host_is_retried_with_backoff(pw):
    """When connections can't form (no TURN reachable), a tab backs off instead of re-signaling every few seconds."""
    env = Env(pw)
    try:
        staff = env.page(STAFF)
        stud = env.page(STUD, init_script=UNREACHABLE)
        offers = []
        stud.on("request", lambda r: offers.append(time.time()) if "action=signalsend" in r.url and '"type":"offer"' in (r.post_data or "") else None)
        end = time.time() + 100
        flip = False
        while time.time() < end:                      # a room update every 5s, like a busy queue
            env.api.get(f"{BASE}/roomd.wsgi?action={'del' if flip else 'add'}&room={env.room}&queue={Q}&waitdata=x")
            flip = not flip
            pump(stud, 5)
        gaps = [b - a for a, b in zip(offers, offers[1:])]
        # attempts thin out (each wait about twice the last) despite an update every 5s
        assert 1 <= len(offers) <= 4 and all(b > a * 1.3 for a, b in zip(gaps, gaps[1:])), [round(t - offers[0]) for t in offers]
        assert stud.evaluate("window.connectFailures") >= 2
    finally:
        env.close()


def malicious_mesh_data_is_not_rendered(pw):
    """HTML smuggled in through the mesh (an envelope's waitdata, or a doctored room update) must never run."""
    env = Env(pw)
    try:
        staff, stud = env.page(STAFF), env.page(STUD)
        mesh_up(staff, stud)
        wait_for(lambda: relying(stud), 15, "student to rely on the staff host")
        evil = "<img src=x onerror=window.__pwned=1>"
        # 1. a student tab sends a self-signed-looking envelope with HTML in waitdata
        stud.evaluate("""(evil) => sendMeshAction({type: "offlineaction", id: crypto.randomUUID(), peerid: window.peerid,
            room: window.roomname, user: window.username, action: "uadd", queue: "%s", target: "", waitdata: evil,
            value: "", t: Math.round(serverNow()), seq: 1, sig: "x"})""" % Q, evil)
        # 2. a (compromised) host relays a doctored room update
        staff.evaluate("""(evil) => {
            const d = JSON.parse(JSON.stringify(window.serverJSON));
            d[window.roomname]["%s"] = [["tstud2", Date.now() / 1000, evil, 0, evil], [evil, 1, "x", 0, ""]];
            d.names = {"tstud2": evil}; d.servertime = (d.servertime || 0) + 1;
            Object.values(window.hostChannels).forEach(dc => dc.send(JSON.stringify({type: "payload", room: window.roomname, data: d})));
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


SCENARIOS = [full_cycle, originator_gone, reopened_tab_syncs,
             rejection_alert_only_for_originator, one_peer_offline_no_storage,
             storage_blocked_entirely, staff_actions_offline, forged_staff_actions_dropped_by_host,
             sse_consolidation, host_leaves, host_goes_quiet,
             slow_server_backlog_cannot_undo_later_actions, slow_sync_falls_back_to_offline,
             stale_pending_action_does_not_hide_newer_state, owner_host_heartbeats_for_its_peers,
             student_host_peers_heartbeat_themselves, refreshed_host_leaves_cleanly, crashed_host_is_waited_out,
             mesh_survives_host_change_while_writes_fail, rename_queue_while_relying_on_host,
             old_build_tab_is_never_elected, outage_switch_and_status_badges,
             crowded_join_is_not_rate_limited, host_signed_out, malicious_mesh_data_is_not_rendered,
             student_host_cannot_relay_room_state, signals_bound_to_their_owner,
             student_host_cannot_fake_staff_actions_downstream, idle_tab_pauses_and_keeps_its_place,
             p2p_toggle_falls_back_to_server, unreachable_host_is_retried_with_backoff]

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
                print(f"PASS {fn.__name__} ({time.time() - t0:.1f}s)")
            except Exception as e:
                failed += 1
                print(f"FAIL {fn.__name__} ({time.time() - t0:.1f}s): {type(e).__name__}: {e}")
    sys.exit(1 if failed else 0)
