# Peer-to-peer mode

QueUp rooms keep working when the server (engineering.purdue.edu) is down or slow. Browsers in the same room connect to each other over WebRTC while the server is up. If it stops answering, they keep the queue going among themselves and send everything back to the server when it returns.

This document explains how that works, what it needs on the server, and how to test it.

## What users see

A status badge next to the room name:

| Badge | Meaning |
|---|---|
| 🟢 Syncing with N users | Server up; connected directly to N other tabs in the room |
| 🟠 Syncing with N users, offline. | Server unreachable; the queue keeps working between connected tabs |
| ⏳ Syncing offline changes… | Server is back; changes made while it was down are being sent to it |
| 🔴 Server disconnected | Server unreachable and no other tabs connected |
| ⚠️ Signed out — reload to sign in again | The browser stopped sending the user's login (401) |

Clicking the badge explains the current state. With the server up and nobody else connected, no badge is shown.

**Works while the server is down:**
- joining and leaving a queue
- staff removing a user, marking/unmarking, and clearing a queue

**Online-only:** locking, queue create/rename/delete, subtitle, owners, cooldown, single-queue, timer, broadcasts, closing the room. These show "Could not reach the server" during an outage.

The page itself is served by the server, so it can't be loaded or reloaded while the server is down. Tabs that are already open keep working.

**Idle tabs pause.** After an hour with no queue changes and no clicks or typing on the page (and no exam timer running), a tab closes its server connection and mesh links and shows a banner with a Reconnect button. It stays in any queue it's in. Without this, forgotten tabs held a server connection open indefinitely.

## How the mesh forms

1. **Joining.** On joining a room, each tab picks a random peer id and calls `peerjoin`. The response carries:
   - TURN credentials
   - a per-tab signing key (for offline actions)
   - the server time (for clock correction)
2. **Presence.** Presence lives in Redis: `peers<ROOM>` (last heartbeat time per peer) and `peermeta<ROOM>` (user, owner flag, join time, page version). It's included in every SSE update.
3. **Host election.** Every tab independently elects a host from the presence list:
   - only peers on the newest mesh version present are eligible (`MESH_VERSION` in `app.html`);
   - among those, owners come first, then the earliest join time, then username.

   Every tab sees the same list, so they all agree without talking to each other.
4. **Star topology.** The mesh is a star around the host. Each non-host tab opens one WebRTC data channel to the host, and the host relays messages between its peers.
5. **Signaling.** Offers, answers and ICE candidates are sent through the server: `signalsend` stores them in `signal<ROOM>`, and they're delivered over the recipient's SSE stream. All pending signals go out in one batched request, never one request per peer.
6. **TURN.** A TURN relay (`verilog.ecn.purdue.edu:3478`, UDP and TCP) carries connections that can't go direct.
7. **Make-before-break.** When the elected host changes, a tab keeps its working link until the replacement is actually open; a replacement that doesn't open within 15s is dropped and retried later. While the server is unreachable, nothing changes, since no new connection could be signaled anyway.
8. **Leaving.** A closing or reloading tab tells the server right away (`peerleave`, sent with a keepalive fetch so it carries the login). That way it stops being elected host immediately. A tab that crashes without notice drops out of presence after 90s.

### One server connection per room

Each SSE connection holds a server worker and a Redis `MONITOR` connection.

