"""Usage: python3 turn_check.py <queup-username>  -- checks the TURN server from this machine (see P2P.md, Testing)."""
import base64, getpass, hashlib, hmac, json, os, random, socket, string, struct, sys, time, urllib.request, uuid
HOST, PORT = "verilog.ecn.purdue.edu", 3478
BASE = "https://engineering.purdue.edu/~menon18/queup-dev/roomd.wsgi"


def mint_credentials(login, password):
    auth = {"Authorization": "Basic " + base64.b64encode(f"{login}:{password}".encode()).decode()}
    get = lambda q: urllib.request.urlopen(urllib.request.Request(f"{BASE}?{q}", headers=auth), timeout=10).read().decode()
    room = "YT" + "".join(random.choices(string.ascii_uppercase + string.digits, k=3))
    get(f"setup=true&action=add&room={room}")
    try:
        joined = json.loads(get(f"setup=true&action=peerjoin&room={room}&peerid={uuid.uuid4()}&v=5"))
    finally:
        get(f"setup=true&action=del&room={room}")
    turn = next(s for s in joined["iceServers"] if str(s["urls"]).startswith("turn:"))
    return turn["username"], turn["credential"]


if len(sys.argv) != 2:
    sys.exit(__doc__)
USER, PASS = mint_credentials(sys.argv[1], getpass.getpass(f"QueUp password for {sys.argv[1]}: "))
COOKIE = 0x2112A442
A = dict(MAPPED=0x0020, USERNAME=0x0006, MI=0x0008, ERROR=0x0009, PEER=0x0012, DATA=0x0013,
         REALM=0x0014, NONCE=0x0015, RELAYED=0x0016, TRANSPORT=0x0019, LIFETIME=0x000D)

def attr(t, v):
    return struct.pack("!HH", t, len(v)) + v + b"\0" * (-len(v) % 4)

def xaddr(ip, port):
    return struct.pack("!BBH", 0, 1, port ^ (COOKIE >> 16)) + struct.pack("!I", struct.unpack("!I", socket.inet_aton(ip))[0] ^ COOKIE)

def unx(v):
    port = struct.unpack("!H", v[2:4])[0] ^ (COOKIE >> 16)
    ip = socket.inet_ntoa(struct.pack("!I", struct.unpack("!I", v[4:8])[0] ^ COOKIE))
    return ip, port

def build(mtype, attrs, auth=None):
    tid = os.urandom(12)
    body = b"".join(attrs)
    if auth:
        realm, nonce, key = auth
        body += attr(A["USERNAME"], USER.encode()) + attr(A["REALM"], realm) + attr(A["NONCE"], nonce)
        head = struct.pack("!HHI12s", mtype, len(body) + 24, COOKIE, tid)
        mi = hmac.new(key, head + body, hashlib.sha1).digest()
        body += attr(A["MI"], mi)
    return struct.pack("!HHI12s", mtype, len(body), COOKIE, tid) + body, tid

def parse(msg):
    mtype, mlen = struct.unpack("!HH", msg[:4]); out = {}; i = 20
    while i + 4 <= 20 + mlen:
        t, l = struct.unpack("!HH", msg[i:i+4]); out[t] = msg[i+4:i+4+l]; i += 4 + l + (-l % 4)
    return mtype, out

class Conn:
    def __init__(self, transport):
        self.t = transport
        if transport == "udp":
            self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); self.s.settimeout(3)
        else:
            self.s = socket.create_connection((HOST, PORT), timeout=3)
        self.buf = b""
    def send(self, data):
        self.s.sendto(data, (HOST, PORT)) if self.t == "udp" else self.s.sendall(data)
    def recv(self):
        if self.t == "udp":
            return self.s.recvfrom(4096)[0]
        while True:
            if len(self.buf) >= 20:
                n = 20 + struct.unpack("!H", self.buf[2:4])[0]
                if len(self.buf) >= n:
                    msg, self.buf = self.buf[:n], self.buf[n:]; return msg
            chunk = self.s.recv(4096)
            if not chunk: raise ConnectionError("closed")
            self.buf += chunk
    def request(self, mtype, attrs, auth=None):
        msg, tid = build(mtype, attrs, auth); self.send(msg)
        while True:
            r = self.recv()
            if r[8:20] == tid: return parse(r)

