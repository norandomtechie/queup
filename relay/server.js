"use strict";
// QueUp relay: pushes live room updates to browsers and passes signed offline actions between them (see RELAY.md)
const fs = require("fs");
const path = require("path");
const https = require("https");
const crypto = require("crypto");
const { WebSocketServer } = require("ws");

const CERT_DIR = process.env.RELAY_CERT_DIR || "/etc/letsencrypt/live/verilog.ecn.purdue.edu";
const PORT = Number(process.env.RELAY_PORT || 8443);
const SECRET_FILE = process.env.RELAY_SECRET_FILE || path.join(__dirname, "relay_secret");
const TREES = new Set((process.env.RELAY_TREES || "queup,queup-dev").split(","));
const ORIGINS = new Set((process.env.RELAY_ORIGINS || "https://engineering.purdue.edu").split(","));

const MAX_MESSAGE = 16 * 1024;          // largest message a browser may send
const MAX_PUBLISH = 4 * 1024 * 1024;    // largest room update roomd may publish
const MAX_TABS_PER_USER = 10;           // per room
const PENDING_MAX = 1000;               // offline actions kept per room for late joiners
const PENDING_TTL_MS = 3 * 3600 * 1000; // same retention as offline sync
const RATE_WINDOW_MS = 10000, RATE_MAX = 40;
// every tab in the room acts on a relayed envelope, so senders get far fewer of them than other messages
const ENVELOPE_WINDOW_MS = 60000, ENVELOPE_MAX = 30;
const PING_MS = 20000;
const PUBLISH_SKEW_S = 120;
const OUTAGE_HINT_MS = 5000;            // at most one outage hint per room this often
const STAFF_ACTIONS = new Set(["usdel", "qmrk", "qclr"]);
const ACTIONS = new Set(["uadd", "udel", "usdel", "qmrk", "qclr"]);
const UUID = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

const secret = fs.readFileSync(SECRET_FILE, "utf8").trim();
if (secret.length < 32) throw new Error("relay secret too short");

const rooms = new Map();   // "tree:room" -> {clients: Set, snapshot: string|null, pending: [], seen: Set, presenceTimer}

function log(...a) { console.log(new Date().toISOString(), ...a); }

function hmac(data) { return crypto.createHmac("sha256", secret).update(data).digest(); }

function safeEqual(a, b) { return a.length === b.length && crypto.timingSafeEqual(a, b); }

// Node 14 has no "base64url" encoding
function fromB64url(s) { return Buffer.from(s.replace(/-/g, "+").replace(/_/g, "/"), "base64"); }

// token = base64url(json {t: tree, r: room, u: user, o: owner, e: expiry}) + "." + base64url(hmac)
function verifyToken(token) {
    if (typeof token !== "string" || token.length > 1024) return null;
    const dot = token.indexOf(".");
    if (dot < 1) return null;
    const body = token.slice(0, dot), sig = fromB64url(token.slice(dot + 1));
    if (!safeEqual(sig, hmac(body))) return null;
    let c;
    try { c = JSON.parse(fromB64url(body).toString()); } catch (e) { return null; }
    if (!c || !TREES.has(c.t) || !/^[A-Z0-9]{5}$/.test(c.r) || !/^[a-z0-9]{2,8}$/.test(c.u)) return null;
    if (typeof c.e !== "number" || c.e < Date.now() / 1000) return null;
    return {tree: c.t, room: c.r, user: c.u, owner: !!c.o};
}

function roomFor(key) {
    let r = rooms.get(key);
    if (!r) {
        r = {clients: new Set(), snapshot: null, pending: [], seen: new Set(), presenceTimer: null};
        rooms.set(key, r);
    }
    return r;
}

function send(ws, obj) {
    if (ws.readyState === ws.OPEN) ws.send(typeof obj === "string" ? obj : JSON.stringify(obj));
}

function broadcast(r, obj, except) {
    const text = JSON.stringify(obj);
    for (const ws of r.clients) if (ws !== except) send(ws, text);
}

