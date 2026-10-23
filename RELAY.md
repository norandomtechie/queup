# Relay and offline mode

QueUp rooms keep working when the server (engineering.purdue.edu) is down or slow. Every open room page also connects to a small WebSocket relay on verilog.ecn.purdue.edu. While the server is up, the relay delivers live room updates, so pages don't each hold an SSE connection on the web server. If the server stops answering, pages pass queue actions to each other through the relay and send everything back to the server when it returns.

This document explains how that works, what it needs on the servers, and how to test it.

## What users see

A status badge next to the room name:

| Badge | Meaning |
|---|---|
| 🟢 Syncing with N users | Server up; N other tabs in the room are on the relay |
| 🟠 Syncing with N users, offline. | Server unreachable; the queue keeps working between tabs on the relay |
| ⏳ Syncing offline changes… | Server is back; changes made while it was down are being sent to it |
| 🔴 Server disconnected | Server unreachable and no other tabs on the relay |
| ⚠️ Signed out — reload to sign in again | The browser stopped sending the user's login (401) |

Clicking the badge explains the current state. With the server up and nobody else in the room, no badge is shown.

**Works while the server is down:**
- joining and leaving a queue
- staff removing a user, marking/unmarking, and clearing a queue

**Online-only:** locking, queue create/rename/delete, subtitle, owners, cooldown, single-queue, timer, broadcasts, closing the room. These show "Could not reach the server" during an outage.

The page itself is served by the server, so it can't be loaded or reloaded while the server is down. Tabs that are already open keep working.

**Idle tabs pause.** After an hour with no queue changes and no clicks or typing on the page (and no exam timer running), a tab closes its server and relay connections and shows a banner with a Reconnect button. It stays in any queue it's in.

## How it fits together

```
 browser ──HTTPS──▶ engineering.purdue.edu  roomd.wsgi ──signed POST /publish──▶ relay (verilog:8443)
    ▲                                                                              │
    └──────────── wss://verilog.ecn.purdue.edu/queup-relay                       ◀─┘
```

1. **Joining.** On joining a room, each tab picks a random peer id and calls `peerjoin`. The response carries:
   - a relay URL and token, signed by roomd for this user, room and deployment (6 hours)
   - a per-tab signing key (for offline actions)
   - the server time (for clock correction)
2. **Connecting.** The tab opens its own SSE stream as before, then connects to the relay. The relay answers with `hello`: the room's latest state, how many tabs are in the room, and any offline actions still waiting from an outage.
3. **Live updates.** After every successful write, roomd posts the room's new state to the relay, which pushes it to every tab in that room. Once a tab has received room state from the relay it closes its own SSE stream. Its own actions still go to roomd over HTTP.
4. **Falling back.** If the relay connection drops, the tab reopens its own SSE stream at once and reconnects to the relay in the background (1s, doubling to 60s, with jitter). A browser that can't reach verilog at all simply keeps using SSE.
5. **Staying correct.** roomd's push to the relay can be lost (a timeout, a restart). A tab on the relay therefore re-checks the server (`chk&full=1`):
   - after its own write, if no update followed within 2.5s;
   - when a queue action is rejected with 400, which usually means its view was out of date;
   - every 2 minutes.

   The relay keeps only the newest state per room, so two writes racing in different Apache workers can't leave it with an older one.
6. **Leaving.** A closing or reloading tab sends `peerleave` with a keepalive fetch and closes its relay connection.

Rooms of different deployments (`queup`, `queup-dev`) never mix: the deployment is part of every token and publish.

## Detecting an outage

The page treats the server as unreachable when any of these happen:
- **SSE fails twice in a row** (tabs not on the relay). `sseupdate` closes the stream every 60s on purpose, so one failure gets a quick retry first.
- **A queue action fails.** One that just gets no answer within 2s goes over the relay, but doesn't by itself put the tab in offline mode.
- **One of the page's own requests fails** (a queue action or sync).
- **A sync gets no answer within 5s.**
- **Another tab says so.** A tab on the relay has no SSE stream to notice an outage, so the first tab that confirms one sends an `outage` hint through the relay (at most one per room every 5s). The hint isn't trusted: each tab that gets it pings the server itself and goes offline only if its own ping fails. Receiving an offline action over the relay triggers the same check.

**Recovery.** While down, the page pings `ping.txt`, a static file that runs no Python. It starts every 5s and backs off to every 30s. A tab on the relay recovers as soon as a ping succeeds; one without the relay reopens SSE. A room update arriving over the relay makes a tab ping right away, rather than assuming it can reach the server too.

