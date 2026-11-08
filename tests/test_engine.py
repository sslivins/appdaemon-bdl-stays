import datetime as dt
import os
import random
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bs_access  # noqa: E402
import bs_engine  # noqa: E402
from bs_access import AccessError  # noqa: E402

TZ = dt.timezone(dt.timedelta(hours=-7))     # BC, permanent UTC-7
H = 3600.0
D = 86400.0


def L(m, d, h=0, mi=0, y=2026):
    return dt.datetime(y, m, d, h, mi, tzinfo=TZ)


def ts(m, d, h=0, mi=0):
    return L(m, d, h, mi).timestamp()


def ev(summary, start, end, uid="u1", desc=None):
    e = {"summary": summary,
         "start": start if isinstance(start, (str, dict)) else
         start.isoformat(),
         "end": end if isinstance(end, (str, dict)) else end.isoformat()}
    if uid:
        e["uid"] = uid
    if desc is not None:
        e["description"] = desc
    return e


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def set(self, t):
        self.t = t


class FakeAccess(bs_access.AccessClient):
    def __init__(self):
        self.visitors = {}
        self.calls = []
        self.fail = {}          # method -> exception to raise
        self.used_pins = set()  # PINs owned by other Access users
        self.n = 0

    def _maybe_fail(self, name):
        exc = self.fail.get(name)
        if exc:
            raise exc

    def resources(self):
        return [{"id": "door-front", "type": "door"}]

    def create_visitor(self, first, last, start, end, res, remarks):
        self.calls.append("create")
        self._maybe_fail("create")
        self.n += 1
        vid = "v%d" % self.n
        self.visitors[vid] = {"first": first, "last": last, "start": start,
                              "end": end, "resources": res,
                              "remarks": remarks, "pin": None}
        return vid

    def update_visitor(self, vid, first, last, start, end, res, remarks):
        self.calls.append("update")
        self._maybe_fail("update")
        if vid not in self.visitors:
            raise AccessError("PUT: gone", code="CODE_NOT_EXISTS")
        self.visitors[vid].update(first=first, last=last, start=start,
                                  end=end, resources=res, remarks=remarks)

    def delete_visitor(self, vid):
        self.calls.append("delete")
        self._maybe_fail("delete")
        self.visitors.pop(vid, None)

    def set_visitor_pin(self, vid, pin):
        self.calls.append("pin")
        self._maybe_fail("pin")
        if pin in self.used_pins:
            raise AccessError("PUT pin: taken",
                              code=bs_access.PIN_EXISTS_CODE)
        self.visitors[vid]["pin"] = pin

    def find_visitor(self, keyword, marker):
        self.calls.append("find")
        for vid, v in self.visitors.items():
            if v["first"].startswith(keyword) and marker in v["remarks"]:
                return vid
        return None


class FakeModes:
    def __init__(self):
        self.active = None
        self.cmds = []
        self.fail = None

    def status(self):
        return {"active": self.active}

    def command(self, cmd, **data):
        if self.fail:
            raise bs_engine.ModesError(self.fail)
        self.cmds.append((cmd, data))
        self.n = getattr(self, "n", 0) + 1
        if cmd == "preheat":
            self.active = {"id": "op%d" % self.n, "kind": "preheat",
                           "status": "scheduled",
                           "groups": [{"status": "pending"},
                                      {"status": "pending"}],
                           "arrival": dt.datetime.fromisoformat(
                               data["arrival"]).timestamp()}
        elif cmd == "update_arrival":
            self.active["arrival"] = dt.datetime.fromisoformat(
                data["arrival"]).timestamp()
        elif cmd == "cancel":
            self.active["status"] = "cancelled"
        else:
            self.active = {"id": "op%d" % self.n, "kind": cmd,
                           "status": "running"}
        return {"active": self.active}


