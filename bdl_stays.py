"""bdl_stays - guest stays for the Black Diamond Lodge (Sun Peaks).

Reads the HA calendar ``calendar.bdl_stays`` (one event per stay), gives
each guest a time-limited UniFi Access visitor with a door PIN, asks the
sibling ``bdl_modes`` app to preheat before arrival and go to vacation after
checkout, and publishes ``sensor.bdl_stay_current`` / ``sensor.bdl_stay_next``.
The logic lives in ``bs_engine`` (no AppDaemon imports) and the Access
client in ``bs_access``. See README.md.
"""

import datetime as dt
import hmac
import os

import requests

try:
    import appdaemon.plugins.hass.hassapi as hass
except ImportError:  # unit tests
    hass = None

import bs_access
import bs_engine


class HAClient:
    """Minimal HA REST client. The REST calendar API returns event uids
    (the calendar.get_events action does not), and REST set_state works
    for entities AppDaemon has not seen yet."""

    def __init__(self, url, token, timeout=20):
        self.url = url.rstrip("/")
        self.h = {"Authorization": "Bearer %s" % token}
        self.timeout = timeout

    def calendar_events(self, entity_id, start, end):
        r = requests.get("%s/api/calendars/%s" % (self.url, entity_id),
                         headers=self.h, timeout=self.timeout,
                         params={"start": start, "end": end})
        if r.status_code != 200:
            raise RuntimeError("calendar %s: HTTP %s %s" % (
                entity_id, r.status_code, r.text[:200]))
        return r.json()

    def set_state(self, entity_id, state, attributes):
        r = requests.post("%s/api/states/%s" % (self.url, entity_id),
                          headers=self.h, timeout=self.timeout,
                          json={"state": state, "attributes": attributes})
        r.raise_for_status()


class BdlModesClient:
    """HTTP client for the bdl_modes endpoint (see its README)."""

    def __init__(self, url, api_key, timeout=20):
        self.url, self.key, self.timeout = url, str(api_key), timeout

    def _post(self, body):
        try:
            r = requests.post(self.url, json=body, timeout=self.timeout,
                              headers={"X-API-Key": self.key})
        except requests.RequestException as exc:
            raise bs_engine.ModesError(str(exc)) from None
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        if r.status_code != 200:
            raise bs_engine.ModesError("HTTP %s: %s" % (
                r.status_code, payload.get("error") or r.text[:200]))
        return payload

    def status(self):
        return self._post({"cmd": "status"})

    def command(self, cmd, **data):
        body = {k: v for k, v in data.items() if v is not None}
        body["cmd"] = cmd
        return self._post(body)


def find_events(res, entity_id):
    """Pull the event list out of a calendar.get_events service result
    (AppDaemon wraps it as {"success", "result": {"response": ...}})."""
    if isinstance(res, dict):
        if entity_id in res and isinstance(res[entity_id], dict) and \
                "events" in res[entity_id]:
            return res[entity_id]["events"]
        if res.get("success") is False:
            raise RuntimeError("calendar.get_events failed: %s" % str(
                res.get("error") or res)[:200])
        for v in res.values():
            ev = find_events(v, entity_id)
            if ev is not None:
                return ev
    return None


_Base = hass.Hass if hass else object