**Signed out.** A 401 means the browser stopped sending the login, not that the server is down. The tab shows "Signed out" and stays on the relay; its own actions are synced by other tabs.

## Offline actions and sync

**Envelopes.** During an outage, an action is applied locally and sent through the relay as an *envelope*, carrying:
- a unique id and the originating peer id
- room, user, action, queue, plus a target user and a value for staff actions
- the click time, corrected to server time
- a per-tab sequence number
- an HMAC-SHA256 signature made with the tab's signing key

**Journal.** Every tab stores every envelope it sees in a journal, in `localStorage` under its own key (in memory if storage is unavailable). The room is drawn as the last server state with the journal applied on top. A tab reopened later adopts journals left by closed tabs of the same room. The relay also keeps an outage's envelopes (up to 1000 per room, 3 hours) for tabs that connect later, and forgets them once roomd publishes again.

**Sync.** When the server is back:
- A tab with actions of its own sends its whole journal to `offlinesync` immediately.
- A tab holding only other tabs' actions waits 5–15s. Their senders usually sync first; once a room update shows the server's watermark at or past an action's click time, the copy is dropped instead of sent.
- Resending is always safe.
- A user whose own action was rejected gets one alert explaining why, from the server's answer (a resubmitted rejection repeats its reason). Relayed rejections are silent.

**Server-side checks** (`lib/offlinesync.py`). For each envelope, the server:
1. Checks the signature against the key it issued for that peer id. The peer id must belong to the claimed user, so nobody can submit actions for someone else.
2. Checks the time: not before the key was issued, not in the future, and within the last 3 hours.
3. Rejects staff actions from anyone who isn't an owner *at sync time*.
4. Applies everything in the batch in click-time order.

**Conflict rules:**
- **Newest change wins** per user and queue. Every change, online or offline, records its click time in `qwatermark<ROOM>`, and an older action arriving later is `superseded`. Queue-wide changes (clear, rename, delete) record a queue-level time.
- **Room rules first.** Lock, cooldown and single-queue are checked before membership. A rejected join never happened, so it doesn't override anything.
- **Redundant actions still count.** Joining when already in, or leaving when already out, records the time, so older actions arriving later can't undo the user's last intent. This applies online too.
- **Placement.** Offline joins are placed by click time, but never ahead of anyone already in the queue.
- **Clears and marks.** A clear removes only entries that existed when it was clicked. Marks are sent as marked/unmarked values, not toggles, so replaying one can't flip it back.
- **Recorded once.** Every decided envelope id is kept in `offlineapplied<ROOM>` as `user:id`, so resubmitting gives `duplicate` (or the original rejection). Online requests record their operation id the same way, under the authenticated user, so nobody can mark another user's action as already decided.

**Slow servers.** Online queue requests also carry the click time and an operation id (`t`, `opid`). The id is the same one used by the action's relayed copy, if it was relayed after 2s. This means:
- a request a hung server processes late can't undo something the user did afterwards;
- an action can't be applied twice.

## Security

**Joining the relay.** Only roomd can mint relay tokens: `base64url(claims) + "." + base64url(HMAC-SHA256)` with the shared secret, where the claims are deployment, room, user, owner flag and expiry. The relay refuses (403) a missing, altered, expired or wrongly signed token, a deployment it doesn't serve, and any `Origin` other than `https://engineering.purdue.edu`.

**Publishing room state.** Only roomd can publish: `POST /publish` must carry `X-Relay-Ts` (within 120s) and `X-Relay-Sig`, the HMAC of `ts.body`. Tabs can't send room state at all; the relay only accepts `envelope`, `outage` and `ping` messages from them.

**Envelopes.** The relay passes on an envelope only if it's for the token's room and user, and staff actions (`usdel`, `qmrk`, `qclr`) only from an owner's token. Each relayed envelope says which user and owner flag sent it, and tabs drop any that don't match. Every field is checked against the server's formats before use.

**Room updates** from the relay are rebuilt from validated fields before rendering. Usernames, queue names and waitdata must match the server's formats, and full names are HTML-escaped.

**Limits.** 16 KB per message, 40 messages per 10s per connection, 10 tabs per user per room.

The server re-verifies every signature at sync time, so a misbehaving tab can at worst affect what others see until the next real update.

## Server reference