- **Handing off.** Once a non-host tab is linked to the host and receiving the host's relayed room updates, it closes its own SSE connection. This only happens when the host is an owner's tab (judged from the tab's own server data); with a student host, every tab keeps its own SSE connection and ignores relayed room state.
- **Its own actions.** These still go to the server over HTTP: the host can't act as another user.
- **Taking it back.** A tab reopens its own SSE when its link to the host drops, a new host is elected, the host stops relaying for 90s, one of its own requests fails, or the host signs out.

### Presence heartbeats

- Every 45s, each tab sends `peerheartbeat`.
- An owner acting as host heartbeats for all of its peers in one request, so those peers skip theirs. The server only accepts this batch from an owner.
- `peerjoin` runs only on joining, after an outage, and hourly to renew TURN credentials (which last 4 hours).

## Detecting an outage

The page treats the server as unreachable when any of these happen:
- **SSE fails twice in a row.** `sseupdate` closes the stream every 60s on purpose, so one failure gets a quick retry first.
- **A queue action gets no answer within 2s** while a peer is connected. The action goes over the mesh instead.
- **One of the page's own requests fails** (a queue action, signaling, or sync).
- **A sync gets no answer within 5s.**

**Recovery.** While down, the page pings `ping.txt`, a static file that runs no Python. It starts every 5s, backs off to every 30s, and reconnects SSE once a ping succeeds. When any tab reconnects, it tells its peers, and they check right away.

**Signed out.** A 401 means the browser stopped sending the login, not that the server is down. The tab shows "Signed out", tells its peers to use their own connections, and stays on as a mesh relay; its own actions are synced by peers.

## Offline actions and sync

**Envelopes.** During an outage, an action is applied locally and sent over the mesh as an *envelope*, carrying:
- a unique id and the originating peer id
- room, user, action, queue, plus a target user and a value for staff actions
- the click time, corrected to server time
- a per-tab sequence number
- an HMAC-SHA256 signature made with the tab's signing key

**Journal.** Every tab stores every envelope it sees in a journal, in `localStorage` under its own key (in memory if storage is unavailable). The room is drawn as the last server state with the journal applied on top. A tab reopened later adopts journals left by closed tabs of the same room.

**Sync.** When the server is back:
- The host sends the journal to `offlinesync` immediately, then tells its peers which entries were accepted (`syncack`).
- Other tabs wait 4–7s. The host's report clears the entries they only relayed, but each tab always confirms its *own* actions with the server itself, so a host can't make a tab drop actions it never synced. Most tabs send nothing; a tab that acted offline sends one small request.
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

**Slow servers.** Online queue requests also carry the click time and an operation id (`t`, `opid`). The id is the same one used by the action's mesh copy, if it was relayed after 2s. This means:
- a request a hung server processes late can't undo something the user did afterwards;
- an action can't be applied twice.

## Server reference

**New `roomd.wsgi` actions.** All are open to any room participant:

| Action | Purpose |
|---|---|
| `turn` | Fresh TURN credentials |
| `peerjoin` | Join presence; returns ICE servers, signing key, server time |
| `peerheartbeat` | Refresh presence (POST body `{"peers": [...]}` from an owner host covers its peers) |
| `peerleave` | Leave presence |
| `signalsend` | POST `{"messages": [{to, type, payload}, ...]}` (offer, answer, ice); only from a peer id you joined with |
| `offlinesync` | POST `{"envelopes": [...]}`; returns `{"results": {id: status}, "servertime"}` |

**Redis keys per room.** All are deleted with the room:

| Key | Contents |
|---|---|
| `peers<ROOM>` | sorted set: peer id → last heartbeat |
| `peermeta<ROOM>` | hash: peer id → user, owner, join time, version |
| `peerjoin<ROOM>` | short-lived flag that triggers an SSE update when a peer (re)appears |
| `signal<ROOM>` | hash: peer id → pending signaling messages |
| `peerkeys<ROOM>` | hash: peer id → signing key, user, issue time |
| `offlineapplied<ROOM>` | set of decided `user:id` envelope / operation ids (24h) |
| `offlinestatus<ROOM>` | hash: `user:id` → how it was decided (24h) |
| `qwatermark<ROOM>` | hash: `user\|queue`, `*\|queue`, `m:user\|queue` → latest change time |
| `lastadd<ROOM>` | hash: user → last self-join time (cooldown is measured from it) |

**SSE.**
- **Extra fields:** each update also carries `peers`, `signals` (for that connection's peer only, and only if the requesting user joined with that peer id), `watermarks` and `servertime`.
- **Keepalive:** the stream sends a keepalive comment after 15s without updates, so the server notices clients that have gone away.
- **Disconnects:** a client disconnect closes the stream normally instead of killing the worker.

**Rate limits** (`lib/ratelimiter.py`). These are per-user fixed windows. A window runs out on time; rejected requests don't extend it.
- **`action`:** 3 per second, for clicks and room requests.
- **`mesh`:** 60 per 10 seconds, for SSE, signaling, presence and sync. A request only counts as mesh traffic if roomd will handle it as such; adding `sseupdate` to a queue or room action does not move it here.

Usernames listed one per line in `<private>/ratelimit_bypass` are exempt. The file is optional and not tracked; production should not have one.

**Log codes:**

| Code | Meaning |
|---|---|
| `ouadd`, `oudel` | Self join/leave while offline |
| `ousdel`, `oqmrk`, `oqclr` | Staff remove/mark/clear while offline |
| `orej` | Offline action rejected at sync, with the reason |

Offline rows carry the time the action happened and the user whose browser synced it. The admin log sorts by time.

**Trusting other browsers.** Everything that arrives over the mesh is checked before use:
- **Envelopes:** checked against the same field formats the server enforces. The host also only relays an envelope if it came from the tab that signed it, and staff actions only if that tab is an owner's.
- **Relayed room updates:** accepted only from an owner's host tab, and rebuilt from validated fields only before rendering. Usernames, queue names and waitdata must match the server's formats, and full names are HTML-escaped. A student host therefore can't show peers a fake queue or broadcast.

The server re-verifies every signature at sync time, so a misbehaving tab can at worst affect what others see until the next real update.

## Deployment

**Private folder.** The private directory (`private/queup/`) needs:
- `redis_socket`: already present.
- `turn_secret`: the coturn `static-auth-secret`, with no trailing newline and mode 600. It's ignored by git; never commit it.
- `ratelimit_bypass`: optional, and best left out in production.

**TURN server.** coturn with `use-auth-secret` on verilog, port 3478 over UDP and TCP. Credentials are minted per user, valid 4 hours. Relays use high ports (49152–65535 in testing). These must be reachable from wherever students are for relayed connections to work.

**Redis.** Needs 6.2 or newer (the code uses `SMISMEMBER`). The current server runs 8.10.2.

**Reloading Python changes.** After changing anything under `lib/`, touch `roomd.wsgi` and `admin/admin.wsgi`. They drop cached `lib` modules when they reload.

**Changing the mesh protocol.** Bump `MESH_VERSION` in `app.html`, so tabs still running an older page can't host.

## Testing

The tests live in the private repo, under `private/queup-dev/tests/`, so they aren't served with the site. Paths below are relative to `private/queup-dev/`.

### Simulate outage switch

Every room page has a hidden "Simulate outage" switch at the right of the room title. To use it, select `#outageswitch` in the browser's devtools and set `display: unset`.

While it's on, that tab's requests to the site all fail as in a real outage, and its WebRTC links keep working. Turning it off makes the tab check the server immediately.

### Peer-to-peer on/off setting

The settings menu has a hidden "Peer-to-peer: On/Off" button (`#p2ptoggle`; set `display: unset` to show it). It's a per-browser setting, saved in that browser's storage, for browsers where peer-to-peer misbehaves. When it's off, the tab never joins the mesh and sends every request straight to the server, with no mesh fallback during outages. Toggling it reloads the room.

### Backend tests

These run on the web host, against the queup-dev Redis only:

```
cd /web/groups/menon18/private/queup-dev && python3 -m pytest tests
```

### End-to-end tests

Real browsers via Playwright, against a live deployment. Each scenario uses its own throwaway room and deletes it.

```
QUEUP_STAFF_PW=... QUEUP_STUD_PW=... [QUEUP_BROWSER=firefox] [QUEUP_BASE=...] python3 tests/e2e_offline_sync.py [scenario ...]
```

Accounts and defaults:
- **Test accounts:** `tstaff1` (an owner) and `tstud1` (a student). They live in the dev tree's `.htpasswd`, with passwords in `private/queup-dev/test_credentials.txt`.
- **Rate limits:** `tstud1` is deliberately subject to them.
- **Default target:** `queup-dev`.

### Recording real browsers

`tests/record_browsers.py <ROOM>` opens a Chrome and a Firefox window on a room and logs to a file:
- console output and page errors
- failed requests
- WebRTC connection states and paths
- each tab's mesh state whenever it changes

It needs `pip install playwright` and `python -m playwright install firefox chromium`. It asks for each browser's login and never stores it. Terminal commands while it runs:

| Command | Effect |
|---|---|
| `b1` / `u1`, `b2` / `u2` | block / unblock all of Chrome's / Firefox's requests (an already-open SSE stream survives) |
| `k1` / `k2` | also cut that browser's open SSE stream |
| `t` | relay-only TURN self-test in both browsers |
| `s` | state snapshot |
| `n <text>` | note |
| `q` | quit |

### Checking the TURN server

`python3 tests/turn_check.py <queup-username>` uses only the Python standard library. It mints credentials from queup-dev the same way the page does, then tests:
- authenticated allocation over UDP and TCP
- relaying between two allocations
- plain UDP reaching a relay port directly
- that a wrong password is refused

If the reported mapped address is a `100.x` Tailscale address, the test went through a tunnel, not the public internet.

## Known limitations

- **Unseen staff tabs.** The host only relays staff actions from tabs its latest presence list marks as owners. Actions from a staff tab that joined just before an outage may not reach other tabs, though they still sync from the staff member's own journal.
- **Backdated joins.** A user could backdate their own signed offline join to move ahead of other *offline* joins, but never ahead of anyone already in the queue. Offline joins are visible in the log as `ouadd`.
- **Abandoned connections.** The server notices a closed tab's SSE connection only at the next write (keepalive every 15s), and the load balancer in front of Apache can delay that further.