def allocate(transport, password=PASS):
    c = Conn(transport)
    rt = attr(A["TRANSPORT"], bytes([17, 0, 0, 0]))
    mtype, a = c.request(0x0003, [rt])
    if mtype != 0x0113: return c, None, f"unexpected first reply 0x{mtype:04x}"
    realm, nonce = a[A["REALM"]], a[A["NONCE"]]
    key = hashlib.md5(f"{USER}:{realm.decode()}:{password}".encode()).digest()
    c.auth = (realm, nonce, key)
    mtype, a = c.request(0x0003, [rt], c.auth)
    if mtype != 0x0103:
        err = a.get(A["ERROR"], b"\0\0\0\0")
        return c, None, f"refused: {err[2] * 100 + err[3]} {err[4:].decode(errors='replace')}"
    return c, (unx(a[A["RELAYED"]]), unx(a[A["MAPPED"]])), "ok"

def permit(c, ip):
    mtype, a = c.request(0x0008, [attr(A["PEER"], xaddr(ip, 0))], c.auth)
    return mtype == 0x0108

def await_data(c, timeout=3):
    c.s.settimeout(timeout); end = time.time() + timeout
    while time.time() < end:
        try: msg = c.recv()
        except socket.timeout: return None
        mtype, a = parse(msg)
        if mtype == 0x0017: return a.get(A["DATA"])
    return None

def relay_between(transport):
    a, ra, why = allocate(transport); b, rb, why2 = allocate(transport)
    if not ra or not rb: return f"allocation failed ({why} / {why2})"
    (a_relay, a_mapped), (b_relay, _) = ra, rb
    permit(a, b_relay[0]); permit(b, a_relay[0])
    send, _ = build(0x0016, [attr(A["PEER"], xaddr(*b_relay)), attr(A["DATA"], b"hello via relay")])
    a.send(send)
    got = await_data(b)
    return f"relay {a_relay[0]}:{a_relay[1]} (we appear as {a_mapped[0]}) -> {'DELIVERED' if got == b'hello via relay' else 'NOT delivered'}"

def direct_to_relay_port():
    a, ra, why = allocate("udp")
    if not ra: return f"allocation failed ({why})"
    relay, mapped = ra
    plain = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); plain.settimeout(3)
    permit(a, mapped[0])                       # allow our own public IP to reach the relay
    for _ in range(3):
        plain.sendto(b"hello straight to the relay port", relay)
    got = await_data(a)
    return f"plain UDP from off-campus -> {relay[0]}:{relay[1]}: {'REACHED the relay' if got else 'did NOT reach the relay'}"

print("TURN server:", socket.gethostbyname(HOST), "(mapped address below = how verilog sees this machine)")
for t in ("udp", "tcp"):
    try:
        c, r, why = allocate(t)
        print(f"\nallocate over {t.upper()}: {why}" + (f", relay {r[0][0]}:{r[0][1]}, mapped {r[1][0]}" if r else ""))
    except Exception as e:
        print(f"\nallocate over {t.upper()}: ERROR {e}")
for t in ("udp", "tcp"):
    try: print(f"relaying over {t.upper()}: {relay_between(t)}")
    except Exception as e: print(f"relaying over {t.upper()}: ERROR {e}")
try: print(direct_to_relay_port())
except Exception as e: print("direct-to-relay: ERROR", e)
try:
    c, r, why = allocate("udp", password="definitely-wrong")
    print("wrong password:", why)
except Exception as e: print("wrong password: ERROR", e)