// at most one presence update per room every 2s
function schedulePresence(key) {
    const r = rooms.get(key);
    if (!r || r.presenceTimer) return;
    r.presenceTimer = setTimeout(() => {
        r.presenceTimer = null;
        broadcast(r, {type: "presence", count: r.clients.size});
        if (r.clients.size === 0 && r.pending.length === 0 && r.snapshot === null) rooms.delete(key);
    }, 2000);
}

function prunePending(r) {
    const cutoff = Date.now() - PENDING_TTL_MS;
    r.pending = r.pending.filter(p => p.at >= cutoff).slice(-PENDING_MAX);
    if (r.seen.size > 4 * PENDING_MAX) r.seen = new Set(r.pending.map(p => p.env.id));
}

// an offline action may only be relayed by the user it belongs to, and staff actions only by an owner
function acceptEnvelope(meta, env) {
    return env && typeof env === "object" && typeof env.id === "string" && UUID.test(env.id) &&
        env.room === meta.room && env.user === meta.user && ACTIONS.has(env.action) &&
        (!STAFF_ACTIONS.has(env.action) || meta.owner);
}

function onClientMessage(ws, raw) {
    const now = Date.now();
    if (now - ws.rate.start > RATE_WINDOW_MS) ws.rate = {start: now, n: 0};
    if (++ws.rate.n > RATE_MAX) return ws.close(1008, "rate limit");
    let msg;
    try { msg = JSON.parse(raw); } catch (e) { return; }
    if (!msg || typeof msg !== "object") return;
    const r = rooms.get(ws.meta.key);
    if (!r) return;
    if (msg.type === "envelope") {
        const env = msg.env;
        if (now - ws.envRate.start > ENVELOPE_WINDOW_MS) ws.envRate = {start: now, n: 0};
        if (++ws.envRate.n > ENVELOPE_MAX) return;
        if (!acceptEnvelope(ws.meta, env) || r.seen.has(env.id)) return;
        r.seen.add(env.id);
        r.pending.push({env, at: now, from: {user: ws.meta.user, owner: ws.meta.owner}});
        prunePending(r);
        broadcast(r, {type: "envelope", env, from: {user: ws.meta.user, owner: ws.meta.owner}}, ws);
    }
    else if (msg.type === "outage") {
        // unauthenticated hint: tabs only check the server themselves when they get it
        if (now - (r.lastOutageHint || 0) < OUTAGE_HINT_MS) return;
        r.lastOutageHint = now;
        broadcast(r, {type: "outage"}, ws);
    }
    else if (msg.type === "ping") {
        send(ws, {type: "pong"});
    }
}

function onConnection(ws, meta) {
    const r = roomFor(meta.key);
    let mine = 0;
    for (const c of r.clients) if (c.meta.user === meta.user) mine++;
    if (mine >= MAX_TABS_PER_USER) return ws.close(1008, "too many tabs");
    ws.meta = meta;
    ws.rate = {start: Date.now(), n: 0};
    ws.envRate = {start: Date.now(), n: 0};
    ws.alive = true;
    r.clients.add(ws);
    prunePending(r);
    send(ws, {
        type: "hello",
        count: r.clients.size,
        data: r.snapshot ? JSON.parse(r.snapshot) : null,
        pending: r.pending.map(p => ({env: p.env, from: p.from}))
    });
    schedulePresence(meta.key);
    ws.on("message", (raw) => onClientMessage(ws, raw));
    ws.on("pong", () => { ws.alive = true; });
    ws.on("close", () => { r.clients.delete(ws); schedulePresence(meta.key); });
    ws.on("error", () => {});
}

