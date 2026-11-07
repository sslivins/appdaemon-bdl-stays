"""UniFi Access developer API client for bdl_stays (no AppDaemon imports).

Source: the official "UniFi Access API" reference (PDF, downloaded from
Access > Settings > General > Advanced > API Token, also published at
https://core-config-gfoz.uid.alpha.ui.com/configs/unifi-access/api_reference.pdf).
Base URL ``https://<console>:12445/api/v1/developer`` (self-signed cert),
header ``Authorization: Bearer <token>``. Every response is wrapped as
``{"code": "SUCCESS", "msg": ..., "data": ...}``. Endpoints used:

* 4.2  POST   /visitors                  create visitor        (edit:visitor)
* 4.4  GET    /visitors?keyword=         search visitors       (view:visitor)
* 4.5  PUT    /visitors/:id              update visitor        (edit:visitor)
* 4.6  DELETE /visitors/:id?is_force=true delete visitor       (edit:visitor)
* 4.9  PUT    /visitors/:id/pin_codes    assign PIN            (edit:visitor)
* 7.8  GET    /doors                     list doors            (view:space)
* 7.5  GET    /door_groups               list door groups      (view:space)

PINs are generated locally (the API cannot read a PIN back once set) and are
never put in exception messages or logs.
"""

import re

API_PREFIX = "/api/v1/developer"
NOT_FOUND_CODES = ("CODE_NOT_EXISTS", "CODE_RESOURCE_NOT_FOUND",
                   "CODE_USER_WORKER_NOT_EXISTS")
PIN_EXISTS_CODE = "CODE_CREDS_PIN_CODE_CREDS_ALREADY_EXIST"
PIN_LENGTH_CODE = "CODE_CREDS_PIN_CODE_CREDS_LENGTH_INVALID"


class AccessError(Exception):
    """An Access API call failed. ``code`` is the API's error code (or
    ``HTTP_<n>`` / ``NETWORK``); the message never contains a PIN."""

    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code = code
        self.status = status

    @property
    def not_found(self):
        return self.code in NOT_FOUND_CODES or self.status == 404

    @property
    def pin_taken(self):
        return self.code == PIN_EXISTS_CODE


class AccessClient:
    """Interface used by the engine (tests provide a fake)."""

    def create_visitor(self, first_name, last_name, start, end, resources,
                       remarks):
        """-> visitor id. start/end are epoch seconds; resources is a list
        of ``{"id": ..., "type": "door"|"door_group"}``."""
        raise NotImplementedError

    def update_visitor(self, visitor_id, first_name, last_name, start, end,
                       resources, remarks):
        raise NotImplementedError

    def delete_visitor(self, visitor_id):
        """Deleting a visitor that no longer exists is not an error."""
        raise NotImplementedError

    def set_visitor_pin(self, visitor_id, pin):
        raise NotImplementedError

    def find_visitor(self, keyword, marker):
        """-> id of an existing visitor whose remarks contain ``marker``
        (used to adopt a visitor created just before a crash), or None."""
        raise NotImplementedError

    def resources(self):
        """-> the resource list every stay visitor gets."""
        raise NotImplementedError


def _scrub(text, secrets):
    text = str(text or "")
    for s in secrets:
        if s:
            text = text.replace(str(s), "******")
    # Belt and braces: never echo a long run of digits (could be a PIN).
    return re.sub(r"\b\d{4,8}\b", "######", text)[:300]


