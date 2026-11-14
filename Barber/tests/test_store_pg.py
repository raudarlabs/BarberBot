"""
Conformance test: the Postgres (Neon HTTP) store must answer exactly like the
in-memory reference for every command the arcade and the bot use.

    ARCADE_TEST_DATABASE_URL=postgresql://... python -m unittest tests.test_store_pg

Skipped when ARCADE_TEST_DATABASE_URL is not set. Uses a unique key prefix and
deletes its rows afterwards, so it is safe to run against the real database.
"""
import os
import sys
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))

import _kv  # noqa: E402

URL = os.environ.get("ARCADE_TEST_DATABASE_URL")


@unittest.skipUnless(URL, "set ARCADE_TEST_DATABASE_URL to run against Postgres")
class PostgresConformance(unittest.TestCase):
    def setUp(self):
        self.p = "test:%s:" % uuid.uuid4().hex[:10]
        self.mem = _kv.MemoryRedis()

    def tearDown(self):
        _kv._neon(URL, [("delete from %s where k like $1" % t, [self.p + "%"])
                        for t in ("arcade_kv", "arcade_zs", "arcade_hs")])

    def both(self, *commands):
        commands = [[str(x) for x in c] for c in commands]
        want = [self.mem.execute(c) for c in commands]
        got = _kv._pg_pipeline(URL, commands)
        self.assertEqual(got, want, commands)
        return got

    def test_strings_sessions_and_counters(self):
        k = self.p + "s"
        self.both(["GET", k], ["SET", k, "v1"], ["GET", k], ["SET", k, "v2", "EX", "60"], ["GET", k])
        self.both(["SET", k, "v3", "NX"], ["GET", k], ["GETDEL", k], ["GETDEL", k], ["SET", k, "v4", "NX"])
        c = self.p + "c"
        self.both(["INCR", c], ["INCR", c], ["EXPIRE", c, "120"], ["EXPIRE", self.p + "none", "5"], ["INCR", c])
        self.both(["SET", self.p + "j", '{"u": "1", "g": "snake", "t": 1.5}', "EX", "7200"],
                  ["GETDEL", self.p + "j"])

    def test_expiry(self):
        k = self.p + "e"
        self.both(["SET", k, "x", "EX", "1"], ["INCR", self.p + "rl"], ["EXPIRE", self.p + "rl", "1"])
        time.sleep(2.2)
        self.mem.expiry[k] = 0  # make the reference expire as well
        self.mem.expiry[self.p + "rl"] = 0
        self.both(["GET", k], ["SET", k, "y", "NX"], ["GET", k], ["INCR", self.p + "rl"])

    def test_sorted_sets_like_leaderboards(self):
        z = self.p + "lb"
        self.both(["ZADD", z, "GT", "25", "111"], ["ZADD", z, "GT", "10", "111"], ["ZADD", z, "GT", "40", "222"],
                  ["ZADD", z, "GT", "40", "333"], ["ZADD", z, "7.5", "444"], ["ZADD", z, "GT", "1000000", "555"])
        self.both(["ZREVRANGE", z, "0", "9", "WITHSCORES"], ["ZREVRANGE", z, "1", "2"],
                  ["ZREVRANGE", z, "0", "-1", "WITHSCORES"], ["ZREVRANGE", z, "0", "0", "WITHSCORES"],
                  ["ZREVRANGE", self.p + "empty", "0", "0", "WITHSCORES"])
        self.both(["ZREVRANK", z, "111"], ["ZREVRANK", z, "222"], ["ZREVRANK", z, "333"], ["ZREVRANK", z, "nope"],
                  ["ZSCORE", z, "444"], ["ZSCORE", z, "555"], ["ZSCORE", z, "nope"],
                  ["ZCARD", z], ["ZCARD", self.p + "empty"])

    def test_hashes_like_player_names(self):
        h = self.p + "users"
        self.both(["HSET", h, "111", "Olena K.", "222", "Тарас"], ["HSET", h, "111", "Olena"],
                  ["HMGET", h, "222", "111", "999"], ["HMGET", self.p + "none", "1"])
        self.both(["HGETALL", h], ["HDEL", h, "111", "nope"], ["HGETALL", h], ["HDEL", h, "222"],
                  ["HGETALL", h], ["HGETALL", self.p + "none"])


if __name__ == "__main__":
    unittest.main()