class Base(unittest.TestCase):
    modes_enabled = True
    config = None

    def setUp(self):
        self.clock = Clock(ts(12, 1, 12))
        self.access = FakeAccess()
        self.modes = FakeModes()
        self.store = bs_engine.MemoryStore()
        self.notes, self.logs = [], []
        self.e = self.make()

    def make(self, store=None, access="same"):
        return bs_engine.Engine(
            store or self.store,
            self.access if access == "same" else access, self.modes,
            lambda t, m: self.notes.append((t, m)),
            lambda m, level="INFO": self.logs.append((level, m)), TZ,
            clock=self.clock, config=self.config,
            modes_enabled=self.modes_enabled, rng=random.Random(7))

    def sync(self, events, at=None):
        if at is not None:
            self.clock.set(at)
        return self.e.sync(events)

    def stay(self, sid="u1"):
        return self.e.stays[sid]


# ------------------------------------------------------------------ parsing
class ParseTests(unittest.TestCase):
    def test_timed_event(self):
        s = bs_engine.parse_event(ev("Henderson family", L(12, 20, 15),
                                     L(12, 27, 10)), TZ)
        self.assertEqual(s["id"], "u1")
        self.assertEqual(s["guest"], "Henderson family")
        self.assertEqual(s["arrival"], ts(12, 20, 15))
        self.assertEqual(s["checkout"], ts(12, 27, 10))
        self.assertFalse(s["all_day"])
        self.assertIsNone(s["calendar_pin"])

    def test_utc_offset_is_respected(self):
        s = bs_engine.parse_event(ev("G", "2026-12-20T23:00:00+00:00",
                                     "2026-12-22T18:00:00Z"), TZ)
        self.assertEqual(s["arrival"], ts(12, 20, 16))
        self.assertEqual(s["checkout"], ts(12, 22, 11))

    def test_all_day_spans_arrival_to_departure_day(self):
        # HA stores all-day ends exclusive: Dec 20..27 -> end 2026-12-28.
        s = bs_engine.parse_event(ev("G", "2026-12-20", "2026-12-28"), TZ)
        self.assertTrue(s["all_day"])
        self.assertEqual(s["arrival"], ts(12, 20, 16))
        self.assertEqual(s["checkout"], ts(12, 27, 11))

    def test_single_all_day_event_is_one_night(self):
        s = bs_engine.parse_event(ev("G", {"date": "2026-12-20"},
                                     {"date": "2026-12-21"}), TZ)
        self.assertEqual(s["checkout"], ts(12, 21, 11))

    def test_all_day_times_configurable(self):
        s = bs_engine.parse_event(
            ev("G", "2026-12-20", "2026-12-23"), TZ,
            {"arrival_time": "15:30", "checkout_time": "10:00",
             "allday_end_inclusive": False})
        self.assertEqual(s["arrival"], ts(12, 20, 15, 30))
        self.assertEqual(s["checkout"], ts(12, 23, 10))

    def test_rest_datetime_dict(self):
        s = bs_engine.parse_event(ev(
            "G", {"dateTime": "2026-12-20T16:00:00-07:00"},
            {"dateTime": "2026-12-21T11:00:00-07:00"}), TZ)
        self.assertEqual(s["arrival"], ts(12, 20, 16))

    def test_manual_pin_and_notes(self):
        s = bs_engine.parse_event(ev(
            "G", L(12, 20, 16), L(12, 21, 11),
            desc="Arriving late\nPIN: 4321\nBringing two dogs"), TZ)
        self.assertEqual(s["calendar_pin"], "4321")
        self.assertIsNone(s["calendar_pin_error"])
        self.assertEqual(s["notes"], "Arriving late\nBringing two dogs")

    def test_pin_formats(self):
        for desc, pin in (("pin 12345678", "12345678"),
                          ("PIN code = 12-34-56", "123456"),
                          ("door pin: 0042", "0042")):
            self.assertEqual(bs_engine.parse_description(desc)[0], pin, desc)
        pin, err, notes = bs_engine.parse_description("pinecone garland")
        self.assertIsNone(pin)
        self.assertIsNone(err)
        self.assertEqual(notes, "pinecone garland")

    def test_bad_pins(self):
        for raw in ("pin: 12", "pin: 123456789", "pin: 12ab56", "pin:"):
            pin, err, _ = bs_engine.parse_description(raw)
            self.assertIsNone(pin, raw)
            self.assertIn("4-8 digits", err)
        s = bs_engine.parse_event(ev("G", L(12, 20, 16), L(12, 21, 11),
                                     desc="pin: 9999"), TZ,
                                  {"pin_blocklist": ["9999"]})
        self.assertIsNone(s["calendar_pin"])
        self.assertIn("blocklist", s["calendar_pin_error"])

    def test_hash_id_without_uid(self):
        a = bs_engine.parse_event(ev("G", L(12, 20, 16), L(12, 21, 11),
                                     uid=None), TZ)
        b = bs_engine.parse_event(ev(" g ", L(12, 20, 16), L(12, 22, 11),
                                     uid=None), TZ)
        c = bs_engine.parse_event(ev("G", L(12, 20, 17), L(12, 21, 11),
                                     uid=None), TZ)
        self.assertEqual(a["id_source"], "hash")
        self.assertEqual(a["id"], b["id"])
        self.assertNotEqual(a["id"], c["id"])

    def test_recurrence_id(self):
        e = ev("G", L(12, 20, 16), L(12, 21, 11))
        e["recurrence_id"] = "20261220"
        self.assertEqual(bs_engine.parse_event(e, TZ)["id"], "u1@20261220")

    def test_checkout_before_arrival(self):
        with self.assertRaises(ValueError):
            bs_engine.parse_event(ev("G", L(12, 21, 16), L(12, 21, 11)), TZ)

    def test_empty_summary(self):
        s = bs_engine.parse_event(ev("", L(12, 20, 16), L(12, 21, 11)), TZ)
        self.assertEqual(s["guest"], "Guest")


