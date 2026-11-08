# appdaemon-bdl-stays

AppDaemon app for the Sun Peaks lodge ("BDL") that turns guest stays on a
Home Assistant calendar into door PINs, heat-mode requests and HA sensors.

- **Source of truth**: HA Local Calendar `calendar.bdl_stays`. One event per
  stay; the app polls it every 5 min (and on `resync`).
- **Door PINs**: one UniFi Access *visitor* per stay, with a PIN that works
  from arrival − 2 h to checkout + 1 h, then is deleted.
- **Heat**: asks the sibling app [bdl_modes](../appdaemon-bdl-modes) to
  `preheat` before arrival and go to `vacation` after checkout (off by
  default: `modes_enabled: false`).
- **Sensors**: `sensor.bdl_stay_current` and `sensor.bdl_stay_next` for the
  BDL dashboard and the BlackDiamondHub kiosk.

Files: `bdl_stays.py` (thin AppDaemon wrapper), `bs_engine.py` (all logic,
pure Python, no AppDaemon imports), `bs_access.py` (UniFi Access client),
`bdl_stays.yaml` (config). State lives in `bdl_stays.db` (SQLite) next to the
app, so restarts are idempotent.

## Calendar conventions

| Field | Meaning |
|---|---|
| Summary | Guest name, e.g. `Henderson family` (becomes the Access visitor name) |
| Start / end | Arrival / checkout. **All-day** events arrive at `arrival_time` (16:00) on the first day and leave at `checkout_time` (11:00) on the last day shown in the calendar. A single-day all-day event is one night. |
| Description | Optional line `pin: 4821` (also `PIN=1234`, `Door PIN: 12-34`): the guest's own PIN, 4–8 digits. Every other line is a note. |

Times are America/Vancouver (the app uses the offsets HA returns; no fixed
UTC offset is assumed). The stay id is the calendar event `uid` (read via
the HA REST calendar API) or, without REST access, a hash of summary +
start. Moving an event keeps the same uid, so the visitor is updated rather
than recreated.

A stay disappears only after it has been missing from **two** successful
polls (`removal_confirmations`); a failed calendar read never deletes
anything.

## Door PINs (UniFi Access)

For every stay whose arrival is within `create_days_before` (14 days) the app
ensures one Access visitor:

- first name = guest, last name = `Stay <Mon D>`, visit reason `Others`,
  remarks `bdl_stays:<stay id> (managed by AppDaemon)` (used to re-adopt a
  visitor after a crash instead of creating a duplicate);
- valid from arrival − `early_access_h` to checkout + `late_access_h`;
- resources = `door_ids` + `door_group_ids` + `door_names` (names are
  resolved through `GET /doors`; cmd `doors` lists what exists);
- PIN, by precedence: endpoint override (`set_pin` / `regenerate_pin`) >
  calendar `pin:` > an auto PIN generated once and persisted. Auto PINs are
  `pin_length` (4) random digits, never trivial (`1234`, `1111`,
  `1212`, …), never in `pin_blocklist`, never equal to another stay's PIN.
  A malformed or blocklisted calendar PIN falls back to an auto PIN and
  sends a `bad_pin` notification. Changing the calendar PIN drops any
  override.

Event changes (name, times, PIN, doors) update the visitor; if the visitor
was deleted in the Access UI it is recreated. The visitor is deleted when the
event is removed or at checkout + `late_access_h` + `cleanup_delay_h`.
Records are kept `retention_days` (30) for status, then purged.

PINs are never logged at info level (masked as `******`) and never placed in
exception text; they *are* stored in `bdl_stays.db` and in the sensor
attributes (owners read them in HA).

Without `access_token` (or with no doors configured) the app runs in
**no-access** mode: everything else works, PINs are still generated and
published, `access_status` is `disabled`.

API used — official *UniFi Access API* reference
(<https://core-config-gfoz.uid.alpha.ui.com/configs/unifi-access/api_reference.pdf>,
also downloadable from Access › Settings › General › Advanced › API Token),
base `https://<console>:12445/api/v1/developer`, `Authorization: Bearer <token>`:

| § | Call | Purpose | Permission |
|---|---|---|---|
| 4.2 | `POST /visitors` | create visitor | edit:visitor |
| 4.4 | `GET /visitors?keyword=` | find our visitor by name + remarks marker | view:visitor |
| 4.5 | `PUT /visitors/:id` | update name / times / doors | edit:visitor |
| 4.6 | `DELETE /visitors/:id?is_force=true` | delete visitor | edit:visitor |
| 4.9 | `PUT /visitors/:id/pin_codes` `{"pin_code": "…"}` | assign PIN | edit:visitor |
| 7.8 | `GET /doors` | resolve `door_names`, cmd `doors` | view:space |
| 7.5 | `GET /door_groups` | cmd `doors` | view:space |

## Heat (bdl_modes)

Calls go to the bdl_modes endpoint (`bdl_modes_url`, `X-API-Key:
bdl_modes_api_key`) and only when `modes_enabled: true`; otherwise the app
logs once per decision `modes disabled: would …`.

