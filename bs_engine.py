"""Stay engine for bdl_stays (pure Python, no AppDaemon imports).

The HA calendar is the source of truth. Each ``sync(events)`` call:

1. parses the calendar events into stays (guest, arrival, checkout, PIN,
   notes) and merges them into the persisted stay records;
2. reconciles each stay's UniFi Access visitor (create / update / PIN /
   delete) through an ``AccessClient`` (``bs_access``), or does nothing in
   "no-access" mode;
3. decides bdl_modes preheat / vacation requests (only sent when
   ``modes_enabled``; otherwise logged as "would ...").

Everything is idempotent: the visitor id, the PIN and what was last pushed
to Access are stored per stay, so re-running after a restart changes
nothing. PINs are never logged; use ``mask()``.
"""

import datetime as dt
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time

DEFAULTS = {
    "arrival_time": "16:00",        # all-day events
    "checkout_time": "11:00",
    "allday_end_inclusive": True,   # all-day event spans arrival..departure
    "lookback_days": 14,            # calendar window before now
    "horizon_days": 60,             # calendar window after now
    "create_days_before": 14,       # create the Access visitor this early
    "early_access_h": 2.0,          # PIN works from arrival - early
    "late_access_h": 1.0,           # ... until checkout + late
    "cleanup_delay_h": 1.0,         # delete visitor at access end + this
    "pin_length": 4,
    "pin_blocklist": [],            # e.g. owner / staff codes
    "preheat_horizon_h": 48.0,
    "vacation_delay_h": 1.0,
    "back_to_back_h": 24.0,
    "vacation_max_late_h": 12.0,    # don't send a vacation this overdue
    "preheat_settle_min": 60.0,     # gap between our vacation and preheat
    "removal_confirmations": 2,     # polls an event must be missing
    "retention_days": 30,           # keep finished stays this long
}

H = 3600.0
FINAL_OPS = ("done", "failed", "cancelled", "superseded")
PIN_RE = re.compile(
    r"^\s*(?:door\s*)?pin(?:\s*code)?\b\s*[:=#]?\s*(.*?)\s*$", re.I)
TYPE_RE = re.compile(r"^\s*(?:stay\s*)?type\s*[:=]\s*(guest|owner)\s*$", re.I)


class ModesError(Exception):
    """A bdl_modes call failed."""


def mask(pin):
    return "*" * len(str(pin)) if pin else "-"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()
                          ).hexdigest()[:16]