// roomd publishes each room change: body {tree, room, data}, headers X-Relay-Ts + X-Relay-Sig (hmac of "ts.body")
function onPublish(req, res) {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
        size += c.length;
        if (size > MAX_PUBLISH) { res.writeHead(413).end(); req.destroy(); return; }
        chunks.push(c);
    });
    req.on("end", () => {
        if (res.writableEnded) return;
        const body = Buffer.concat(chunks).toString();
        const ts = Number(req.headers["x-relay-ts"]);
        const sig = Buffer.from(String(req.headers["x-relay-sig"] || ""), "hex");
        if (!ts || Math.abs(Date.now() / 1000 - ts) > PUBLISH_SKEW_S || !safeEqual(sig, hmac(ts + "." + body))) {
            res.writeHead(403).end();
            return;
        }
        let p;
        try { p = JSON.parse(body); } catch (e) { res.writeHead(400).end(); return; }
        if (!p || !TREES.has(p.tree) || !/^[A-Z0-9]{5}$/.test(p.room) || typeof p.data !== "object") {
            res.writeHead(400).end();
            return;
        }
        const key = p.tree + ":" + p.room;
        const r = roomFor(key);
        const deleted = Object.keys(p.data).length === 0;
        // writes in different Apache workers can publish out of order: keep the newest state
        if (!deleted && r.snapshot && typeof p.data.servertime === "number" && p.data.servertime < r.snapshotTime) {
            res.writeHead(204).end();
            return;
        }
        r.snapshotTime = deleted ? 0 : (p.data.servertime || 0);
        // the server is up and authoritative again: tabs keep their own journals until they sync
        r.pending = [];
        r.snapshot = deleted ? null : JSON.stringify(p.data);
        broadcast(r, {type: "room", data: p.data});
        if (deleted) schedulePresence(key);
        res.writeHead(204).end();
    });
}

function readTls() {
    return {key: fs.readFileSync(path.join(CERT_DIR, "privkey.pem")), cert: fs.readFileSync(path.join(CERT_DIR, "fullchain.pem"))};
}

const server = https.createServer(readTls(), (req, res) => {
    const url = new URL(req.url, "https://relay");
    if (req.method === "POST" && url.pathname === "/publish") return onPublish(req, res);
    if (req.method === "GET" && url.pathname === "/health") {
        let tabs = 0;
        for (const r of rooms.values()) tabs += r.clients.size;
        res.writeHead(200, {"Content-Type": "text/plain"}).end("ok rooms=" + rooms.size + " tabs=" + tabs + "\n");
        return;
    }
    res.writeHead(404).end();
});

const wss = new WebSocketServer({noServer: true, maxPayload: MAX_MESSAGE, perMessageDeflate: false});

server.on("upgrade", (req, socket, head) => {
    const url = new URL(req.url, "https://relay");
    const meta = url.pathname === "/relay" ? verifyToken(url.searchParams.get("token")) : null;
    if (!meta || !ORIGINS.has(req.headers.origin)) {
        socket.write("HTTP/1.1 403 Forbidden\r\n\r\n");
        socket.destroy();
        return;
    }
    meta.key = meta.tree + ":" + meta.room;
    wss.handleUpgrade(req, socket, head, (ws) => onConnection(ws, meta));
});

// drop connections that stop answering pings
setInterval(() => {
    for (const ws of wss.clients) {
        if (!ws.alive) { ws.terminate(); continue; }
        ws.alive = false;
        ws.ping();
    }
}, PING_MS);

// pick up renewed certificates without a restart
let certMtime = 0;
setInterval(() => {
    try {
        const m = fs.statSync(path.join(CERT_DIR, "fullchain.pem")).mtimeMs;
        if (certMtime && m !== certMtime) { server.setSecureContext(readTls()); log("reloaded certificate"); }
        certMtime = m;
    } catch (e) { log("certificate check failed:", e.message); }
}, 6 * 3600 * 1000);

setInterval(() => {
    let tabs = 0;
    for (const r of rooms.values()) tabs += r.clients.size;
    log("rooms", rooms.size, "tabs", tabs);
}, 10 * 60 * 1000);

server.listen(PORT, () => log("queup relay listening on", PORT));
process.on("SIGTERM", () => { log("stopping"); server.close(); for (const ws of wss.clients) ws.close(1001); process.exit(0); });