class BdlStays(_Base):
    def initialize(self):
        a = self.args
        self.tz = dt.timezone.utc
        try:
            import zoneinfo
            self.tz = zoneinfo.ZoneInfo(a.get("timezone",
                                              "America/Vancouver"))
        except Exception:  # noqa: BLE001
            pass
        self.api_key = str(a["api_key"])
        self.calendar = a.get("calendar_entity", "calendar.bdl_stays")
        self.ha = HAClient(a["ha_url"], a["ha_token"]) \
            if a.get("ha_url") and a.get("ha_token") else None
        self.remote_notify = [r for r in (a.get("remote_notify") or [])
                              if r.get("url") and r.get("token")]
        self.notify_service = a.get("notify_service")

        access = None
        if a.get("access_host") and a.get("access_token"):
            access = bs_access.UnifiAccessClient(
                a["access_host"], a["access_token"],
                door_ids=a.get("door_ids"),
                door_group_ids=a.get("door_group_ids"),
                door_names=a.get("door_names"),
                verify=bool(a.get("access_verify_ssl", False)))
        self.access = access        # kept for cmd "doors" even if unused
        if access and not (a.get("door_ids") or a.get("door_group_ids") or
                           a.get("door_names")):
            self._say("no doors configured: running in no-access mode",
                      "WARNING")
            access = None
        modes = None
        if a.get("bdl_modes_url") and a.get("bdl_modes_api_key"):
            modes = BdlModesClient(a["bdl_modes_url"], a["bdl_modes_api_key"])

        here = os.path.dirname(os.path.abspath(__file__))
        self.store = bs_engine.SqliteStore(
            os.path.join(here, a.get("db", "bdl_stays.db")))
        self.engine = bs_engine.Engine(
            self.store, access, modes, self._push, self._say, self.tz,
            config=a.get("engine"),
            modes_enabled=bool(a.get("modes_enabled", False)))
        self.register_endpoint(self._endpoint, "bdl_stays")
        self.run_every(self._poll, "now+10",
                       int(a.get("poll_interval_s", 300)))
        self._say("initialized (access=%s, modes_enabled=%s, calendar=%s)" % (
            "enabled" if access else "DISABLED (no-access mode)",
            self.engine.modes_enabled, self.calendar))

    def terminate(self):
        self.store.close()

    def _say(self, msg, level="INFO"):
        self.log(msg, level=level)

    # ------------------------------------------------------------- polling
    def _fetch_events(self):
        w0, w1 = self.engine.window()
        start = dt.datetime.fromtimestamp(w0, self.tz).isoformat()
        end = dt.datetime.fromtimestamp(w1, self.tz).isoformat()
        if self.ha:
            return self.ha.calendar_events(self.calendar, start, end)
        res = self.call_service("calendar/get_events",
                                entity_id=self.calendar,
                                start_date_time=start, end_date_time=end,
                                hass_timeout=30)
        events = find_events(res, self.calendar)
        if events is None:
            raise RuntimeError("no events in calendar.get_events result")
        return events

    def _poll(self, kwargs=None):
        try:
            events = self._fetch_events()
        except Exception as exc:  # noqa: BLE001
            self.engine.calendar_failed(str(exc))
            events = None
        else:
            self.engine.calendar_ok()
        try:
            self.engine.sync(events)
        except Exception as exc:  # noqa: BLE001
            self._say("sync failed: %s" % exc, "ERROR")
        self._publish()

    def _publish(self):
        for eid, (state, attrs) in self.engine.sensors().items():
            try:
                if self.ha:
                    self.ha.set_state(eid, state, attrs)
                else:
                    self.set_state(eid, state=state, attributes=attrs,
                                   replace=True)
            except Exception as exc:  # noqa: BLE001
                self._say("publishing %s failed: %s" % (eid, exc), "WARNING")

    # ------------------------------------------------------------ endpoint
    def _endpoint(self, data, kwargs):
        req = kwargs.get("request")
        key = req.headers.get("X-API-Key", "") if req is not None else ""
        if not hmac.compare_digest(str(key), self.api_key):
            return {"error": "unauthorized"}, 401
        data = dict(data or {})
        cmd = data.get("cmd", "status")
        try:
            return self._command(cmd, data), 200
        except ValueError as exc:
            return {"error": str(exc)}, 400
        except Exception as exc:  # noqa: BLE001
            self._say("endpoint %s failed: %s" % (cmd, exc), "ERROR")
            return {"error": "internal error"}, 500

    def _command(self, cmd, data):
        e = self.engine
        if cmd == "status":
            return e.status(show_pins=bool(data.get("show_pins")))
        if cmd == "resync":
            if data.get("force"):
                e.force_resync()
            self._poll()
            return e.status()
        if cmd == "regenerate_pin":
            out = e.regenerate_pin(self._sid(data))
        elif cmd == "set_pin":
            if "pin" not in data:
                raise ValueError("pin is required (empty clears it)")
            out = e.set_pin(self._sid(data), data.get("pin"))
        elif cmd == "doors":
            if not self.access:
                raise ValueError("UniFi Access is not configured")
            return {"doors": self.access.list_doors(),
                    "door_groups": self.access.list_door_groups()}
        else:
            raise ValueError("unknown cmd %r" % cmd)
        self._publish()
        return out

    @staticmethod
    def _sid(data):
        sid = data.get("stay_id")
        if not sid:
            raise ValueError("stay_id is required")
        return str(sid)

    # -------------------------------------------------------------- notify
    def _push(self, title, message):
        self._say("NOTIFY %s: %s" % (title, message), "WARNING")
        if self.notify_service:
            try:
                self.call_service(str(self.notify_service).replace(".", "/"),
                                  title=title, message=message)
            except Exception as exc:  # noqa: BLE001
                self._say("notify via %s failed: %s" % (
                    self.notify_service, exc), "ERROR")
        for r in self.remote_notify:
            svc = str(r["service"]).replace("notify.", "").replace(
                "notify/", "")
            try:
                resp = requests.post(
                    "%s/api/services/notify/%s" % (
                        str(r["url"]).rstrip("/"), svc),
                    headers={"Authorization": "Bearer %s" % r["token"]},
                    json={"title": title, "message": message}, timeout=10)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                self._say("remote push via %s failed: %s" % (r["url"], exc),
                          "ERROR")