# ------------------------------------------------------------------- PINs
def is_trivial(pin):
    """Same digit, straight runs (with wrap) or short repeating patterns."""
    d = [int(c) for c in pin]
    if len(set(d)) == 1:
        return True
    steps = {(b - a) % 10 for a, b in zip(d, d[1:])}
    if steps in ({1}, {9}):
        return True
    for period in (2, 3):
        if len(pin) > period and len(pin) % period == 0 and \
                pin == pin[:period] * (len(pin) // period):
            return True
    return False


def manual_pin_error(pin, blocklist=()):
    """Reason a guest/owner supplied PIN is unusable, else None. Trivial
    PINs are allowed here (the guest chose them)."""
    if not re.fullmatch(r"\d{4,8}", pin or ""):
        return "PIN must be 4-8 digits"
    if pin in {str(b) for b in blocklist}:
        return "PIN is on the blocklist"
    return None


def generate_pin(length=4, blocklist=(), avoid=(), rng=None):
    rng = rng or secrets.SystemRandom()
    bad = {str(b) for b in blocklist} | {str(a) for a in avoid if a}
    for _ in range(1000):
        pin = "".join(str(rng.randrange(10)) for _ in range(int(length)))
        if not is_trivial(pin) and pin not in bad:
            return pin
    raise RuntimeError("could not generate a PIN")


# ---------------------------------------------------------------- parsing
def _hhmm(s):
    h, m = str(s).split(":")[:2]
    return dt.time(int(h), int(m))


def _when(v, tz):
    """-> (aware datetime | date, is_all_day). Accepts the calendar
    service's strings, the REST API's {"date"/"dateTime"} dicts and
    date/datetime objects."""
    if isinstance(v, dict):
        v = v.get("dateTime") or v.get("date")
    if isinstance(v, dt.datetime):
        return (v if v.tzinfo else v.replace(tzinfo=tz)), False
    if isinstance(v, dt.date):
        return v, True
    s = str(v or "").strip()
    if not s:
        raise ValueError("missing start/end")
    if len(s) == 10:
        return dt.date.fromisoformat(s), True
    t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (t if t.tzinfo else t.replace(tzinfo=tz)), False


def parse_description(text):
    """-> (pin or None, pin_error or None, notes)."""
    pin, err, notes = None, None, []
    for line in str(text or "").splitlines():
        if TYPE_RE.match(line):
            continue
        m = PIN_RE.match(line)
        if m and pin is None and err is None:
            raw = re.sub(r"[\s-]", "", m.group(1))
            err = manual_pin_error(raw)
            pin = None if err else raw
            continue
        notes.append(line)
    return pin, err, "\n".join(notes).strip()


def is_owner_visit(text):
    """A ``type: owner`` line marks the owners' own visit."""
    for line in str(text or "").splitlines():
        m = TYPE_RE.match(line)
        if m:
            return m.group(1).lower() == "owner"
    return False


def stay_id(ev, summary, start_raw):
    uid = ev.get("uid")
    if uid:
        rid = ev.get("recurrence_id")
        return ("%s@%s" % (uid, rid)) if rid else str(uid), "uid"
    if isinstance(start_raw, dict):
        start_raw = start_raw.get("dateTime") or start_raw.get("date")
    key = "%s|%s" % (summary.strip().lower(), start_raw)
    return "h" + hashlib.sha1(key.encode()).hexdigest()[:12], "hash"


def parse_event(ev, tz, cfg=None):
    """Calendar event -> stay dict. Raises ValueError for unusable events."""
    c = dict(DEFAULTS)
    c.update(cfg or {})
    summary = str(ev.get("summary") or "").strip() or "Guest"
    start, s_day = _when(ev.get("start"), tz)
    end, e_day = _when(ev.get("end"), tz)
    all_day = s_day or e_day
    if s_day:
        arrival = dt.datetime.combine(start, _hhmm(c["arrival_time"]), tz)
    else:
        arrival = start
    if e_day:
        out = end
        if c["allday_end_inclusive"]:
            out = end - dt.timedelta(days=1)
            if s_day and out <= start:
                out = end       # single-day event = one night
        checkout = dt.datetime.combine(out, _hhmm(c["checkout_time"]), tz)
    else:
        checkout = end
    if checkout <= arrival:
        raise ValueError("checkout is not after arrival")
    pin, pin_err, notes = parse_description(ev.get("description"))
    owner = is_owner_visit(ev.get("description"))
    if owner:
        # Owners use their own codes: no visitor, so no PIN to manage.
        pin, pin_err = None, None
    if pin and pin in {str(b) for b in c["pin_blocklist"]}:
        pin, pin_err = None, "PIN is on the blocklist"
    sid, source = stay_id(ev, summary, ev.get("start"))
    return {"id": sid, "id_source": source, "guest": summary,
            "arrival": arrival.timestamp(), "checkout": checkout.timestamp(),
            "all_day": all_day, "notes": notes, "calendar_pin": pin,
            "calendar_pin_error": pin_err, "owner": owner}


# ------------------------------------------------------------------ store
class MemoryStore:
    def __init__(self):
        self.stays, self.kv = {}, {}

    def load_all(self):
        return json.loads(json.dumps(self.stays))

    def put(self, rec):
        self.stays[rec["id"]] = json.loads(json.dumps(rec))

    def delete(self, sid):
        self.stays.pop(sid, None)

    def get_kv(self, k, default=None):
        return self.kv.get(k, default)

    def set_kv(self, k, v):
        self.kv[k] = v


class SqliteStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS stays (id TEXT PRIMARY"
                        " KEY, body TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY,"
                        " v TEXT)")
        self.db.commit()
        self.lock = threading.Lock()

    def load_all(self):
        with self.lock:
            rows = self.db.execute("SELECT id, body FROM stays").fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}

    def put(self, rec):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO stays VALUES (?, ?)",
                            (rec["id"], json.dumps(rec)))
            self.db.commit()

    def delete(self, sid):
        with self.lock:
            self.db.execute("DELETE FROM stays WHERE id=?", (sid,))
            self.db.commit()

    def get_kv(self, k, default=None):
        with self.lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_kv(self, k, v):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)",
                            (k, json.dumps(v)))
            self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()


