import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bdl_stays  # noqa: E402
import bs_access  # noqa: E402


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
        self.text = str(payload)

    def json(self):
        return self._p


class FakeSession:
    def __init__(self, replies):
        self.headers = {}
        self.replies = list(replies)
        self.sent = []

    def request(self, method, url, json=None, params=None, timeout=None,
                verify=None):
        self.sent.append((method, url, json, params))
        return self.replies.pop(0)


def client(replies, **kw):
    s = FakeSession(replies)
    return bs_access.UnifiAccessClient("https://ua:12445/", "tok",
                                       session=s, **kw), s


class AccessClientTests(unittest.TestCase):
    def test_create_visitor(self):
        c, s = client([Resp(200, {"code": "SUCCESS", "data": {"id": "v9"}})])
        vid = c.create_visitor("Henderson family", "Stay Dec 20", 100.0,
                               200.9, [{"id": "d", "type": "door"}],
                               "bdl_stays:u1 (x)")
        self.assertEqual(vid, "v9")
        method, url, body, _ = s.sent[0]
        self.assertEqual((method, url), (
            "POST", "https://ua:12445/api/v1/developer/visitors"))
        self.assertEqual(body["visit_reason"], "Others")
        self.assertEqual((body["start_time"], body["end_time"]), (100, 200))
        self.assertEqual(s.headers["Authorization"], "Bearer tok")

    def test_pin_errors_never_contain_the_pin(self):
        c, s = client([Resp(400, {
            "code": bs_access.PIN_EXISTS_CODE,
            "msg": "PIN 482913 already exists"})])
        with self.assertRaises(bs_access.AccessError) as cm:
            c.set_visitor_pin("v1", "482913")
        self.assertTrue(cm.exception.pin_taken)
        self.assertNotIn("482913", str(cm.exception))
        self.assertEqual(s.sent[0][:3], (
            "PUT", "https://ua:12445/api/v1/developer/visitors/v1/pin_codes",
            {"pin_code": "482913"}))

    def test_error_code_with_http_200(self):
        c, _ = client([Resp(200, {"code": "CODE_PARAMS_INVALID",
                                  "msg": "Invalid parameters."})])
        with self.assertRaises(bs_access.AccessError) as cm:
            c.update_visitor("v1", "a", "b", 1, 2, [], "r")
        self.assertEqual(cm.exception.code, "CODE_PARAMS_INVALID")

    def test_delete_missing_visitor_is_ok(self):
        c, s = client([Resp(404, {"code": "CODE_NOT_EXISTS", "msg": "x"})])
        c.delete_visitor("v1")
        self.assertEqual(s.sent[0][3], {"is_force": "true"})

    def test_resources_by_name(self):
        doors = {"code": "SUCCESS", "data": [
            {"id": "d1", "name": "Front Door"},
            {"id": "d2", "name": "Ski Room"}]}
        c, _ = client([Resp(200, doors)], door_names=["front door"],
                      door_group_ids=["g1"])
        self.assertEqual(c.resources(), [{"id": "g1", "type": "door_group"},
                                         {"id": "d1", "type": "door"}])
        c, _ = client([Resp(200, doors)], door_names=["Garage"])
        with self.assertRaises(bs_access.AccessError):
            c.resources()

    def test_find_visitor_by_marker(self):
        c, s = client([Resp(200, {"code": "SUCCESS", "data": [
            {"id": "a", "remarks": "other"},
            {"id": "b", "remarks": "bdl_stays:u1 (managed by AppDaemon)"}]})])
        self.assertEqual(c.find_visitor("Smith", "bdl_stays:u1 "), "b")
        self.assertEqual(s.sent[0][3]["keyword"], "Smith")


class FindEventsTests(unittest.TestCase):
    def test_appdaemon_wrapped_result(self):
        res = {"success": True, "result": {"response": {
            "calendar.bdl_stays": {"events": [{"summary": "G"}]}}}}
        self.assertEqual(bdl_stays.find_events(res, "calendar.bdl_stays"),
                         [{"summary": "G"}])

    def test_failure(self):
        with self.assertRaises(RuntimeError):
            bdl_stays.find_events({"success": False, "error": "x"},
                                  "calendar.bdl_stays")


if __name__ == "__main__":
    unittest.main()
