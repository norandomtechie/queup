"""Record a Chrome and a Firefox session on a room for debugging (see P2P.md, Testing)."""
import argparse
import asyncio
import datetime
import getpass
import json
import sys

from playwright.async_api import async_playwright

# runs before the page's own scripts
INIT_SCRIPT = r"""
(() => {
  const Orig = window.RTCPeerConnection;
  if (!Orig) return;
  let n = 0;
  // describe a candidate without its actual address
  const kind = (c) => {
    const a = c.address || c.ip || "";
    const fam = a.endsWith(".local") ? "mdns" : a.includes(":") ? "ipv6" : a ? "ipv4" : "?";
    return (c.candidateType || c.type) + "/" + (c.protocol || "?") + "/" + fam +
      (c.relayProtocol ? "(relay-" + c.relayProtocol + ")" : "");
  };
  async function describePath(pc, id) {
    try {
      const stats = await pc.getStats();
      const byId = {}; stats.forEach(s => byId[s.id] = s);
      let pair = null; const states = {};
      stats.forEach(s => {
        if (s.type === "candidate-pair") {
          states[s.state] = (states[s.state] || 0) + 1;
          if (s.nominated && s.state === "succeeded") pair = s;
        }
      });
      const local = [], remote = [];
      stats.forEach(s => { if (s.type === "local-candidate") local.push(kind(s)); if (s.type === "remote-candidate") remote.push(kind(s)); });
      console.log("[rec] " + id + " candidates local=[" + [...new Set(local)].join(" ") + "] remote=[" + [...new Set(remote)].join(" ") +
        "] pairs=" + JSON.stringify(states) +
        (pair ? " via " + kind(byId[pair.localCandidateId] || {}) + " -> " + kind(byId[pair.remoteCandidateId] || {}) : " (no working pair)"));
    } catch (e) { console.log("[rec] " + id + " stats failed: " + e); }
  }
  window.RTCPeerConnection = function (...args) {
    const pc = new Orig(...args);
    const id = "pc" + (++n);
    const cfg = args[0] || {};
    const servers = (cfg.iceServers || []).map(s => [].concat(s.urls).join(",")).join(" ");
    console.log("[rec] " + id + " created" + (cfg.iceTransportPolicy === "relay" ? " (relay-only test)" : "") + " servers=" + servers);
    pc.addEventListener("connectionstatechange", () => {
      console.log("[rec] " + id + " connection=" + pc.connectionState);
      if (["connected", "failed"].includes(pc.connectionState)) describePath(pc, id);
    });
    pc.addEventListener("iceconnectionstatechange", () => console.log("[rec] " + id + " ice=" + pc.iceConnectionState));
    pc.addEventListener("icegatheringstatechange", () => { if (pc.iceGatheringState === "complete") describePath(pc, id); });
    return pc;
  };
  window.RTCPeerConnection.prototype = Orig.prototype;

  // Two connections in this page that may only use the TURN relay: they
  // connect only if this browser, on this network, can actually use it.
  window.__turnTest = async () => {
    const servers = window.iceServers;
    if (!servers) return "no iceServers yet (not joined?)";
    const results = [];
    for (const transport of ["udp", "tcp"]) {
      const only = servers.filter(s => [].concat(s.urls).some(u => u.startsWith("turn:") &&
        (transport === "tcp" ? u.includes("transport=tcp") : !u.includes("transport=tcp"))));
      if (!only.length) { results.push(transport + ": no such server"); continue; }
      const a = new RTCPeerConnection({iceServers: only, iceTransportPolicy: "relay"});
      const b = new RTCPeerConnection({iceServers: only, iceTransportPolicy: "relay"});
      let relays = 0;
      a.onicecandidate = e => { if (e.candidate) { relays++; b.addIceCandidate(e.candidate).catch(() => {}); } };
      b.onicecandidate = e => { if (e.candidate) a.addIceCandidate(e.candidate).catch(() => {}); };
      const dc = a.createDataChannel("t");
      const opened = new Promise(res => { dc.onopen = () => res(true); setTimeout(() => res(false), 10000); });
      await a.setLocalDescription(await a.createOffer());
      await b.setRemoteDescription(a.localDescription);
      await b.setLocalDescription(await b.createAnswer());
      await a.setRemoteDescription(b.localDescription);
      const ok = await opened;
      results.push(transport + ": " + (ok ? "OK" : "FAILED") + " (" + relays + " relay candidates)");
      a.close(); b.close();
    }
    return results.join("; ");
  };
})();
"""

STATE_JS = r"""
() => {
  if (typeof window.peerid === "undefined") return {loaded: false};
  const short = (x) => x ? String(x).slice(0, 8) : null;
  const links = typeof openLinks === "function" ? openLinks().map(l => (l.upstream ? "up:" : "down:") + short(l.peerid)) : [];
  return {
    room: window.roomname || null,
    me: short(window.peerid),
    host: window.currentHost ? short(window.currentHost.peerid) + (window.currentHost.owner ? "*" : "") : null,
    amIHost: !!window.amIHost,
    links: links,
    pending: window.pendingConn ? short(window.pendingConn.hostId) : null,
    peers: (window.knownPeers || []).map(p => short(p.peerid) + (p.owner ? "*" : "")),
    relying: !!window.relyingOnHost,
    ownSSE: window.evtSource ? ["connecting", "open", "closed"][window.evtSource.readyState] : null,
    sseDown: window.sseDownSince !== null && window.sseDownSince !== undefined,
    outage: !!window.confirmedOutage,
    badge: document.documentElement.getAttribute("offlinemode"),
    journal: window.journal ? Object.values(window.journal).map(x => x.env.action + (x.acked ? "(acked)" : "")).join(",") : "",
  };
}
"""