# ----------------------------------------------------------------- engine
class Engine:
    def __init__(self, store, access, modes, notify, log, tz,
                 clock=time.time, config=None, modes_enabled=False,
                 rng=None):
        """store:  MemoryStore / SqliteStore interface
        access: bs_access.AccessClient or None (no-access mode)
        modes:  object with status() -> dict and command(cmd, **data) ->
                dict, raising ModesError; or None
        notify: callable(title, message)
        log:    callable(msg, level="INFO")"""
        self.store, self.access, self.modes = store, access, modes
        self.notify, self.log, self.tz, self.clock = notify, log, tz, clock
        self.cfg = dict(DEFAULTS)
        self.cfg.update(config or {})
        self.modes_enabled = bool(modes_enabled)
        self.rng = rng
        self.lock = threading.RLock()
        self.stays = store.load_all()
        self.last_sync = store.get_kv("last_sync")
        self.calendar_error = store.get_kv("calendar_error")

    # -------------------------------------------------------------- helpers
    def iso(self, ts):
        return dt.datetime.fromtimestamp(ts, self.tz).isoformat() \
            if ts is not None else None

    def _label(self, r):
        return "%s (%s)" % (r["guest"], dt.datetime.fromtimestamp(
            r["arrival"], self.tz).strftime("%b %d"))

    def _save(self, r):
        r["updated"] = self.clock()
        self.store.put(r)

    def _fail(self, r, kind, msg):
        errs = r.setdefault("errors", {})
        if errs.get(kind) != msg:   # don't repeat the same line every poll
            self.log("%s: %s failed: %s" % (self._label(r), kind, msg),
                     "WARNING")
        errs[kind] = msg
        r["last_error"] = "%s: %s" % (kind, msg)
        notified = r.setdefault("notified", [])
        if kind not in notified:
            notified.append(kind)
            self.notify("BDL stay: %s problem" % kind.replace("_", " "),
                        "%s: %s" % (self._label(r), msg))

    def _ok(self, r, *kinds):
        errs = r.setdefault("errors", {})
        for k in kinds:
            errs.pop(k, None)
            if k in r.get("notified", []):
                r["notified"].remove(k)
        if not errs:
            r["last_error"] = None

    def _pin_taken_by_others(self, r):
        return {self.pin_of(o)[0] for o in self.stays.values()
                if o["id"] != r["id"] and not o.get("removed")
                and o.get("access_status") not in ("expired", "removed")}

    def _new_auto_pin(self, r):
        return generate_pin(self.cfg["pin_length"], self.cfg["pin_blocklist"],
                            self._pin_taken_by_others(r), self.rng)

    def pin_of(self, r):
        """-> (pin, "manual"|"auto")."""
        ov = r.get("pin_override")
        if ov:
            return ov["pin"], ov["source"]
        if r.get("calendar_pin"):
            return r["calendar_pin"], "manual"
        return r.get("auto_pin"), "auto"

    # ----------------------------------------------------------------- sync
    def sync(self, events):
        """events: raw calendar events, or None if the calendar could not
        be read (then nothing is treated as removed)."""
        with self.lock:
            now = self.clock()
            if events is not None:
                self._merge(events, now)
                self.last_sync = now
                self.store.set_kv("last_sync", now)
            for r in sorted(self.stays.values(), key=lambda x: x["arrival"]):
                self._reconcile_access(r, now)
            self._modes_pass(now)
            self._purge(now)
            for r in self.stays.values():
                self._save(r)
            return self.status()

    def calendar_failed(self, msg):
        with self.lock:
            self.log("calendar read failed: %s" % msg, "WARNING")
            if not self.calendar_error:
                self.notify("BDL stays: calendar unreadable", str(msg)[:200])
            self.calendar_error = str(msg)[:200]
            self.store.set_kv("calendar_error", self.calendar_error)

    def calendar_ok(self):
        with self.lock:
            if self.calendar_error:
                self.calendar_error = None
                self.store.set_kv("calendar_error", None)

    def window(self, now=None):
        now = self.clock() if now is None else now
        return (now - self.cfg["lookback_days"] * 86400.0,
                now + self.cfg["horizon_days"] * 86400.0)

    def _merge(self, events, now):
        parsed = {}
        for ev in events:
            try:
                s = parse_event(ev, self.tz, self.cfg)
            except (ValueError, TypeError) as exc:
                self.log("ignoring calendar event %r: %s" % (
                    str(ev.get("summary"))[:40], exc), "WARNING")
                continue
            parsed[s["id"]] = s
        w0, w1 = self.window(now)
        missing = [r for sid, r in self.stays.items()
                   if sid not in parsed and not r.get("removed")
                   and r["checkout"] > w0 and r["arrival"] < w1]
        # An edited event without a uid gets a new hash id: carry the old
        # record (visitor, PIN) over instead of deleting and recreating.
        for sid in [k for k in parsed if k not in self.stays]:
            s = parsed[sid]
            cands = [r for r in missing if r["checkout"] > now and
                     r["guest"].strip().lower() == s["guest"].lower()]
            if not cands:
                continue
            old = min(cands, key=lambda r: abs(r["arrival"] - s["arrival"]))
            missing.remove(old)
            self.store.delete(old["id"])
            del self.stays[old["id"]]
            self.log("%s: event changed id, keeping its visitor/PIN" %
                     self._label(old))
            old["id"] = sid
            self.stays[sid] = old
        for sid, s in parsed.items():
            r = self.stays.get(sid)
            if r is None:
                r = {"id": sid, "created": now, "access_status": "new",
                     "visitor_id": None, "synced": None, "synced_pin": None,
                     "auto_pin": None, "pin_override": None, "removed": False,
                     "missing": 0, "modes": {}, "errors": {},
                     "notified": [], "last_error": None,
                     "calendar_pin": None, "calendar_pin_error": None}
                self.stays[sid] = r
                self.log("new stay %s" % self._label(dict(r, **s)))
            elif (r["arrival"], r["checkout"], r["guest"]) != (
                    s["arrival"], s["checkout"], s["guest"]):
                self.log("stay %s changed" % self._label(s))
            if r.get("calendar_pin") != s["calendar_pin"] and \
                    r.get("pin_override"):
                self.log("%s: calendar PIN changed, dropping override" %
                         self._label(s))
                r["pin_override"] = None
            if r.get("removed"):
                self.log("stay %s is back in the calendar" % self._label(s))
            r.update(s)
            r["removed"] = False
            r["missing"] = 0
            if s["calendar_pin_error"]:
                self._fail(r, "bad_pin", "%s; using a generated PIN instead"
                           % s["calendar_pin_error"])
            else:
                self._ok(r, "bad_pin")
            if s["owner"]:
                r["auto_pin"], r["pin_override"] = None, None
            elif not self.pin_of(r)[0]:
                r["auto_pin"] = self._new_auto_pin(r)
        need = int(self.cfg["removal_confirmations"])
        for r in missing:
            r["missing"] = r.get("missing", 0) + 1
            if r["missing"] >= need:
                r["removed"] = True
                r["removed_at"] = now
                self.log("stay %s removed from the calendar" % self._label(r))

    # --------------------------------------------------------------- access
    def _visitor_fields(self, r, resources):
        a = dt.datetime.fromtimestamp(r["arrival"], self.tz)
        return {"first_name": r["guest"][:60],
                "last_name": "Stay %s %d" % (a.strftime("%b"), a.day),
                "start": int(r["arrival"] - self.cfg["early_access_h"] * H),
                "end": int(r["checkout"] + self.cfg["late_access_h"] * H),
                "resources": resources,
                "remarks": "bdl_stays:%s (managed by AppDaemon)" % r["id"]}

    def _reconcile_access(self, r, now):
        if self.access is None:
            r["access_status"] = "disabled"
            return
        access_end = r["checkout"] + self.cfg["late_access_h"] * H
        try:
            if r.get("removed") or r.get("owner") or now >= access_end + \
                    self.cfg["cleanup_delay_h"] * H:
                if r.get("visitor_id"):
                    self.access.delete_visitor(r["visitor_id"])
                    self.log("%s: visitor deleted" % self._label(r))
                    r["visitor_id"], r["synced"], r["synced_pin"] = \
                        None, None, None
                r["access_status"] = "removed" if r.get("removed") \
                    else "owner" if r.get("owner") else "expired"
                self._ok(r, "access", "access_delete", "pin_conflict")
                return
            due = r["arrival"] - self.cfg["create_days_before"] * 86400.0
            if now < due and not r.get("visitor_id"):
                r["access_status"] = "scheduled"
                return
            self._push_visitor(r)
            r["access_status"] = "active"
            self._ok(r, "access")
        except Exception as exc:  # noqa: BLE001
            kind = "access_delete" if (r.get("removed") or
                                       now >= access_end) else "access"
            if getattr(exc, "pin_taken", False):
                kind = "pin_conflict"
            r["access_status"] = "error"
            msg = str(exc) if exc.__class__.__name__ == "AccessError" \
                else "%s: %s" % (type(exc).__name__, exc)
            self._fail(r, kind, msg)

    def _push_visitor(self, r):
        f = self._visitor_fields(r, self.access.resources())
        fp = fingerprint(f)
        args = (f["first_name"], f["last_name"], f["start"], f["end"],
                f["resources"], f["remarks"])
        marker = "bdl_stays:%s " % r["id"]
        if not r.get("visitor_id"):
            vid = self.access.find_visitor(f["first_name"], marker)
            if vid:
                self.log("%s: adopted existing visitor" % self._label(r))
                r["synced"] = None
            else:
                vid = self.access.create_visitor(*args)
                self.log("%s: visitor created" % self._label(r))
                r["synced"] = fp
            r["visitor_id"], r["synced_pin"] = vid, None
            self._save(r)           # crash-safe: never create twice
        if r["synced"] != fp:
            try:
                self.access.update_visitor(r["visitor_id"], *args)
            except Exception as exc:  # noqa: BLE001
                if not getattr(exc, "not_found", False):
                    raise
                self.log("%s: visitor vanished from Access, recreating" %
                         self._label(r), "WARNING")
                r["visitor_id"] = self.access.create_visitor(*args)
                r["synced_pin"] = None
                self._save(r)
            r["synced"] = fp
            self.log("%s: visitor updated" % self._label(r))
        for _ in range(3):
            pin, source = self.pin_of(r)
            if r.get("synced_pin") == fingerprint(pin):
                return
            try:
                self.access.set_visitor_pin(r["visitor_id"], pin)
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "pin_taken", False) and source == "auto" \
                        and not r.get("pin_override"):
                    self.log("%s: generated PIN already used in Access, "
                             "picking another" % self._label(r), "WARNING")
                    r["auto_pin"] = self._new_auto_pin(r)
                    continue
                raise
            r["synced_pin"] = fingerprint(pin)
            self._ok(r, "pin_conflict")
            self.log("%s: PIN %s set (%s)" % (self._label(r), mask(pin),
                                              source))
            return
        raise RuntimeError("no free PIN after 3 tries")

    # ---------------------------------------------------------------- modes
    def _counts(self, rec):
        return bool(rec) and (not rec.get("dry") or not self.modes_enabled)

    def _withdraw_preheat(self, r, occupied=False):
        """A removed stay's preheat: cancel it if nothing has run yet, or
        send vacation to undo a ramp already under way (unless another stay
        is in the lodge). Only the bdl_modes operation this app started is
        touched."""
        p = r["modes"].get("preheat")
        if not p or not p.get("owned") or p.get("withdrawn") or \
                not self._counts(p) or not self.modes_enabled or \
                self.modes is None:
            return
        label = self._label(r)
        try:
            act = (self.modes.status() or {}).get("active") or {}
            if act.get("id") != p.get("op_id") or \
                    act.get("status") in FINAL_OPS:
                p["withdrawn"] = "inactive"
                self.log("%s: removed; its preheat is no longer active" %
                         label)
                self._ok(r, "preheat_orphan")
                return
            started = any(g.get("status") in ("running", "retrying", "done",
                                              "failed")
                          for g in act.get("groups") or [])
            if started and not occupied:
                self.modes.command("vacation",
                                   request_id="bdl_stays:withdraw:%s:%d" %
                                   (r["id"], p["arrival"]))
                p["withdrawn"] = "vacation"
            else:
                self.modes.command("cancel")
                p["withdrawn"] = "cancel"
        except Exception as exc:  # noqa: BLE001
            self._fail(r, "preheat_orphan", "stay was removed but its "
                       "preheat could not be withdrawn: %s" % exc)
            return
        self._ok(r, "preheat_orphan")
        self.log("%s: removed; bdl_modes %s (preheat withdrawn)" % (
            label, p["withdrawn"]))

    def _modes_pass(self, now):
        occupied = any(r["arrival"] <= now < r["checkout"]
                       for r in self.stays.values() if not r.get("removed"))
        for r in self.stays.values():
            if r.get("removed"):
                self._withdraw_preheat(r, occupied)
        live = [r for r in self.stays.values() if not r.get("removed")]
        current = [r for r in live if r["arrival"] <= now < r["checkout"]]
        delay = self.cfg["vacation_delay_h"] * H
        vacation_pending, last_vac = False, None
        for r in sorted(live, key=lambda x: x["checkout"]):
            if r["checkout"] > now:
                continue
            v = r["modes"].get("vacation")
            if v and v["checkout"] == r["checkout"] and self._counts(v):
                if v["result"] in ("sent", "would_vacation"):
                    last_vac = max(last_vac or 0, v["at"])
                continue
            due = r["checkout"] + delay
            if now < due:
                vacation_pending = True
                continue
            if now > due + self.cfg["vacation_max_late_h"] * H:
                self._record(r, "vacation", "missed", r["checkout"], now)
                self.log("%s: vacation not sent (checkout too long ago)" %
                         self._label(r))
                continue
            b2b = [o for o in live if o is not r and
                   o["checkout"] > r["checkout"] and
                   o["arrival"] < r["checkout"] +
                   self.cfg["back_to_back_h"] * H]
            if b2b:
                self._record(r, "vacation", "skipped_back_to_back",
                             r["checkout"], now)
                self.log("%s: no vacation, %s is back-to-back" % (
                    self._label(r), self._label(b2b[0])))
                continue
            if self._modes_call(r, "vacation", "vacation", r["checkout"],
                                now, request_id="bdl_stays:vacation:%s:%d" %
                                (r["id"], r["checkout"])):
                last_vac = now
            else:
                vacation_pending = True     # failed: retried next sync
        upcoming = sorted([r for r in live if r["arrival"] > now],
                          key=lambda x: x["arrival"])
        if not upcoming:
            return
        n = upcoming[0]
        lead = n["arrival"] - now
        p = n["modes"].get("preheat")
        if p and p["arrival"] == n["arrival"] and self._counts(p):
            return
        # A preheat already requested for this stay follows the arrival
        # even when it moves beyond the horizon.
        moved = bool(p) and self._counts(p)
        if (lead > self.cfg["preheat_horizon_h"] * H and not moved) or \
                lead < 120:
            return
        wait = None
        if current:
            wait = "lodge occupied by %s" % self._label(current[0])
        elif vacation_pending:
            wait = "waiting for the previous stay's vacation"
        elif last_vac and now - last_vac < self.cfg["preheat_settle_min"] * 60:
            wait = "letting the vacation settle"
        if wait:
            if n["modes"].get("preheat_wait") != wait:
                n["modes"]["preheat_wait"] = wait
                self.log("%s: preheat deferred (%s)" % (self._label(n), wait))
            return
        n["modes"].pop("preheat_wait", None)
        self._modes_call(n, "preheat", "preheat", n["arrival"], now)

    def _record(self, r, key, result, ref, now, dry=False, **extra):
        r["modes"][key] = dict({"result": result, "at": now, "dry": dry,
                                ("checkout" if key == "vacation"
                                 else "arrival"): ref}, **extra)

    def _modes_call(self, r, key, cmd, ref, now, request_id=None):
        """Send (or, when disabled, log) a bdl_modes request. -> True if it
        was sent (or would have been), False if it failed."""
        label = self._label(r)
        if not self.modes_enabled or self.modes is None:
            self.log("modes disabled: would %s for %s%s" % (
                cmd, label, " (arrival %s)" % self.iso(ref)
                if key == "preheat" else ""))
            self._record(r, key, "would_" + cmd, ref, now, dry=True)
            return True
        extra = {}
        try:
            if key == "preheat":
                prev = r["modes"].get("preheat")
                prev_op = prev.get("op_id") if self._counts(prev) else None
                owned = bool(prev_op and prev.get("owned"))
                act = (self.modes.status() or {}).get("active") or {}
                live = act.get("kind") == "preheat" and \
                    act.get("status") not in FINAL_OPS
                if prev_op and (act.get("id") != prev_op or not live):
                    # Cancelled, finished or replaced outside this app:
                    # don't fight a manual change.
                    result = "overridden"
                    extra = {"op_id": prev_op, "owned": False}
                    self.log("%s: preheat was changed outside the stays "
                             "app; not following the new arrival" % label)
                elif live:
                    if abs((act.get("arrival") or 0) - ref) < 60:
                        result = "already"
                    else:
                        self.modes.command("update_arrival",
                                           arrival=self.iso(ref))
                        result = "update_arrival"
                    extra = {"op_id": act.get("id"), "owned": owned}
                else:
                    res = self.modes.command(
                        "preheat", arrival=self.iso(ref),
                        request_id="bdl_stays:preheat:%s:%d" % (r["id"],
                                                                ref))
                    result = "preheat"
                    extra = {"op_id": ((res or {}).get("active") or {})
                             .get("id"), "owned": True}
            else:
                self.modes.command(cmd, request_id=request_id)
                result = "sent"
        except Exception as exc:  # noqa: BLE001
            self._fail(r, key, "bdl_modes %s failed: %s" % (cmd, exc))
            return False
        self._record(r, key, result, ref, now, **extra)
        if result == "overridden":
            return True
        self._ok(r, key)
        self.log("%s: bdl_modes %s (%s)" % (label, cmd, result))
        return True

    # -------------------------------------------------------------- cleanup
    def _purge(self, now):
        keep = self.cfg["retention_days"] * 86400.0
        for sid in list(self.stays):
            r = self.stays[sid]
            if r.get("visitor_id") or r.get("access_status") == "error":
                continue
            ref = r.get("removed_at") if r.get("removed") else r["checkout"]
            if (r.get("removed") or r["checkout"] < now) and \
                    now > (ref or now) + keep:
                del self.stays[sid]
                self.store.delete(sid)

    # ------------------------------------------------------------- commands
    def _get(self, sid):
        r = self.stays.get(sid)
        if r is None or r.get("removed"):
            raise ValueError("unknown stay %r" % sid)
        return r

    def set_pin(self, sid, pin):
        """Owner override; empty pin clears the override."""
        with self.lock:
            r = self._get(sid)
            if r.get("owner"):
                raise ValueError("owner visits don't get a door PIN")
            if pin in (None, ""):
                r["pin_override"] = None
                self.log("%s: PIN override cleared" % self._label(r))
            else:
                pin = str(pin).strip()
                err = manual_pin_error(pin, self.cfg["pin_blocklist"])
                if err:
                    raise ValueError(err)
                if pin in self._pin_taken_by_others(r):
                    raise ValueError("PIN is used by another stay")
                r["pin_override"] = {"pin": pin, "source": "manual"}
                self.log("%s: PIN override set (%s)" % (self._label(r),
                                                        mask(pin)))
            if not self.pin_of(r)[0]:
                r["auto_pin"] = self._new_auto_pin(r)
            self._reconcile_access(r, self.clock())
            self._save(r)
            return self.stay_view(r, show_pin=False)

    def regenerate_pin(self, sid):
        with self.lock:
            r = self._get(sid)
            pin = self._new_auto_pin(r)
            if r.get("calendar_pin"):
                r["pin_override"] = {"pin": pin, "source": "auto"}
            else:
                r["pin_override"] = None
                r["auto_pin"] = pin
            self.log("%s: PIN regenerated" % self._label(r))
            self._reconcile_access(r, self.clock())
            self._save(r)
            return self.stay_view(r, show_pin=False)

    def force_resync(self):
        """Re-push every visitor on the next sync (fixes edits made in the
        Access UI)."""
        with self.lock:
            for r in self.stays.values():
                r["synced"], r["synced_pin"] = None, None

    # --------------------------------------------------------------- status
    def stay_view(self, r, show_pin=True):
        pin, source = self.pin_of(r)
        return {"stay_id": r["id"], "guest": r["guest"],
                "owner": bool(r.get("owner")),
                "arrival": self.iso(r["arrival"]),
                "checkout": self.iso(r["checkout"]),
                "all_day": r.get("all_day", False),
                "pin": pin if show_pin else mask(pin),
                "pin_source": source,
                "access_status": r.get("access_status"),
                "access_start": self.iso(
                    r["arrival"] - self.cfg["early_access_h"] * H),
                "access_end": self.iso(
                    r["checkout"] + self.cfg["late_access_h"] * H),
                "notes": r.get("notes") or "",
                "removed": bool(r.get("removed")),
                "last_error": r.get("last_error"),
                "modes": {k: v for k, v in (r.get("modes") or {}).items()}}

    def current_and_next(self, now=None):
        now = self.clock() if now is None else now
        live = sorted([r for r in self.stays.values()
                       if not r.get("removed")], key=lambda x: x["arrival"])
        cur = [r for r in live if r["arrival"] <= now < r["checkout"]]
        nxt = [r for r in live if r["arrival"] > now]
        return (cur[0] if cur else None), (nxt[0] if nxt else None)

    def sensors(self):
        """-> {entity_id: (state, attributes)} for HA."""
        with self.lock:
            cur, nxt = self.current_and_next()
            out = {}
            for eid, r, name, icon in (
                    ("sensor.bdl_stay_current", cur, "BDL Current Stay",
                     "mdi:home-account"),
                    ("sensor.bdl_stay_next", nxt, "BDL Next Stay",
                     "mdi:calendar-account")):
                attrs = {"friendly_name": name, "icon": icon,
                         "access_mode": "enabled" if self.access else
                         "disabled",
                         "modes_enabled": self.modes_enabled}
                keys = ("stay_id", "guest", "owner", "arrival", "checkout",
                        "all_day", "pin", "pin_source", "access_status",
                        "access_start", "access_end", "notes", "last_error")
                if r:
                    v = self.stay_view(r)
                    attrs.update({k: v[k] for k in keys})
                    out[eid] = (r["guest"], attrs)
                else:
                    attrs.update({k: None for k in keys})
                    out[eid] = ("none", attrs)
            return out

    def status(self, show_pins=False):
        with self.lock:
            cur, nxt = self.current_and_next()
            return {
                "access": "enabled" if self.access else "disabled",
                "modes_enabled": self.modes_enabled,
                "last_sync": self.iso(self.last_sync),
                "calendar_error": self.calendar_error,
                "current": cur["id"] if cur else None,
                "next": nxt["id"] if nxt else None,
                "stays": [self.stay_view(r, show_pins) for r in sorted(
                    self.stays.values(), key=lambda x: x["arrival"])]}