**`roomd.wsgi` actions.** All are open to any room participant:

| Action | Purpose |
|---|---|
| `peerjoin` | Join; returns relay URL and token, signing key, server time |
| `peerleave` | Leave |
| `offlinesync` | POST `{"envelopes": [...]}`; returns `{"results": {id: status}, "servertime"}` |
| `chk&full=1` | The room payload the relay and SSE carry (queues, watermarks, server time) |

Every successful write (`add`, `del`, `ren`, `own`, `delown`, `setcool`, `setsub`, `lock`, `unlock`, `clear`, `mark`, `setperm`, `tgl1q`, `timer`, `broadcast`, `offlinesync`) is published to the relay; a deleted room is published as `{}`. A failed publish is retried once and logged to Apache's error log as `relay publish to ... failed`. After three failures in a row a worker skips publishing for 10s.

**Redis keys per room.** All are deleted with the room:

| Key | Contents |
|---|---|
| `peers<ROOM>`, `peermeta<ROOM>` | which peer ids joined, as which user |
| `peerkeys<ROOM>` | hash: peer id → signing key, user, issue time |
| `offlineapplied<ROOM>` | set of decided `user:id` envelope / operation ids (24h) |
| `offlinestatus<ROOM>` | hash: `user:id` → how it was decided (24h) |
| `qwatermark<ROOM>` | hash: `user\|queue`, `*\|queue`, `m:user\|queue` → latest change time |
| `lastadd<ROOM>` | hash: user → last self-join time (cooldown is measured from it) |

**SSE** (tabs not on the relay). Each update carries `watermarks` and `servertime`. The stream sends a keepalive comment every 5s, because the load balancer in front of Apache cuts connections idle for about 10s. A client disconnect closes the stream normally instead of killing the worker.

**Rate limits** (`lib/ratelimiter.py`). These are per-user fixed windows. A window runs out on time; rejected requests don't extend it.
- **`action`:** 3 per second, for clicks and room requests.
- **`mesh`:** 60 per 10 seconds, for SSE, presence and sync.

Usernames listed one per line in `<private>/ratelimit_bypass` are exempt. The file is optional and not tracked; production should not have one.

**Log codes:**

| Code | Meaning |
|---|---|
| `ouadd`, `oudel` | Self join/leave while offline |
| `ousdel`, `oqmrk`, `oqclr` | Staff remove/mark/clear while offline |
| `orej` | Offline action rejected at sync, with the reason |

Offline rows carry the time the action happened and the user whose browser synced it. The admin log sorts by time.

## Deployment

**Private folder.** Each deployment's private directory (`private/queup/`, `private/queup-dev/`) needs, all ignored by git and mode 600:
- `relay_url`: what browsers connect to, `wss://verilog.ecn.purdue.edu/queup-relay`
- `relay_publish_url`: where roomd publishes, `https://verilog.ecn.purdue.edu:8443/publish` (on campus, so the port is reachable)
- `relay_secret`: the shared secret, the same file as on the relay

Without `relay_url` and `relay_secret` the relay is simply off: tabs use SSE as before.

**The relay** (`relay/` in the private repo) runs on verilog as the `queup-relay` systemd service: Node.js with `ws`, user `ece270`, port 8443, from `/home/ece270/queup-relay` (with its own `relay_secret`). It reads the Let's Encrypt certificate and picks up a renewed one within 6 hours without a restart. `GET /health` reports room and tab counts. To install or update:

```
cd /home/ece270/queup-relay && npm install          # once
sudo cp queup-relay.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now queup-relay              # after updating server.js: sudo systemctl restart queup-relay
```

**Port 443.** Off campus only port 443 on verilog is reachable, and it belongs to the ECE 270 simulator. The simulator's `cluster.js` hands upgrade requests for `/queup-relay` to the relay before its own session check, through its existing `http-proxy`. The relay still does all the checking; the simulator never sees a QueUp login.

**Redis.** Needs 6.2 or newer (the code uses `SMISMEMBER`). The current server runs 8.10.2.

**Reloading Python changes.** After changing anything under `lib/`, touch `roomd.wsgi` and `admin/admin.wsgi`. They drop cached `lib` modules when they reload.