- **Preheat**: once the next arrival is within `preheat_horizon_h` (48 h),
  send `preheat` with the arrival (request_id
  `bdl_stays:preheat:<stay>:<arrival>`, so repeats are harmless). If
  bdl_modes already runs a preheat for a different arrival, send
  `update_arrival` instead. An arrival that moves later keeps being tracked.
  Not sent while a stay is in progress, while the previous stay's vacation is
  still pending, or within 60 min after our own vacation request.
- **Vacation**: at checkout + `vacation_delay_h` (1 h) send `vacation`,
  unless another stay arrives within `back_to_back_h` (24 h) of checkout.
  Skipped if more than 12 h late (e.g. AppDaemon was down).
- Removing a stay after its preheat was sent only notifies
  (`preheat_orphan`); cancel/vacation is left to a human.

## Sensors

Both sensors are republished every poll and after every change. State =
guest name or `none`. **Attribute names are a stable contract** (the
BlackDiamondHub kiosk reads `sensor.bdl_stay_current` to switch to guest mode
and show a welcome card):

| Attribute | Example | Notes |
|---|---|---|
| `stay_id` | `0c2f…@google.com` | |
| `guest` | `Henderson family` | |
| `arrival` / `checkout` | `2026-12-20T16:00:00-07:00` | ISO 8601 with offset |
| `all_day` | `true` | |
| `pin` | `482913` | |
| `pin_source` | `manual` / `auto` | |
| `access_status` | `scheduled` / `active` / `expired` / `removed` / `error` / `disabled` | |
| `access_start` / `access_end` | ISO 8601 | PIN validity window |
| `notes` | `Two dogs` | description minus the PIN line |
| `last_error` | `null` | |
| `access_mode` | `enabled` / `disabled` | app-wide |
| `modes_enabled` | `false` | app-wide |

With no stay all stay attributes are `null`. `sensor.bdl_stay_current` is
the stay with arrival ≤ now < checkout; `sensor.bdl_stay_next` the first
stay arriving after now.

## Endpoint

`POST http://<appdaemon>:5050/api/appdaemon/bdl_stays`, header
`X-API-Key: <bdl_stays_api_key>`, JSON body:

| cmd | Args | Does |
|---|---|---|
| `status` | `show_pins` (bool, default false) | all stays, access/mode state, errors |
| `resync` | `force` (bool) | poll now; `force` re-pushes every visitor (fixes edits made in the Access UI) |
| `regenerate_pin` | `stay_id` | new random PIN (overrides a calendar PIN until the calendar PIN changes) |
| `set_pin` | `stay_id`, `pin` | manual PIN override (4–8 digits); `""` clears the override |
| `doors` | – | list Access doors and door groups (ids for config) |

```bash
curl -s -X POST http://appdaemon.bdl:5050/api/appdaemon/bdl_stays \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"cmd": "set_pin", "stay_id": "<id>", "pin": "2468"}'
```

## Notifications

Failures only, one per stay per kind until it recovers: `access` (create /
update failed), `access_delete`, `pin_conflict` (manual PIN already used in
Access), `bad_pin`, `preheat` / `update_arrival` / `vacation` (bdl_modes
call failed), `preheat_orphan`, plus one global "calendar unreadable". Sent
to `notify_service` (local HA) and every `remote_notify` target, like
bdl_modes.

## Install

1. Copy `bdl_stays.py`, `bs_engine.py`, `bs_access.py` and `bdl_stays.yaml`
   to `conf/apps/bdl_stays/` on the AppDaemon host.
2. Add these to `conf/secrets.yaml`:
   - `bdl_stays_api_key` – any long random string (callers of this endpoint).
   - `bdl_stays_access_token` – UniFi Access API token (step 5).
   - `bdl_modes_api_key` – the key bdl_modes already uses (its `api_key`).
   - Already present from bdl_modes: `bdl_modes_ha_token`,
     `mi_ha_notify_token`.
3. Merge `install/logs.yaml` into the `logs:` section of `appdaemon.yaml`.
4. Home Assistant: Settings › Devices & services › Add integration ›
   **Local Calendar**, name it **BDL Stays** (→ `calendar.bdl_stays`).
   Add `install/dashboard_tab.yaml` as a view of the BDL dashboard.
5. UniFi Access (≥ 1.9.1): Settings › General › Advanced › API Token ›
   create a token with **Visitor** (view + edit) and **Space** (view)
   permissions. Check the port (`12445`) in `access_host`.
6. Pick the guest doors: keep `door_names: [Front Door, Ski Room]` (the two doors with PIN pads) or call cmd
   `doors` and put ids into `door_ids` / `door_group_ids`. Set
   `pin_blocklist` to owner/staff codes and `pin_length` to Access's
   configured PIN length.
7. Restart the app, check `status` and the log. Set `modes_enabled: true`
   once the `modes disabled: would …` lines look right.

## Tests

```
python -m pytest tests        # or: python -m unittest discover -s tests
```

`tests/test_engine.py` drives the engine with a fake clock, fake Access and
fake bdl_modes; `tests/test_clients.py` covers the HTTP clients with a fake
session.
