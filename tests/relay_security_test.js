"use strict";
// Security tests for relay/server.js: run on verilog against a running relay, e.g.
//   cd ~/queup-relay && node /path/to/relay_security_test.js 8443
const fs = require("fs");
const path = require("path");
const https = require("https");
const crypto = require("crypto");
const WebSocket = require(path.join(process.cwd(), "node_modules", "ws"));

const PORT = Number(process.argv[2] || 8443);
const HOST = "verilog.ecn.purdue.edu";
const ORIGIN = "https://engineering.purdue.edu";
const secret = fs.readFileSync(path.join(process.cwd(), "relay_secret"), "utf8").trim();
const ROOM = "ZR" + Math.random().toString(36).slice(2, 5).toUpperCase().padEnd(3, "X");
let failures = 0;

function b64url(buf) { return buf.toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
function token(claims, key = secret) {
    const body = b64url(Buffer.from(JSON.stringify(claims)));
    return body + "." + b64url(crypto.createHmac("sha256", key).update(body).digest());
}
const now = () => Math.floor(Date.now() / 1000);
const claims = (u, o, extra = {}) => Object.assign({t: "queup-dev", r: ROOM, u, o, e: now() + 600}, extra);
const uuid = () => crypto.randomUUID ? crypto.randomUUID() : [8, 4, 4, 4, 12].map(n => crypto.randomBytes(n / 2).toString("hex")).join("-");

function check(name, ok) {
    console.log((ok ? "PASS " : "FAIL ") + name);
    if (!ok) failures++;
}

function publish(data, {sign = true, ts = now(), key = secret} = {}) {
    const body = JSON.stringify({tree: "queup-dev", room: ROOM, data});
    const headers = {"Content-Type": "application/json"};
    if (sign) {
        headers["X-Relay-Ts"] = String(ts);
        headers["X-Relay-Sig"] = crypto.createHmac("sha256", key).update(ts + "." + body).digest("hex");
    }
    return new Promise((resolve) => {
        const req = https.request({host: HOST, port: PORT, path: "/publish", method: "POST", headers}, (res) => { res.resume(); resolve(res.statusCode); });
        req.on("error", () => resolve(0));
        req.end(body);
    });
}

// resolves with {ws, messages} once open, or with the HTTP status if the upgrade is refused
function connect(tok, origin = ORIGIN) {
    return new Promise((resolve) => {
        const ws = new WebSocket(`wss://${HOST}:${PORT}/relay?token=${encodeURIComponent(tok)}`, {headers: {Origin: origin}});
        const messages = [];
        ws.on("message", (m) => messages.push(JSON.parse(m)));
        ws.on("open", () => resolve({ws, messages}));
        ws.on("unexpected-response", (req, res) => resolve({refused: res.statusCode}));
        ws.on("error", () => resolve({refused: -1}));
    });
}

const wait = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    // publishing room state
    check("unsigned publish is refused", await publish({x: 1}, {sign: false}) === 403);
    check("publish signed with the wrong key is refused", await publish({x: 1}, {key: "not-the-secret-not-the-secret-123"}) === 403);
    check("replayed (stale) publish is refused", await publish({x: 1}, {ts: now() - 600}) === 403);

    // joining a room
    check("connecting without a token is refused", (await connect("")).refused === 403);
    check("token signed with the wrong key is refused", (await connect(token(claims("tstud1", false), "forged-key-forged-key-forged-key"))).refused === 403);
    const good = token(claims("tstud1", false));
    const tampered = b64url(Buffer.from(JSON.stringify(claims("tstud1", true)))) + good.slice(good.indexOf("."));
    check("token edited to claim owner is refused", (await connect(tampered)).refused === 403);
    check("expired token is refused", (await connect(token(claims("tstud1", false, {e: now() - 5})))).refused === 403);
    check("connection from another site is refused", (await connect(good, "https://evil.example")).refused === 403);

    // genuine traffic
    const stud = await connect(good);
    const staff = await connect(token(claims("tstaff1", true)));
    const other = await connect(token(claims("tstud2", false)));
    check("genuine tokens connect", !!(stud.ws && staff.ws && other.ws));
    await wait(300);
    check("joining sends hello", stud.messages.some(m => m.type === "hello"));
    check("signed publish is accepted", await publish({[ROOM]: {default_queue: []}, servertime: now()}) === 204);
    await wait(300);
    check("published state reaches every tab", [stud, staff, other].every(c => c.messages.some(m => m.type === "room" && m.data[ROOM])));

    const env = (user, action, extra = {}) => Object.assign({id: uuid(), peerid: uuid(), room: ROOM, user, action, queue: "default_queue",
        target: "", waitdata: "", value: "", t: Date.now(), seq: 1, sig: "x"}, extra);
    const count = (c, id) => c.messages.filter(m => m.type === "envelope" && m.env.id === id).length;

    const own = env("tstud1", "uadd", {waitdata: "hi"});
    stud.ws.send(JSON.stringify({type: "envelope", env: own}));
    const asOther = env("tstud2", "udel");
    stud.ws.send(JSON.stringify({type: "envelope", env: asOther}));
    const studStaff = env("tstud1", "qclr");
    stud.ws.send(JSON.stringify({type: "envelope", env: studStaff}));
    const staffAction = env("tstaff1", "usdel", {target: "tstud1"});
    staff.ws.send(JSON.stringify({type: "envelope", env: staffAction}));
    await wait(500);
    check("a user's own action is relayed to others", count(other, own.id) === 1 && count(staff, own.id) === 1);
    check("the sender doesn't get its own action back", count(stud, own.id) === 0);
    check("relayed actions carry who sent them", other.messages.some(m => m.type === "envelope" && m.env.id === own.id && m.from.user === "tstud1" && m.from.owner === false));
    check("an action claiming another user is dropped", count(other, asOther.id) === 0 && count(staff, asOther.id) === 0);
    check("a staff action from a student is dropped", count(other, studStaff.id) === 0);
    check("a staff action from an owner is relayed", count(other, staffAction.id) === 1);

    const late = await connect(token(claims("tstud3", false)));
    await wait(300);
    const hello = late.messages.find(m => m.type === "hello");
    check("a tab joining later gets pending offline actions", !!hello && hello.pending.some(p => p.env.id === own.id));

    const before = stud.messages.filter(m => m.type === "room").length;
    await publish({[ROOM]: {default_queue: [["new"]]}, servertime: now() + 2});
    await publish({[ROOM]: {default_queue: [["old"]]}, servertime: now() + 1});
    await wait(300);
    const rooms = stud.messages.filter(m => m.type === "room").slice(before);
    const lateHello = (await connect(token(claims("tstud4", false)))).messages;
    await wait(300);
    check("an older publish arriving late is ignored", rooms.length === 1 && rooms[0].data[ROOM].default_queue[0][0] === "new");
    check("late joiners get the newest state", lateHello.find(m => m.type === "hello").data[ROOM].default_queue[0][0] === "new");

    // envelopes make every tab act, so each sender gets at most 30 a minute (this tab sent 3 above)
    const flood = [];
    for (let i = 0; i < 30; i++) flood.push(env("tstud1", "uadd"));
    for (const e of flood) stud.ws.send(JSON.stringify({type: "envelope", env: e}));
    await wait(500);
    const through = flood.filter(e => count(other, e.id) === 1).length;
    check("a sender's envelopes are capped at 30 a minute (" + through + " of 30 got through)", through === 30 - 3);

    check("room deletion is accepted", await publish({}) === 204);
    for (const c of [stud, staff, other, late]) c.ws.close();
    console.log(failures ? failures + " FAILED" : "all passed");
    process.exit(failures ? 1 : 0);
})();