class UnifiAccessClient(AccessClient):
    def __init__(self, host, token, door_ids=None, door_group_ids=None,
                 door_names=None, timeout=15, verify=False, session=None):
        import requests
        if not verify:
            try:
                import urllib3
                urllib3.disable_warnings(
                    urllib3.exceptions.InsecureRequestWarning)
            except Exception:  # noqa: BLE001
                pass
        self.base = host.rstrip("/") + API_PREFIX
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": "Bearer %s" % token,
                               "Accept": "application/json",
                               "Content-Type": "application/json"})
        self.timeout = timeout
        self.verify = verify
        self.door_ids = list(door_ids or [])
        self.door_group_ids = list(door_group_ids or [])
        self.door_names = list(door_names or [])
        self._resources = None
        self._requests = requests

    # ---------------------------------------------------------------- core
    def _call(self, method, path, body=None, params=None, secrets=()):
        try:
            r = self.s.request(method, self.base + path, json=body,
                               params=params, timeout=self.timeout,
                               verify=self.verify)
        except self._requests.RequestException as exc:
            raise AccessError("%s %s: %s" % (
                method, path, _scrub(type(exc).__name__ + " " + str(exc),
                                     secrets)), code="NETWORK") from None
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        code = payload.get("code") if isinstance(payload, dict) else None
        if r.status_code == 200 and code in (None, "SUCCESS"):
            return payload.get("data") if isinstance(payload, dict) else None
        msg = (payload.get("msg") if isinstance(payload, dict) else None) \
            or r.text
        raise AccessError("%s %s: HTTP %s %s %s" % (
            method, path, r.status_code, code or "", _scrub(msg, secrets)),
            code=code or "HTTP_%s" % r.status_code, status=r.status_code)

    # ------------------------------------------------------------ visitors
    @staticmethod
    def _visitor_body(first_name, last_name, start, end, resources,
                      remarks):
        return {"first_name": first_name, "last_name": last_name,
                "remarks": remarks, "start_time": int(start),
                "end_time": int(end), "visit_reason": "Others",
                "resources": resources}

    def create_visitor(self, first_name, last_name, start, end, resources,
                       remarks):
        data = self._call("POST", "/visitors", self._visitor_body(
            first_name, last_name, start, end, resources, remarks))
        vid = (data or {}).get("id")
        if not vid:
            raise AccessError("POST /visitors: no visitor id in response",
                              code="BAD_RESPONSE")
        return vid

    def update_visitor(self, visitor_id, first_name, last_name, start, end,
                       resources, remarks):
        self._call("PUT", "/visitors/%s" % visitor_id, self._visitor_body(
            first_name, last_name, start, end, resources, remarks))

    def delete_visitor(self, visitor_id):
        try:
            self._call("DELETE", "/visitors/%s" % visitor_id,
                       params={"is_force": "true"})
        except AccessError as exc:
            if not exc.not_found:
                raise

    def set_visitor_pin(self, visitor_id, pin):
        self._call("PUT", "/visitors/%s/pin_codes" % visitor_id,
                   {"pin_code": str(pin)}, secrets=(pin,))

    def find_visitor(self, keyword, marker):
        data = self._call("GET", "/visitors", params={
            "keyword": keyword, "page_num": 1, "page_size": 100})
        for v in data or []:
            if marker in (v.get("remarks") or ""):
                return v.get("id")
        return None

    # ----------------------------------------------------------- resources
    def resources(self):
        if self._resources is None:
            res = [{"id": d, "type": "door"} for d in self.door_ids]
            res += [{"id": g, "type": "door_group"}
                    for g in self.door_group_ids]
            if self.door_names:
                doors = self._call("GET", "/doors") or []
                by_name = {(d.get("name") or "").strip().lower(): d["id"]
                           for d in doors if d.get("id")}
                missing = [n for n in self.door_names
                           if n.strip().lower() not in by_name]
                if missing:
                    raise AccessError("unknown door name(s): %s" % ", ".join(
                        missing), code="CONFIG")
                for n in self.door_names:
                    did = by_name[n.strip().lower()]
                    if did not in self.door_ids:
                        res.append({"id": did, "type": "door"})
            if not res:
                raise AccessError("no doors configured (door_ids, "
                                  "door_group_ids or door_names)",
                                  code="CONFIG")
            self._resources = res
        return list(self._resources)

    def list_doors(self):
        return [{"id": d.get("id"), "name": d.get("name"),
                 "full_name": d.get("full_name")}
                for d in (self._call("GET", "/doors") or [])]

    def list_door_groups(self):
        return [{"id": g.get("id"), "name": g.get("name"),
                 "type": g.get("type")}
                for g in (self._call("GET", "/door_groups") or [])]