class PinTests(unittest.TestCase):
    def test_trivial(self):
        for p in ("123456", "654321", "111111", "890123", "121212",
                  "123123", "0000", "9876"):
            self.assertTrue(bs_engine.is_trivial(p), p)
        for p in ("204917", "43219", "583920", "1357"):
            self.assertFalse(bs_engine.is_trivial(p), p)

    def test_generate_rules(self):
        class Seq:
            def __init__(self, digits):
                self.d = iter(digits)

            def randrange(self, n):
                return next(self.d)
        # 111111 (trivial), 123456 (trivial), 204917 (blocklisted),
        # 583920 (used by another stay), then 731904.
        digits = [1] * 6 + [1, 2, 3, 4, 5, 6] + [2, 0, 4, 9, 1, 7] + \
            [5, 8, 3, 9, 2, 0] + [7, 3, 1, 9, 0, 4]
        pin = bs_engine.generate_pin(6, ["204917"], ["583920"], Seq(digits))
        self.assertEqual(pin, "731904")

    def test_generate_random(self):
        rng = random.Random(1)
        for _ in range(200):
            p = bs_engine.generate_pin(6, rng=rng)
            self.assertRegex(p, r"^\d{6}$")
            self.assertFalse(bs_engine.is_trivial(p))
        self.assertRegex(bs_engine.generate_pin(8, rng=rng), r"^\d{8}$")

    def test_manual_pin_rules(self):
        self.assertIsNone(bs_engine.manual_pin_error("1111"))  # guest choice
        self.assertIsNotNone(bs_engine.manual_pin_error("123"))
        self.assertIsNotNone(bs_engine.manual_pin_error("123456789"))
        self.assertIsNotNone(bs_engine.manual_pin_error("4242", ["4242"]))


