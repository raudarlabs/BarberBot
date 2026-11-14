"""
Storage for the arcade (and the bot's phone-link store), behind a tiny Redis-style API:
`pipeline([...], [...])` runs several commands in one round trip.

Backends, first match wins:
1. Postgres via Neon's HTTP SQL endpoint, when DATABASE_URL (or POSTGRES_URL /
   ARCADE_DATABASE_URL) is set. Commands are translated to SQL and run as one
   transaction per pipeline. Tables are created on first use. Stdlib only.
2. Upstash Redis REST, when UPSTASH_REDIS_REST_URL/TOKEN (or Vercel KV names) are set.
3. In-memory stand-in for local dev and tests (ARCADE_DEV=1).

Supported commands: SET [EX n] [NX], GET, GETDEL, INCR, EXPIRE, ZADD [GT],
ZREVRANGE [WITHSCORES], ZREVRANK, ZSCORE, HSET, HMGET.
"""
import json
import os
import random
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


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


def _pg_url():
    for name in ("ARCADE_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL"):
        v = (os.environ.get(name) or "").strip().strip('"')
        if v.startswith(("postgres://", "postgresql://")):
            return v
    return None


def backend():
    """'postgres', 'upstash', 'memory' (dev) or None (not configured)."""
    if _pg_url():
        return "postgres"
    if all(_creds()):
        return "upstash"
    if os.environ.get("ARCADE_DEV") == "1":
        return "memory"
    return None


def configured():
    """True when a persistent store (Postgres or Upstash) is configured."""
    return backend() in ("postgres", "upstash")


class RedisError(RuntimeError):
    pass


def pipeline(*commands):
    """Run several commands in one round trip. Returns a list of results."""
    commands = [[str(a) for a in c] for c in commands]
    kind = backend()
    if kind == "postgres":
        return _pg_pipeline(_pg_url(), commands)
    if kind == "memory":
        return [memory().execute(c) for c in commands]
    if kind is None:
        raise RedisError("No storage configured: set DATABASE_URL (Neon) or UPSTASH_REDIS_REST_URL/TOKEN")

    url, token = _creds()
    body = json.dumps(commands).encode()
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


# ---------------------------------------------------------------- Postgres (Neon HTTP)

SCHEMA = [
    "create table if not exists arcade_kv (k text primary key, v text not null, exp timestamptz)",
    "create table if not exists arcade_zs (k text not null, m text not null, s double precision not null,"
    " primary key (k, m))",
    "create index if not exists arcade_zs_rank on arcade_zs (k, s desc, m desc)",
    "create table if not exists arcade_hs (k text not null, f text not null, v text not null, primary key (k, f))",
]
_schema_ready = set()
_EXP = "case when $3::int is null then null else now() + make_interval(secs => $3::int) end"
_ALIVE = "(exp is null or exp > now())"


def _num(v):
    if v is None:
        return None
    f = float(v)
    return str(int(f)) if f.is_integer() else repr(f)