class Recorder:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")
        self.path = path

    def log(self, who, kind, text):
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"{ts} [{who}] {kind}: {text}"
        self.f.write(line + "\n")
        self.f.flush()
        if kind in ("NOTE", "ERROR", "BLOCK", "STATE") or "[rec]" in text or "fail" in text.lower():
            print(line)


async def open_browser(pw, label, launcher, base, room, creds, rec):
    browser = await launcher()
    ctx = await browser.new_context(http_credentials=creds, viewport=None)
    await ctx.add_init_script(INIT_SCRIPT)
    page = await ctx.new_page()
    page.on("console", lambda m: rec.log(label, "console." + m.type, m.text[:500]))
    page.on("pageerror", lambda e: rec.log(label, "ERROR", str(e)[:500]))
    page.on("requestfailed", lambda r: rec.log(label, "reqfail", f"{r.method} {r.url[:160]} ({r.failure})")
            if "engineering.purdue.edu" in r.url else None)
    page.on("dialog", lambda d: rec.log(label, "dialog." + d.type, d.message[:300]))   # left for you to answer
    await page.goto(f"{base}/?room={room}")
    return {"label": label, "browser": browser, "ctx": ctx, "page": page, "blocked": False, "last": None}


async def snapshot(b, rec, force=False):
    try:
        state = await b["page"].evaluate(STATE_JS)
    except Exception as e:
        state = {"unavailable": str(e)[:80]}
    text = json.dumps(state, separators=(",", ":"))
    if force or text != b["last"]:
        b["last"] = text
        rec.log(b["label"], "STATE", text)


async def set_blocked(b, blocked, rec, cut_sse=False):
    async def abort(route):
        await route.abort()
    if blocked and not b["blocked"]:
        await b["ctx"].route("**/*", abort)
    elif not blocked and b["blocked"]:
        await b["ctx"].unroute("**/*")
    b["blocked"] = blocked
    if cut_sse:
        await b["page"].evaluate("if (window.evtSource) { window.evtSource.close(); createEventSource(); }")
    rec.log(b["label"], "BLOCK", ("blocked" if blocked else "unblocked") + (" + cut open SSE" if cut_sse else ""))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("room")
    ap.add_argument("--base", default="https://engineering.purdue.edu/~menon18/queup-dev")
    ap.add_argument("--log", default=None)
    args = ap.parse_args()
    path = args.log or datetime.datetime.now().strftime("queup-browsers-%Y%m%d-%H%M%S.log")
    rec = Recorder(path)

    creds = []
    for name in ("Chrome", "Firefox"):
        user = input(f"{name} username: ").strip()
        creds.append({"username": user, "password": getpass.getpass(f"{name} password: ")})

    async with async_playwright() as pw:
        async def chrome():
            try:
                return await pw.chromium.launch(channel="chrome", headless=False)
            except Exception:
                return await pw.chromium.launch(headless=False)
        async def firefox():
            return await pw.firefox.launch(headless=False)

        browsers = [
            await open_browser(pw, "chrome", chrome, args.base, args.room, creds[0], rec),
            await open_browser(pw, "firefox", firefox, args.base, args.room, creds[1], rec),
        ]
        rec.log("rec", "NOTE", f"recording to {path}; room {args.room}")
        print(f"\nRecording to {path}. Commands: b1/u1 b2/u2 k1/k2 t s n <text> q\n")

        async def poll():
            while True:
                for b in browsers:
                    await snapshot(b, rec)
                await asyncio.sleep(2)
        poller = asyncio.create_task(poll())

        loop = asyncio.get_running_loop()
        while True:
            cmd = (await loop.run_in_executor(None, sys.stdin.readline)).strip()
            if not cmd:
                continue
            if cmd == "q":
                break
            if cmd == "t":
                for b in browsers:
                    try:
                        res = await b["page"].evaluate("window.__turnTest()")
                    except Exception as e:
                        res = "error: " + str(e)[:120]
                    rec.log(b["label"], "NOTE", "TURN self-test: " + res)
                continue
            if cmd == "s":
                for b in browsers:
                    await snapshot(b, rec, force=True)
            elif cmd.startswith("n "):
                rec.log("you", "NOTE", cmd[2:])
            elif len(cmd) == 2 and cmd[0] in "buk" and cmd[1] in "12":
                b = browsers[int(cmd[1]) - 1]
                await set_blocked(b, cmd[0] != "u", rec, cut_sse=(cmd[0] == "k"))
            else:
                print("commands: b1/u1 b2/u2 k1/k2 t s n <text> q")
        poller.cancel()
        for b in browsers:
            await b["browser"].close()
    print(f"Log written to {path}")


if __name__ == "__main__":
    asyncio.run(main())