# ------------------------------------------------------------ access cycle
class AccessLifecycleTests(Base):
    modes_enabled = False

    def events(self, **kw):
        return [ev(kw.get("summary", "Henderson family"),
                   kw.get("start", L(12, 20, 16)),
                   kw.get("end", L(12, 27, 11)), desc=kw.get("desc"))]

    def test_scheduled_then_created(self):
        self.sync(self.events())                       # 19 days out
        self.assertEqual(self.stay()["access_status"], "scheduled")
        self.assertEqual(self.access.calls, [])
        pin = self.e.pin_of(self.stay())[0]
        self.assertRegex(pin, r"^\d{4}$")              # known ahead of time
        self.sync(self.events(), at=ts(12, 6, 16))     # 14 days out
        self.assertEqual(self.stay()["access_status"], "active")
        self.assertEqual(self.access.calls, ["find", "create", "pin"])
        v = self.access.visitors["v1"]
        self.assertEqual(v["first"], "Henderson family")
        self.assertEqual(v["start"], ts(12, 20, 14))   # arrival - 2 h
        self.assertEqual(v["end"], ts(12, 27, 12))     # checkout + 1 h
        self.assertEqual(v["resources"], [{"id": "door-front",
                                           "type": "door"}])
        self.assertEqual(v["pin"], pin)
        self.assertIn("bdl_stays:u1 ", v["remarks"])
        # Nothing to do on the next poll.
        self.sync(self.events(), at=ts(12, 6, 16, 5))
        self.assertEqual(self.access.calls, ["find", "create", "pin"])

    def test_event_changes_update_visitor(self):
        self.sync(self.events(), at=ts(12, 10))
        self.access.calls.clear()
        self.sync(self.events(start=L(12, 21, 16), summary="Hendersons"))
        self.assertEqual(self.access.calls, ["update"])
        v = self.access.visitors["v1"]
        self.assertEqual(v["start"], ts(12, 21, 14))
        self.assertEqual(v["first"], "Hendersons")
        self.access.calls.clear()
        self.sync(self.events(start=L(12, 21, 16), summary="Hendersons",
                              desc="pin: 2468"))
        self.assertEqual(self.access.calls, ["pin"])
        self.assertEqual(v["pin"], "2468")
        self.assertEqual(self.e.pin_of(self.stay()), ("2468", "manual"))

    def test_owner_visit_gets_no_visitor_or_pin(self):
        self.sync(self.events(desc="type: owner\npin: 2468\nbring skis"),
                  at=ts(12, 10))
        r = self.stay()
        self.assertEqual(self.access.calls, [])
        self.assertEqual(r["access_status"], "owner")
        self.assertEqual(self.e.pin_of(r), (None, "auto"))
        self.assertEqual(r["notes"], "bring skis")
        self.assertTrue(self.e.stay_view(r)["owner"])
        with self.assertRaises(ValueError):
            self.e.set_pin(r["id"], "1357")

    def test_guest_stay_switched_to_owner_drops_visitor(self):
        self.sync(self.events(), at=ts(12, 10))
        self.assertIn("v1", self.access.visitors)
        self.sync(self.events(desc="Type: Owner"))
        self.assertNotIn("v1", self.access.visitors)
        self.assertEqual(self.stay()["access_status"], "owner")

    def test_removed_event_deletes_after_confirmation(self):
        self.sync(self.events(), at=ts(12, 10))
        self.sync([])
        self.assertIn("v1", self.access.visitors)       # 1 miss: keep
        self.sync([])
        self.assertEqual(self.access.visitors, {})
        self.assertEqual(self.stay()["access_status"], "removed")
        self.assertIsNone(self.e.current_and_next()[1])
        # A calendar read failure never counts as a removal.
        self.sync(self.events())
        self.assertEqual(len(self.access.visitors), 1)
        self.sync(None)
        self.sync(None)
        self.assertEqual(len(self.access.visitors), 1)

    def test_cleanup_after_checkout(self):
        self.sync(self.events(), at=ts(12, 10))
        self.sync(self.events(), at=ts(12, 27, 12, 30))   # in late window
        self.assertIn("v1", self.access.visitors)
        self.sync(self.events(), at=ts(12, 27, 13))       # +late +cleanup
        self.assertEqual(self.access.visitors, {})
        self.assertEqual(self.stay()["access_status"], "expired")
        self.sync(self.events(), at=ts(12, 28))
        self.assertEqual(self.access.calls.count("create"), 1)
        # Purged after retention.
        self.sync([], at=ts(12, 27, 11) + 31 * D)
        self.assertNotIn("u1", self.e.stays)

    def test_restart_is_idempotent(self):
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "_db_tmp")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        try:
            path = os.path.join(d, "bdl_stays.db")
            st = bs_engine.SqliteStore(path)
            self.e = self.make(store=st)
            evs = self.events() + [ev("Smith", L(12, 12, 16), L(12, 14, 11),
                                      uid="u2", desc="pin: 8642")]
            self.sync(evs, at=ts(12, 10))
            pins = {k: self.e.pin_of(r)[0] for k, r in self.e.stays.items()}
            self.assertEqual(self.access.calls.count("create"), 2)
            st.close()
            for _ in range(2):              # two restarts
                st = bs_engine.SqliteStore(path)
                self.e = self.make(store=st)
                self.sync(evs, at=self.clock() + 300)
                st.close()
            self.assertEqual(self.access.calls.count("create"), 2)
            self.assertEqual(self.access.calls.count("pin"), 2)
            self.assertEqual(len(self.access.visitors), 2)
            self.assertEqual(
                {k: self.e.pin_of(r)[0] for k, r in self.e.stays.items()},
                pins)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_adopts_visitor_created_before_crash(self):
        self.access.visitors["vX"] = {
            "first": "Henderson family", "last": "", "start": 0, "end": 0,
            "resources": [], "remarks": "bdl_stays:u1 (managed by AppDaemon)",
            "pin": None}
        self.sync(self.events(), at=ts(12, 10))
        self.assertNotIn("create", self.access.calls)
        self.assertEqual(self.stay()["visitor_id"], "vX")
        self.assertEqual(self.access.visitors["vX"]["start"], ts(12, 20, 14))
        self.assertIsNotNone(self.access.visitors["vX"]["pin"])

    def test_visitor_deleted_in_access_is_recreated(self):
        self.sync(self.events(), at=ts(12, 10))
        del self.access.visitors["v1"]
        self.sync(self.events(start=L(12, 21, 16)))
        self.assertEqual(self.stay()["visitor_id"], "v2")
        self.assertIsNotNone(self.access.visitors["v2"]["pin"])

    def test_hash_id_edit_keeps_visitor_and_pin(self):
        e1 = [ev("Smith", L(12, 20, 16), L(12, 22, 11), uid=None)]
        self.sync(e1, at=ts(12, 10))
        (sid,) = self.e.stays
        pin = self.e.pin_of(self.e.stays[sid])[0]
        e2 = [ev("Smith", L(12, 21, 16), L(12, 23, 11), uid=None)]
        self.sync(e2)
        (sid2,) = self.e.stays
        self.assertNotEqual(sid, sid2)
        self.assertEqual(self.e.pin_of(self.e.stays[sid2])[0], pin)
        self.assertEqual(self.access.calls.count("create"), 1)
        self.assertNotIn("delete", self.access.calls)
        self.assertEqual(self.access.visitors["v1"]["start"], ts(12, 21, 14))

    def test_auto_pin_collision_picks_new_pin(self):
        self.sync(self.events(), at=ts(12, 1))
        first = self.e.pin_of(self.stay())[0]
        self.access.used_pins.add(first)
        self.sync(self.events(), at=ts(12, 10))
        pin = self.e.pin_of(self.stay())[0]
        self.assertNotEqual(pin, first)
        self.assertEqual(self.access.visitors["v1"]["pin"], pin)
        self.assertEqual(self.notes, [])

    def test_manual_pin_collision_notifies_once(self):
        self.access.used_pins.add("2468")
        for h in range(3):
            self.sync(self.events(desc="pin: 2468"), at=ts(12, 10, h))
        self.assertEqual(self.stay()["access_status"], "error")
        self.assertEqual(len(self.notes), 1)
        self.assertIn("pin conflict", self.notes[0][0])
        self.access.used_pins.clear()
        self.sync(self.events(desc="pin: 2468"))
        self.assertEqual(self.stay()["access_status"], "active")
        self.assertIsNone(self.stay()["last_error"])

    def test_access_errors_notify_once_per_type(self):
        self.access.fail["create"] = AccessError("POST /visitors: HTTP 500",
                                                 code="HTTP_500")
        for h in range(3):
            self.sync(self.events(), at=ts(12, 10, h))
        self.assertEqual(len(self.notes), 1)
        self.assertEqual(self.stay()["access_status"], "error")
        self.access.fail.clear()
        self.sync(self.events())
        self.assertEqual(self.stay()["access_status"], "active")
        self.access.fail["update"] = AccessError("PUT: HTTP 500",
                                                 code="HTTP_500")
        self.sync(self.events(start=L(12, 21, 16)))
        self.assertEqual(len(self.notes), 2)     # cleared, so notifies again

    def test_bad_manual_pin_falls_back_and_notifies(self):
        self.sync(self.events(desc="pin: 12ab"), at=ts(12, 10))
        pin, src = self.e.pin_of(self.stay())
        self.assertEqual(src, "auto")
        self.assertEqual(self.access.visitors["v1"]["pin"], pin)
        self.sync(self.events(desc="pin: 12ab"))
        self.assertEqual(len(self.notes), 1)
        self.assertIn("bad pin", self.notes[0][0])

    def test_set_and_regenerate_pin(self):
        self.sync(self.events(), at=ts(12, 10))
        with self.assertRaises(ValueError):
            self.e.set_pin("u1", "12")
        with self.assertRaises(ValueError):
            self.e.set_pin("nope", "4444")
        self.e.set_pin("u1", "97531")
        self.assertEqual(self.access.visitors["v1"]["pin"], "97531")
        self.assertEqual(self.e.pin_of(self.stay()), ("97531", "manual"))
        self.e.regenerate_pin("u1")
        pin, src = self.e.pin_of(self.stay())
        self.assertEqual(src, "auto")
        self.assertNotEqual(pin, "97531")
        self.assertEqual(self.access.visitors["v1"]["pin"], pin)
        # A new calendar PIN beats the auto PIN and drops an owner override.
        self.e.set_pin("u1", "86420")
        self.sync(self.events(desc="pin: 1357"))
        self.assertEqual(self.e.pin_of(self.stay()), ("1357", "manual"))
        self.assertEqual(self.access.visitors["v1"]["pin"], "1357")

    def test_pins_never_logged(self):
        self.access.used_pins.add("2468")
        self.sync(self.events(desc="pin: 2468"), at=ts(12, 10))
        self.access.used_pins.clear()
        self.e.set_pin("u1", "97531")
        self.e.regenerate_pin("u1")
        pins = {"2468", "97531", self.e.pin_of(self.stay())[0]}
        text = " ".join(m for _, m in self.logs) + " ".join(
            t + m for t, m in self.notes)
        for p in pins:
            self.assertNotIn(p, text)
        st = self.e.status()
        self.assertTrue(all(set(s["pin"]) == {"*"} for s in st["stays"]))

    def test_no_access_mode(self):
        self.e = self.make(access=None)
        self.sync(self.events(desc="pin: 2468\nlate arrival"),
                  at=ts(12, 20, 17))
        cur = self.e.sensors()["sensor.bdl_stay_current"]
        self.assertEqual(cur[0], "Henderson family")
        self.assertEqual(cur[1]["access_status"], "disabled")
        self.assertEqual(cur[1]["access_mode"], "disabled")
        self.assertEqual(cur[1]["pin"], "2468")
        self.assertEqual(cur[1]["notes"], "late arrival")
        self.assertEqual(self.access.calls, [])

    def test_sensors(self):
        evs = self.events() + [ev("Smith", L(12, 28, 16), L(12, 30, 11),
                                  uid="u2", desc="pin: 8642")]
        self.sync(evs, at=ts(12, 21))
        s = self.e.sensors()
        cur = s["sensor.bdl_stay_current"]
        nxt = s["sensor.bdl_stay_next"]
        self.assertEqual(cur[0], "Henderson family")
        self.assertEqual(cur[1]["arrival"], "2026-12-20T16:00:00-07:00")
        self.assertEqual(cur[1]["checkout"], "2026-12-27T11:00:00-07:00")
        self.assertEqual(cur[1]["pin_source"], "auto")
        self.assertEqual(cur[1]["access_status"], "active")
        self.assertEqual(nxt[0], "Smith")
        self.assertEqual(nxt[1]["pin"], "8642")
        self.assertEqual(nxt[1]["pin_source"], "manual")
        self.sync(evs, at=ts(12, 31))
        s = self.e.sensors()
        self.assertEqual(s["sensor.bdl_stay_current"][0], "none")
        self.assertIsNone(s["sensor.bdl_stay_next"][1]["guest"])