**Open pages update themselves.** Every room update carries a `build` stamp: `app.html`'s modification time. A page remembers the stamp from its first room check, and when a later update carries a different one, it reloads itself at a random moment within a minute, so tabs don't all come back at once. While someone is typing, the page is offline, or it holds unsynced offline actions, it shows a "QueUp has been updated" banner instead and tries again every 30s. If a reload still brings back the old page (a cached copy), it keeps the banner, which asks for a hard refresh, rather than reloading in a loop. Deploys that don't change `app.html` don't reload anything. Pages from before this feature (before 2026-10-23) don't have it.

**Announcing to every room.** `utils/announce.py` in the private repo sends a message to rooms as a staff broadcast, which every page version shows as an alert ("'message' from user."). Run it on charlotte, where the Redis socket is:

```
cd /web/groups/menon18/private/queup/utils
python3 announce.py --dry-run "QueUp will be down at 9pm."   # list the rooms it would reach
python3 announce.py "QueUp will be down at 9pm."             # rooms used in the last 12 hours, after confirming
```

`--rooms A,B` or `--all` pick other rooms, `--hours N` changes "recently used", and `--tree queup-dev` targets dev. Each room's log records it as `rbcast`; tabs on SSE get it from the write, tabs on the relay from a publish.

## Testing

The tests live in the private repo, under `private/queup-dev/tests/`, so they aren't served with the site. Paths below are relative to `private/queup-dev/`.

### Simulate outage switch

Every room page has a hidden "Simulate outage" switch at the right of the room title. To use it, select `#outageswitch` in the browser's devtools and set `display: unset`.

While it's on, that tab's requests to the site all fail as in a real outage, and its relay connection keeps working. Turning it off makes the tab check the server immediately.

### Backup relay on/off setting

The settings menu has a hidden "Backup relay: On/Off" button (`#p2ptoggle`; set `display: unset` to show it). It's a per-browser setting, saved in that browser's storage. When it's off, the tab never connects to the relay and uses only its own SSE stream, with no fallback during outages. Toggling it reloads the room.

### Backend tests

These run on the web host, against the queup-dev Redis only:

```
cd /web/groups/menon18/private/queup-dev && python3 -m pytest tests
```

### Relay security tests

`tests/relay_security_test.js` runs on verilog against the live relay, using a throwaway room:

```
cd /home/ece270/queup-relay && node /path/to/relay_security_test.js 8443
```

It checks that unsigned, wrongly signed and stale publishes, missing, forged, altered and expired tokens, and foreign origins are all refused; that genuine traffic flows; that envelopes for another user and staff actions from students are dropped; and that an older publish arriving late can't replace newer state.

### End-to-end tests

Real browsers via Playwright, against a live deployment and the live relay. Each scenario uses its own throwaway room and deletes it.

```
QUEUP_STAFF_PW=... QUEUP_STUD_PW=... [QUEUP_BROWSER=firefox] [QUEUP_BASE=...] python3 tests/e2e_offline_sync.py [scenario ...]
```

Accounts and defaults:
- **Test accounts:** `tstaff1` (an owner) and `tstud1` (a student). They live in the dev tree's `.htpasswd`, with passwords in `private/queup-dev/test_credentials.txt`.
- **Rate limits:** `tstud1` is deliberately subject to them.
- **Default target:** `queup-dev`.
- **Outages:** the tests block a tab's requests to engineering.purdue.edu; its relay connection stays up, as in a real outage.

### Recording real browsers

`tests/record_browsers.py <ROOM>` opens a Chrome and a Firefox window on a room and logs to a file:
- console output and page errors
- failed requests
- relay connections opening and closing (without tokens)
- each tab's relay and outage state whenever it changes

It needs `pip install playwright` and `python -m playwright install firefox chromium`. It asks for each browser's login and never stores it. Terminal commands while it runs:

| Command | Effect |
|---|---|
| `b1` / `u1`, `b2` / `u2` | block / unblock Chrome's / Firefox's requests to the site (an already-open SSE stream and the relay survive) |
| `k1` / `k2` | also cut that browser's open SSE stream |
| `t` | relay self-test in both browsers: opens a second connection with the tab's token |
| `s` | state snapshot |
| `n <text>` | note |
| `q` | quit |

## Known limitations

- **One relay.** If verilog is down, tabs fall back to SSE and there's no offline mode until it's back.
- **Missed updates.** If roomd can't publish a change, tabs on the relay see it on their next re-check (at most 2 minutes, sooner if they act).
- **Backdated joins.** A user could backdate their own signed offline join to move ahead of other *offline* joins, but never ahead of anyone already in the queue. Offline joins are visible in the log as `ouadd`.
