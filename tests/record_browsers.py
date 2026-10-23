"""Record a Chrome and a Firefox session on a room for debugging (see RELAY.md, Testing)."""
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
  const Orig = window.WebSocket;
  let n = 0;
  // log the relay connection's life without its token
  window.WebSocket = function (url, protocols) {
    const ws = new Orig(url, protocols);
    const id = "ws" + (++n), t0 = Date.now(), host = new URL(url).host + new URL(url).pathname;
    console.log("[rec] " + id + " connecting to " + host);
    ws.addEventListener("open", () => console.log("[rec] " + id + " open after " + (Date.now() - t0) + "ms"));
    ws.addEventListener("close", (e) => console.log("[rec] " + id + " closed code=" + e.code + " after " + Math.round((Date.now() - t0) / 1000) + "s"));
    return ws;
  };
  window.WebSocket.prototype = Orig.prototype;
  ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(k => window.WebSocket[k] = Orig[k]);

  // A second connection to the relay with this tab's token: does this browser, on this network, reach it?
  window.__relayTest = () => new Promise((resolve) => {
    const info = window.relayInfo;
    if (!info) return resolve("no relay token yet (not joined, or relay off)");
    const t0 = Date.now();
    const ws = new Orig(info.url + "?token=" + encodeURIComponent(info.token));
    const done = (msg) => { try { ws.close(); } catch (e) {} resolve(msg); };
    const timer = setTimeout(() => done("FAILED: no answer in 10s from " + new URL(info.url).host), 10000);
    ws.onmessage = (m) => { clearTimeout(timer); const j = JSON.parse(m.data);
      done("OK: " + j.type + " in " + (Date.now() - t0) + "ms, " + j.count + " tabs in the room"); };
    ws.onclose = (e) => { clearTimeout(timer); done("FAILED: closed with code " + e.code + " after " + (Date.now() - t0) + "ms"); };
  });
})();
"""

STATE_JS = r"""
() => {
  if (typeof window.peerid === "undefined") return {loaded: false};
  const short = (x) => x ? String(x).slice(0, 8) : null;
  return {
    room: window.roomname || null,
    me: short(window.peerid),
    relay: window.relay ? ["connecting", "open", "closing", "closed"][window.relay.readyState] : null,
    others: typeof meshUserCount === "function" ? meshUserCount() : null,
    relying: !!window.relyingOnRelay,
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
                        res = await b["page"].evaluate("window.__relayTest()")
                    except Exception as e:
                        res = "error: " + str(e)[:120]
                    rec.log(b["label"], "NOTE", "relay self-test: " + res)
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