# -------------------------------------------------------------------- modes
class ModesTests(Base):
    def test_disabled_only_logs(self):
        self.modes_enabled = False
        self.e = self.make()
        evs = [ev("A", L(12, 3, 16), L(12, 5, 11))]
        self.sync(evs, at=ts(12, 1, 17))                # 47 h before arrival
        self.sync(evs, at=ts(12, 1, 18))
        self.sync(evs, at=ts(12, 5, 12))                # checkout + 1 h
        self.assertEqual(self.modes.cmds, [])
        would = [m for _, m in self.logs if "would" in m]
        self.assertEqual(len(would), 2)
        self.assertIn("would preheat", would[0])
        self.assertIn("would vacation", would[1])
        # Enabling later sends real requests for still-relevant decisions.
        self.e.modes_enabled = True
        self.sync(evs, at=ts(12, 5, 12, 10))
        self.assertEqual([c for c, _ in self.modes.cmds], ["vacation"])

    def test_preheat_window_and_once(self):
        evs = [ev("A", L(12, 3, 16), L(12, 5, 11))]
        self.sync(evs, at=ts(12, 1, 15))                # 49 h: too early
        self.assertEqual(self.modes.cmds, [])
        self.sync(evs, at=ts(12, 1, 16))                # 48 h
        self.assertEqual(self.modes.cmds, [("preheat", {
            "arrival": "2026-12-03T16:00:00-07:00",
            "request_id": "bdl_stays:preheat:u1:%d" % ts(12, 3, 16)})])
        self.sync(evs, at=ts(12, 1, 17))
        self.assertEqual(len(self.modes.cmds), 1)
        # Arrival moved: update_arrival on the active preheat.
        evs = [ev("A", L(12, 3, 19), L(12, 5, 11))]
        self.sync(evs)
        self.assertEqual(self.modes.cmds[-1], (
            "update_arrival", {"arrival": "2026-12-03T19:00:00-07:00"}))
        self.assertEqual(self.modes.active["arrival"], ts(12, 3, 19))

    def test_existing_preheat_for_same_arrival(self):
        self.modes.active = {"kind": "preheat", "status": "scheduled",
                             "arrival": ts(12, 3, 16)}
        self.sync([ev("A", L(12, 3, 16), L(12, 5, 11))], at=ts(12, 2))
        self.assertEqual(self.modes.cmds, [])
        self.assertEqual(self.stay()["modes"]["preheat"]["result"],
                         "already")

    def test_finished_preheat_is_not_updated(self):
        self.modes.active = {"kind": "preheat", "status": "done",
                             "arrival": ts(11, 20, 16)}
        self.sync([ev("A", L(12, 3, 16), L(12, 5, 11))], at=ts(12, 2))
        self.assertEqual([c for c, _ in self.modes.cmds], ["preheat"])

    def test_vacation_after_checkout(self):
        evs = [ev("A", L(12, 1, 16), L(12, 3, 11))]
        self.sync(evs, at=ts(12, 3, 11, 30))
        self.assertEqual(self.modes.cmds, [])
        self.sync(evs, at=ts(12, 3, 12))
        self.assertEqual(self.modes.cmds, [("vacation", {
            "request_id": "bdl_stays:vacation:u1:%d" % ts(12, 3, 11)})])
        self.sync(evs, at=ts(12, 3, 13))
        self.assertEqual(len(self.modes.cmds), 1)

    def test_old_checkout_not_sent(self):
        self.sync([ev("A", L(11, 25, 16), L(11, 28, 11))], at=ts(12, 1))
        self.assertEqual(self.modes.cmds, [])

    def test_back_to_back_skips_vacation(self):
        evs = [ev("A", L(12, 1, 16), L(12, 3, 11)),
               ev("B", L(12, 3, 16), L(12, 6, 11), uid="u2")]
        self.sync(evs, at=ts(12, 2))
        self.assertEqual(self.modes.cmds, [])      # occupied: no preheat
        self.sync(evs, at=ts(12, 3, 11, 30))
        self.assertEqual(self.modes.cmds, [])      # vacation decision pending
        self.sync(evs, at=ts(12, 3, 12))
        self.assertEqual([c for c, _ in self.modes.cmds], ["preheat"])
        self.assertEqual(self.stay()["modes"]["vacation"]["result"],
                         "skipped_back_to_back")

    def test_gap_vacation_then_preheat_after_settle(self):
        evs = [ev("A", L(12, 1, 16), L(12, 3, 11)),
               ev("B", L(12, 4, 17), L(12, 6, 11), uid="u2")]   # 30 h gap
        self.sync(evs, at=ts(12, 3, 12))
        self.assertEqual([c for c, _ in self.modes.cmds], ["vacation"])
        self.sync(evs, at=ts(12, 3, 12, 30))
        self.assertEqual(len(self.modes.cmds), 1)  # settling
        self.sync(evs, at=ts(12, 3, 13))
        self.assertEqual([c for c, _ in self.modes.cmds],
                         ["vacation", "preheat"])

    def test_modes_failure_notifies_once_and_retries(self):
        self.modes.fail = "HTTP 500: boom"
        evs = [ev("A", L(12, 3, 16), L(12, 5, 11))]
        self.sync(evs, at=ts(12, 2))
        self.sync(evs, at=ts(12, 2, 1))
        self.assertEqual(len(self.notes), 1)
        self.assertIn("preheat", self.notes[0][0])
        self.modes.fail = None
        self.sync(evs, at=ts(12, 2, 2))
        self.assertEqual([c for c, _ in self.modes.cmds], ["preheat"])
        self.assertIsNone(self.stay()["last_error"])

    def test_removed_stay_gets_no_vacation(self):
        evs = [ev("A", L(12, 1, 16), L(12, 3, 11))]
        self.sync(evs, at=ts(12, 2))
        self.sync([], at=ts(12, 2, 1))
        self.sync([], at=ts(12, 2, 2))
        self.sync([], at=ts(12, 3, 12))
        self.assertEqual(self.modes.cmds, [])

    def _preheated(self):
        evs = [ev("A", L(12, 3, 16), L(12, 5, 11))]
        self.sync(evs, at=ts(12, 2))
        self.assertEqual([c for c, _ in self.modes.cmds], ["preheat"])
        return evs

    def _remove(self, *at):
        self.sync([], at=ts(*at))
        self.sync([], at=ts(*at, 1))        # removal_confirmations

    def test_removed_stay_cancels_unstarted_preheat(self):
        self._preheated()
        self._remove(12, 2, 1)
        self.assertEqual([c for c, _ in self.modes.cmds],
                         ["preheat", "cancel"])
        self.assertEqual(self.notes, [])
        self.sync([], at=ts(12, 2, 3))
        self.assertEqual(len(self.modes.cmds), 2)

    def test_removed_stay_reverts_started_preheat(self):
        self._preheated()
        self.modes.active["groups"][0]["status"] = "done"
        self._remove(12, 2, 1)
        self.assertEqual(self.modes.cmds[-1], ("vacation", {
            "request_id": "bdl_stays:withdraw:u1:%d" % ts(12, 3, 16)}))

    def test_removed_stay_leaves_foreign_operation(self):
        self._preheated()
        self.modes.active = {"id": "manual", "kind": "preheat",
                             "status": "scheduled", "arrival": ts(12, 3, 16)}
        self._remove(12, 2, 1)
        self.assertEqual([c for c, _ in self.modes.cmds], ["preheat"])

    def test_adopted_preheat_is_not_cancelled(self):
        self.modes.active = {"id": "manual", "kind": "preheat",
                             "status": "scheduled", "arrival": ts(12, 3, 16)}
        self.sync([ev("A", L(12, 3, 16), L(12, 5, 11))], at=ts(12, 2))
        self._remove(12, 2, 1)
        self.assertEqual(self.modes.cmds, [])

    def test_manual_change_is_not_overridden(self):
        self._preheated()
        self.modes.active["status"] = "cancelled"      # owner cancelled it
        self.sync([ev("A", L(12, 3, 19), L(12, 5, 11))], at=ts(12, 2, 1))
        self.assertEqual([c for c, _ in self.modes.cmds], ["preheat"])
        self.assertEqual(self.stay()["modes"]["preheat"]["result"],
                         "overridden")
        self.sync([ev("A", L(12, 3, 19), L(12, 5, 11))], at=ts(12, 2, 2))
        self.assertEqual(len(self.modes.cmds), 1)


if __name__ == "__main__":
    unittest.main()
