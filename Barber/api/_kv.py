"""
Redis access.

Production: Upstash Redis over its REST pipeline endpoint (stdlib only, no deps).
Local dev (ARCADE_DEV=1 and no Upstash env): a small in-memory stand-in that
implements just the commands the arcade API uses.
"""
import json
import os
import time
import urllib.request


def _find(suffix):
    """Env var by exact name, or with the store prefix Vercel adds (e.g. barber_bot_KV_REST_API_URL)."""
    if os.environ.get(suffix):
        return os.environ[suffix]
    for k in sorted(os.environ):
        if k.endswith("_" + suffix) and os.environ[k]:
            return os.environ[k]
    return None


def _creds():
    # Upstash marketplace names, Vercel KV names, or Vercel KV names with a store prefix
    url = _find("UPSTASH_REDIS_REST_URL") or _find("KV_REST_API_URL")
    token = _find("UPSTASH_REDIS_REST_TOKEN") or _find("KV_REST_API_TOKEN")
    return url, token


class RedisError(RuntimeError):
    pass


def pipeline(*commands):
    """Run several commands in one round trip. Returns a list of results."""
    url, token = _creds()
    if not url or not token:
        if os.environ.get("ARCADE_DEV") == "1":
            return [memory().execute([str(a) for a in c]) for c in commands]
        raise RedisError("Missing UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN")

    body = json.dumps([[str(a) for a in c] for c in commands]).encode()
    req = urllib.request.Request(url.rstrip("/") + "/pipeline", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read())
    except OSError as e:
        raise RedisError(str(e))
    results = []
    for item in out:
        if "error" in item:
            raise RedisError(item["error"])
        results.append(item.get("result"))
    return results


def cmd(*args):
    return pipeline(list(args))[0]


# ---------------------------------------------------------------- dev stand-in

class MemoryRedis:
    """Tiny subset of Redis semantics, enough for local dev and tests."""

    def __init__(self):
        self.data = {}
        self.expiry = {}

    def _alive(self, key):
        exp = self.expiry.get(key)
        if exp is not None and exp <= time.time():
            self.data.pop(key, None)
            self.expiry.pop(key, None)
        return key in self.data

    @staticmethod
    def _num(score):
        return str(int(score)) if float(score).is_integer() else str(score)

    def _zsorted(self, key):
        z = self.data.get(key, {}) if self._alive(key) else {}
        # ZREVRANGE order: score desc, then member desc
        return sorted(z.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)

    def execute(self, c):
        op = c[0].upper()
        a = c[1:]
        if op == "SET":
            opts = [x.upper() for x in a[2:]]
            if "NX" in opts and self._alive(a[0]):
                return None
            self.data[a[0]] = a[1]
            self.expiry.pop(a[0], None)
            if "EX" in opts:
                self.expiry[a[0]] = time.time() + int(a[2 + opts.index("EX") + 1])
            return "OK"
        if op == "GET":
            return self.data.get(a[0]) if self._alive(a[0]) else None
        if op == "GETDEL":
            if not self._alive(a[0]):
                return None
            self.expiry.pop(a[0], None)
            return self.data.pop(a[0])
        if op == "INCR":
            v = int(self.data.get(a[0], 0) if self._alive(a[0]) else 0) + 1
            self.data[a[0]] = str(v)
            return v
        if op == "EXPIRE":
            if not self._alive(a[0]):
                return 0
            self.expiry[a[0]] = time.time() + int(a[1])
            return 1
        if op == "ZADD":
            key, rest = a[0], list(a[1:])
            gt = False
            while rest and rest[0].upper() in ("GT", "LT", "NX", "XX", "CH"):
                gt = gt or rest[0].upper() == "GT"
                rest.pop(0)
            if not self._alive(key):
                self.data[key] = {}
            z = self.data[key]
            added = 0
            for i in range(0, len(rest), 2):
                score, member = float(rest[i]), rest[i + 1]
                if member not in z:
                    added += 1
                    z[member] = score
                elif not gt or score > z[member]:
                    z[member] = score
            return added
        if op == "ZREVRANGE":
            items = self._zsorted(a[0])
            start, stop = int(a[1]), int(a[2])
            stop = len(items) - 1 if stop < 0 else stop
            sl = items[start:stop + 1]
            if len(a) > 3 and a[3].upper() == "WITHSCORES":
                flat = []
                for m, s in sl:
                    flat += [m, self._num(s)]
                return flat
            return [m for m, _ in sl]
        if op == "ZREVRANK":
            for i, (m, _) in enumerate(self._zsorted(a[0])):
                if m == a[1]:
                    return i
            return None
        if op == "ZSCORE":
            z = self.data.get(a[0], {}) if self._alive(a[0]) else {}
            return self._num(z[a[1]]) if a[1] in z else None
        if op == "HSET":
            if not self._alive(a[0]):
                self.data[a[0]] = {}
            h = self.data[a[0]]
            added = 0
            for i in range(1, len(a), 2):
                added += a[i] not in h
                h[a[i]] = a[i + 1]
            return added
        if op == "HMGET":
            h = self.data.get(a[0], {}) if self._alive(a[0]) else {}
            return [h.get(f) for f in a[1:]]
        raise RedisError("MemoryRedis: unsupported command " + op)


_memory = None


def memory():
    global _memory
    if _memory is None:
        _memory = MemoryRedis()
    return _memory