def _sql(c):
    """Translate one command into (sql, params, decode(rows))."""
    op, a = c[0].upper(), c[1:]
    first = lambda rows: rows[0][0] if rows else None  # noqa: E731
    if op == "SET":
        opts = [x.upper() for x in a[2:]]
        ex = a[2 + opts.index("EX") + 1] if "EX" in opts else None
        cond = " where arcade_kv.exp is not null and arcade_kv.exp <= now()" if "NX" in opts else ""
        sql = ("insert into arcade_kv (k, v, exp) values ($1, $2, %s) on conflict (k) do update"
               " set v = excluded.v, exp = excluded.exp%s returning 'OK'" % (_EXP, cond))
        return sql, [a[0], a[1], ex], first
    if op == "GET":
        return "select v from arcade_kv where k = $1 and " + _ALIVE, [a[0]], first
    if op == "GETDEL":
        return ("with d as (delete from arcade_kv where k = $1 returning v, exp)"
                " select v from d where " + _ALIVE), [a[0]], first
    if op == "INCR":
        dead = "arcade_kv.exp is not null and arcade_kv.exp <= now()"
        sql = ("insert into arcade_kv (k, v, exp) values ($1, '1', null) on conflict (k) do update set"
               " v = case when %s then '1' else (arcade_kv.v::bigint + 1)::text end,"
               " exp = case when %s then null else arcade_kv.exp end returning v" % (dead, dead))
        return sql, [a[0]], lambda rows: int(rows[0][0])
    if op == "EXPIRE":
        return ("update arcade_kv set exp = now() + make_interval(secs => $2::int) where k = $1 and "
                + _ALIVE + " returning 1"), [a[0], a[1]], len
    if op == "ZADD":
        key, rest = a[0], list(a[1:])
        gt = False
        while rest and rest[0].upper() in ("GT", "LT", "NX", "XX", "CH"):
            gt = gt or rest[0].upper() == "GT"
            rest.pop(0)
        params, values = [key], []
        for i in range(0, len(rest), 2):
            params += [rest[i], rest[i + 1]]
            values.append("($1, $%d, $%d::double precision)" % (len(params), len(params) - 1))
        upd = "greatest(arcade_zs.s, excluded.s)" if gt else "excluded.s"
        sql = ("insert into arcade_zs (k, m, s) values %s on conflict (k, m) do update set s = %s"
               " returning (xmax = 0)::int" % (", ".join(values), upd))
        return sql, params, lambda rows: sum(int(r[0]) for r in rows)
    if op == "ZREVRANGE":
        start, stop = int(a[1]), int(a[2])
        limit = None if stop < 0 else max(0, stop - start + 1)
        withscores = len(a) > 3 and a[3].upper() == "WITHSCORES"
        sql = "select m, s from arcade_zs where k = $1 order by s desc, m desc offset $2::bigint limit $3::bigint"

        def dec(rows):
            if withscores:
                return [x for m, s in rows for x in (m, _num(s))]
            return [m for m, _ in rows]
        return sql, [a[0], str(max(0, start)), None if limit is None else str(limit)], dec
    if op == "ZREVRANK":
        sql = ("select (select count(*) from arcade_zs z2 where z2.k = z.k and"
               " (z2.s > z.s or (z2.s = z.s and z2.m > z.m))) from arcade_zs z where z.k = $1 and z.m = $2")
        return sql, [a[0], a[1]], lambda rows: int(rows[0][0]) if rows else None
    if op == "ZSCORE":
        return "select s from arcade_zs where k = $1 and m = $2", [a[0], a[1]], lambda rows: _num(first(rows))
    if op == "HSET":
        params, values = [a[0]], []
        for i in range(1, len(a), 2):
            params += [a[i], a[i + 1]]
            values.append("($1, $%d, $%d)" % (len(params) - 1, len(params)))
        sql = ("insert into arcade_hs (k, f, v) values %s on conflict (k, f) do update set v = excluded.v"
               " returning (xmax = 0)::int" % ", ".join(values))
        return sql, params, lambda rows: sum(int(r[0]) for r in rows)
    if op == "HMGET":
        fields = list(a[1:])
        marks = ", ".join("$%d" % (i + 2) for i in range(len(fields)))
        sql = "select f, v from arcade_hs where k = $1 and f in (%s)" % marks

        def dec(rows):
            got = dict(rows)
            return [got.get(f) for f in fields]
        return sql, [a[0]] + fields, dec
    raise RedisError("Postgres store: unsupported command " + op)


def _neon(url, queries):
    host = urlparse(url).hostname
    body = json.dumps({"queries": [{"query": q, "params": p} for q, p in queries]}).encode()
    req = urllib.request.Request("https://%s/sql" % host, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Neon-Connection-String": url,
        "Neon-Raw-Text-Output": "true",
        "Neon-Array-Mode": "true",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())["results"]
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise RedisError("Neon HTTP %d: %s" % (e.code, detail))
    except (OSError, ValueError, KeyError) as e:
        raise RedisError("Neon: %s" % e)


def _pg_pipeline(url, commands):
    plans = [_sql(c) for c in commands]
    queries = [(sql, params) for sql, params, _ in plans]
    pre = []
    if url not in _schema_ready:
        pre = [(q, []) for q in SCHEMA]
    elif random.random() < 0.02:  # occasional cleanup of expired sessions and rate-limit counters
        pre = [("delete from arcade_kv where exp < now() - interval '1 hour'", [])]
    try:
        results = _neon(url, pre + queries)
    except RedisError:
        if not pre or url in _schema_ready:
            raise
        results = _neon(url, pre + queries)  # first-use race on CREATE TABLE: retry once
    _schema_ready.add(url)
    results = results[len(pre):]
    return [dec(r.get("rows") or []) for (_, _, dec), r in zip(plans, results)]


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
