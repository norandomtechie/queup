import os
from urllib.parse import parse_qs

import redis

# Per-user fixed windows: 'action' for clicks, 'mesh' for background traffic (see P2P.md)
LIMITS = {"action": (3, 1), "mesh": (60, 10)}     # bucket: (max requests, window seconds)
MESH_ACTIONS = {"peerjoin", "peerleave", "offlinesync"}



def load_bypass(private):
    """Usernames exempt from rate limits, one per line in <private>/ratelimit_bypass (optional)."""
    try:
        with open(private + "ratelimit_bypass") as f:
            return {line.strip() for line in f if line.strip() and not line.startswith("#")}
    except OSError:
        return set()


def bucket_for(environ):
    """'mesh' only for what roomd will actually treat as mesh traffic; everything else is 'action'."""
    query = parse_qs(environ.get("QUERY_STRING", ""))
    action = query.get("action", [""])[0]
    if "queue" in query:                # queue requests are always handled as actions
        return "action"
    if action:
        return "mesh" if action in MESH_ACTIONS and "sseupdate" not in query else "action"
    return "mesh" if "sseupdate" in query else "action"


class RateLimiter:
    def __init__(self, dbpath, environ):
        self.dbpath = dbpath
        self.environ = environ

    def __enter__(self):
        # The Redis socket sits next to ratelimit.db in the private dir
        private = os.path.dirname(self.dbpath) + '/'
        try:
            self.conn = redis.Redis(unix_socket_path=open(private + 'redis_socket').read().strip())
        except Exception:
            raise Exception("Database connection failed.  Contact course staff **immediately**.")
        self.bypass = load_bypass(private)
        self.bucket = bucket_for(self.environ)
        limit, window = LIMITS[self.bucket]
        self.limit = limit
        key = "rl:%s:%s" % (self.bucket, self.environ['REMOTE_USER'])
        pipe = self.conn.pipeline()
        pipe.set(key, 0, ex=window, nx=True)     # opens a window only if none is running
        pipe.incr(key)
        self.count = pipe.execute()[1]
        return self

    def __exit__(self, type, value, traceback):
        self.conn.close()

    def should_limit(self, username):
        if username in self.bypass:
            return False
        return self.count > self.limit
