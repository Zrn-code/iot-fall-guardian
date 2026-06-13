"""Automated ThingsBoard provisioning for the Wearable Guardian v3.1.

Two clear roles instead of the old confusing 'Guardian_Device_001 + workers':
  - 配戴者 Wearer  (device type=wearer): the protected person who wears the watch.
                   HR / IMU situation / escalation / GPS live on this device, and
                   the rule chain raises alarms here directly.
  - 守護者 Guardian (TB CUSTOMER_USER): the person who monitors from the phone.
                   No telemetry of their own — a viewer that logs in and sees all
                   wearers + alarms (via the owning customer).

Creates / updates (idempotent):
  - Devices:       Wearer_<id>  (type=wearer)  + tokens -> data/worker_tokens.json
  - Customer:      Guardian Ops (owns the wearers)
  - Guardian users: CUSTOMER_USER logins for the phone control-centre app
  - Rule chain:    GuardianRules (root; backs up old root first;
                   situation/escalation telemetry -> Collapse/Fall alarms,
                   propagated to the owning customer so guardians see them)
  - Dashboard:     Guardian Live Ops (wearer map + status + latest event + alarms)
  - Deletes the legacy single Guardian_Device_001 if present.

Run:
    python scripts/provision_thingsboard.py
        --base http://localhost:18080
        --user tenant@thingsboard.org
        --password tenant
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------- HTTP helpers ----------

TIMEOUT = 15.0


def _request(method: str, url: str, *, headers: dict[str, str], body: Any = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json", **headers}, method=method)
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # Some endpoints (e.g. /api/user/{id}/activationLink) return
                # a plain-text body (a URL), not JSON.
                return raw
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {url} → HTTP {e.code}: {body}") from e
    except URLError as e:
        raise SystemExit(f"{method} {url} → network error: {e}") from e


class TbClient:
    def __init__(self, base: str, user: str, password: str) -> None:
        self.base = base.rstrip("/")
        self.jwt = self._login(user, password)
        self.h = {"X-Authorization": f"Bearer {self.jwt}"}

    def _login(self, user: str, password: str) -> str:
        url = f"{self.base}/api/auth/login"
        r = _request("POST", url, headers={}, body={"username": user, "password": password})
        return r["token"]

    def get(self, path: str) -> Any:
        return _request("GET", f"{self.base}{path}", headers=self.h)

    def post(self, path: str, body: Any) -> Any:
        return _request("POST", f"{self.base}{path}", headers=self.h, body=body)

    def put(self, path: str, body: Any) -> Any:
        return _request("PUT", f"{self.base}{path}", headers=self.h, body=body)

    def delete(self, path: str) -> Any:
        return _request("DELETE", f"{self.base}{path}", headers=self.h)


# ---------- Device + token ----------


def ensure_device(client: TbClient, name: str, *, type_: str = "default", label: str | None = None) -> tuple[str, str]:
    """Return (device_id, access_token). Creates the device when missing."""
    try:
        dev = client.get(f"/api/tenant/devices?deviceName={name}")
    except SystemExit as exc:
        if "404" not in str(exc):
            raise
        body = {"name": name, "type": type_}
        if label is not None:
            body["label"] = label
        dev = client.post("/api/device", body)
        print(f"  created device {name} (type={type_})")
    else:
        print(f"  found device {name}")
        if dev.get("type") != type_:
            dev["type"] = type_
            if label is not None:
                dev["label"] = label
            dev = client.post("/api/device", dev)
            print(f"  updated device {name} type -> {type_}")
    dev_id = dev["id"]["id"]
    creds = client.get(f"/api/device/{dev_id}/credentials")
    return dev_id, creds["credentialsId"]


def ensure_wearer_devices(
    client: TbClient, wearer_ids: list[str], tokens_path: Path
) -> tuple[dict[str, str], dict[str, str]]:
    """Idempotently create Wearer_<id> devices (type=wearer) and persist tokens.

    A *wearer* is the protected person who wears the watch: their device carries
    HR / IMU situation / escalation / GPS, and the rule chain raises alarms on
    it directly (no separate 'guardian device').

    Returns (id_to_token, id_to_device_id).
    """
    # The token file reflects exactly the requested wearers (extras pruned), so a
    # single-wearer run cleans up tokens from any previous multi-wearer run.
    tokens: dict[str, str] = {}
    device_ids: dict[str, str] = {}
    for wid in wearer_ids:
        name = f"Wearer_{wid}"
        dev_id, tok = ensure_device(client, name, type_="wearer", label=wid)
        tokens[wid] = tok
        device_ids[wid] = dev_id
    tokens_path.parent.mkdir(parents=True, exist_ok=True)
    tokens_path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    print(f"  wrote {len(tokens)} wearer tokens to {tokens_path}")
    return tokens, device_ids


def ensure_guardian_devices(
    client: TbClient, guardian_emails: list[str], tokens_path: Path
) -> tuple[dict[str, str], dict[str, str]]:
    """Create one Guardian_<name> device (type=guardian) per guardian login.

    A *guardian* is a CUSTOMER_USER who monitors from the phone. The user account
    can't hold telemetry, so each guardian also gets a device that carries *their
    own* GPS — making the guardian's position a first-class TB entity (map + the
    'nearby' rule logic both read it). Keyed by the guardian's local-part
    (e.g. 'guardian1').

    Returns (name_to_token, name_to_device_id) keyed by local-part.
    """
    tokens: dict[str, str] = {}
    if tokens_path.exists():
        try:
            tokens = json.loads(tokens_path.read_text(encoding="utf-8"))
        except Exception:
            tokens = {}
    device_ids: dict[str, str] = {}
    for email in guardian_emails:
        key = email.split("@")[0]
        name = f"Guardian_{key}"
        dev_id, tok = ensure_device(client, name, type_="guardian", label=key)
        tokens[key] = tok
        device_ids[key] = dev_id
    tokens_path.parent.mkdir(parents=True, exist_ok=True)
    tokens_path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    print(f"  wrote {len(tokens)} guardian tokens to {tokens_path}")
    return tokens, device_ids


def ensure_relation(client: TbClient, from_id: str, from_type: str,
                    to_id: str, to_type: str, relation_type: str = "Manages") -> None:
    """Idempotently create a generic EntityRelation from -> to."""
    body = {
        "from": {"id": from_id, "entityType": from_type},
        "to": {"id": to_id, "entityType": to_type},
        "type": relation_type,
        "typeGroup": "COMMON",
    }
    client.post("/api/relation", body)


# Static resident metadata + demo aggregate stats. These are NOT sensor data, so
# they live as device attributes (read by the profile card + today-stats strip).
# A daily job / rule chain can later refresh the stat_* values from alarm history.
# Demo persona (the user chose to keep a clearly-labelled sample resident). These
# are care-record fields a real deployment sets per resident; the profile card
# tags 年齡/慢性病/緊急聯絡 as "示範資料 Demo" so they're never mistaken for real.
RESIDENT_PROFILE = {
    "resident_name": "王伯伯",
    "resident_age": 74,
    "resident_gender": "男",
    "resident_chronic": "高血壓",
    "emergency_name": "王小明",
    "emergency_relation": "兒子",
    "emergency_phone": "0912-345-678",
}


def compute_today_stats(client: TbClient, device_id: str) -> dict:
    """Compute the KPI strip from REAL data (not hardcoded):
      - falls          = # fall/collapse alarms in the last 24h
      - avg_recovery_s = mean (clearTs − startTs) of cleared fall alarms — now
                         time-to-resolution, since alarms only clear when a
                         guardian resolves them (no auto-clear on recovery)
      - self_recovery  = % of fall alarms that have been resolved (cleared)
      - active_hours   = non-fall time from the seeded situation_code trace (12h)
    """
    now = int(time.time() * 1000)
    since = now - 24 * 3600 * 1000
    alarms = client.get(
        f"/api/alarm/DEVICE/{device_id}?pageSize=500&page=0"
        f"&startTime={since}&endTime={now}&sortProperty=createdTime&sortOrder=DESC"
    ).get("data", [])
    fall_types = {"FallSuspected", "MedicalCollapse"}
    falls = [a for a in alarms if a.get("type") in fall_types]
    durs, cleared = [], 0
    for a in falls:
        if a.get("cleared") or str(a.get("status", "")).startswith("CLEARED"):
            cleared += 1
            st, ct = a.get("startTs") or a.get("createdTime"), a.get("clearTs")
            if st and ct and ct > st:
                durs.append((ct - st) / 1000.0)
    avg_rec = round(sum(durs) / len(durs)) if durs else 0
    self_rate = round(cleared / len(falls) * 100) if falls else 100
    # non-fall (code != 1) time from the seeded 30-min situation trace
    active_intervals = sum(1 for i in range(len(_SIT_TRACE) - 1) if _SIT_TRACE[i] != 1)
    active_hours = round(active_intervals * 0.5, 1)
    return {
        "stat_falls_today": len(falls),
        "stat_avg_recovery_s": avg_rec,
        "stat_self_recovery_pct": self_rate,
        "stat_active_hours": active_hours,
    }


# A 12-hour demo trace (every 30 min) so the HR-vs-baseline + activity-timeline
# charts render a full curve instead of a flat line with one recent spike. bpm
# wanders near the 67 baseline, with one fall spike (→121, situation_code 1)
# mirroring the mockup's afternoon event. situation is binary now: 0=NORMAL,
# 1=FALL (the old sit/lie stretches fold into NORMAL).
_BPM_TRACE = [68, 70, 69, 71, 70, 72, 69, 68, 70, 71, 70, 69,
              72, 70, 71, 73, 72, 70, 74, 72, 121, 96, 82, 74, 72]
_SIT_TRACE = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
              0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0]


def seed_wearer_profile(client: TbClient, device_id: str) -> None:
    """Seed resident metadata + demo stats (attributes), a 12h telemetry trace
    (so the trend/timeline charts aren't empty) and a final NORMAL point on
    campus, so the care-console renders fully — before any watch/demo traffic.
    Live telemetry simply continues from here."""
    client.post(f"/api/plugins/telemetry/DEVICE/{device_id}/SERVER_SCOPE", RESIDENT_PROFILE)

    now = int(time.time() * 1000)
    half_hour = 30 * 60 * 1000
    n = len(_BPM_TRACE)
    history = []
    for i in range(n):
        bpm = _BPM_TRACE[i]
        history.append({
            "ts": now - (n - i) * half_hour,
            "values": {
                "bpm": bpm, "hr_max": bpm, "hr_baseline": 67,
                "hr_above_baseline": max(0, bpm - 67),
                "situation_code": _SIT_TRACE[i],
                # battery is real telemetry from the watch; seed a gentle drain so
                # the demo shows a plausible *measured* level, not a fixed label.
                "battery": round(86 - i * 0.5),
            },
        })
    client.post(f"/api/plugins/telemetry/DEVICE/{device_id}/timeseries/ANY?scope=ANY", history)

    client.post(
        f"/api/plugins/telemetry/DEVICE/{device_id}/timeseries/ANY?scope=ANY",
        {
            "lat": 24.78686, "lon": 120.99681, "active": True, "entity_type": "wearer",
            "bpm": 78, "hr_max": 78, "hr_above_baseline": 0, "hr_baseline": 67,
            "situation_confidence": 0.95, "last_situation": "NORMAL",
            "last_escalation": "NONE", "situation_code": 0, "zone": "浩然圖書館",
            "battery": 74, "charging": False, "worn": True, "last_seen": now,
        },
    )

    # KPI strip = real numbers from the alarm history (computed, not hardcoded).
    client.post(f"/api/plugins/telemetry/DEVICE/{device_id}/SERVER_SCOPE",
                compute_today_stats(client, device_id))


# ---------- Customer + guardian (monitor) users ----------


def ensure_customer(client: TbClient, title: str) -> str:
    """Return the customer id, creating the customer when missing."""
    customers = client.get("/api/customers?pageSize=500&page=0").get("data", [])
    existing = next((c for c in customers if c.get("title") == title), None)
    if existing is not None:
        print(f"  found customer '{title}'")
        return existing["id"]["id"]
    created = client.post("/api/customer", {"title": title})
    print(f"  created customer '{title}'")
    return created["id"]["id"]


def _try_activate(client: TbClient, user_id: str, password: str) -> bool:
    """Set a user's password via the activation link. No-op if already active.

    `/api/user/{id}/activationLink` only returns a link while the user is still
    inactive; once activated it errors (SystemExit here). So a False result
    means 'already active' — which is fine for an idempotent re-run.
    """
    try:
        link = client.get(f"/api/user/{user_id}/activationLink")
    except SystemExit:
        return False
    token = str(link).split("activateToken=")[-1].strip()
    try:
        # /api/noauth/activate is unauthenticated; reuse the raw request helper.
        _request(
            "POST",
            f"{client.base}/api/noauth/activate",
            headers={},
            body={"activateToken": token, "password": password},
        )
        return True
    except SystemExit:
        return False


def ensure_customer_user(
    client: TbClient,
    customer_id: str,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
) -> bool:
    """Idempotently create a CUSTOMER_USER and ensure it has a usable password.

    Handles the partially-created case (user exists but was never activated, so
    it can't log in) by activating it on the spot. Returns True when a password
    was applied this run, False when the user was already active.
    """
    users = client.get(
        f"/api/customer/{customer_id}/users?pageSize=500&page=0"
    ).get("data", [])
    existing = next((u for u in users if u.get("email") == email), None)
    if existing is not None:
        user_id = existing["id"]["id"]
        if _try_activate(client, user_id, password):
            print(f"  guardian user '{email}' existed but was inactive -> activated (password set)")
            return True
        print(f"  guardian user '{email}' already active (password unchanged)")
        return False
    user = client.post(
        "/api/user?sendActivationMail=false",
        {
            "email": email,
            "authority": "CUSTOMER_USER",
            "firstName": first_name,
            "lastName": last_name,
            "customerId": {"entityType": "CUSTOMER", "id": customer_id},
        },
    )
    user_id = user["id"]["id"]
    _try_activate(client, user_id, password)
    print(f"  created guardian user '{email}' (password set)")
    return True


def assign_device_to_customer(client: TbClient, customer_id: str, device_id: str) -> None:
    client.post(f"/api/customer/{customer_id}/device/{device_id}", {})


def assign_dashboard_to_customer(client: TbClient, customer_id: str, dashboard_id: str) -> None:
    client.post(f"/api/customer/{customer_id}/dashboard/{dashboard_id}", {})


# ---------- Rule chain ----------

# escalation is now computed HERE in TB (ported from the server's
# decide_escalation). The watch/demo posts only situation + features; TB owns
# the decision. Thresholds mirror notify.py defaults.
JS_TRANSFORM = """\
// Compute escalation from the classified situation + features (was server-side).
// Also derive named zone / situation_code / hr_baseline for the care console,
// and zero hazard state out on recovery so nothing lingers.
var HR_CRASH_ABOVE = 40.0;   // hr above personal baseline
var MEDICAL_HR_MAX = 150.0;
var STILL_EPS = 0.05;        // post-impact variance: lower = more "collapsed"

// Named campus zones: map raw GPS -> nearest landmark within radius (no API).
// ~41 NYCU Guangfu-campus landmarks at OpenStreetMap-accurate coordinates.
var ZONES = [
  // 教學/研究館舍
  {n:'工程一館',     la:24.78871, lo:120.99798},
  {n:'工程三館',     la:24.78697, lo:120.99736},
  {n:'工程四館',     la:24.78697, lo:120.99678},
  {n:'工程五館',     la:24.78587, lo:120.99740},
  {n:'工程六館',     la:24.78597, lo:120.99600},
  {n:'科學一館',     la:24.78801, lo:120.99673},
  {n:'科學二館',     la:24.78903, lo:120.99656},
  {n:'綜合一館',     la:24.78466, lo:120.99731},
  {n:'管理一館',     la:24.78762, lo:121.00053},
  {n:'管理二館',     la:24.78533, lo:120.99850},
  {n:'人社一館',     la:24.78740, lo:120.99867},
  {n:'人社二館',     la:24.78706, lo:121.00002},
  {n:'電子資訊大樓', la:24.78659, lo:121.00178},
  {n:'田家炳光電大樓',la:24.78743, lo:120.99586},
  {n:'交映樓',       la:24.78676, lo:120.99594},
  {n:'計網中心',     la:24.78790, lo:120.99779},
  // 行政/公共
  {n:'浩然圖書館',   la:24.78645, lo:120.99842},
  {n:'行政大樓',     la:24.78779, lo:120.99913},
  {n:'中正堂',       la:24.78838, lo:120.99851},
  {n:'學生活動中心', la:24.78620, lo:121.00037},
  // 體育
  {n:'體育館',       la:24.78868, lo:120.99582},
  {n:'游泳池',       la:24.79000, lo:120.99587},
  {n:'綜合球館',     la:24.78982, lo:120.99509},
  {n:'棒壘球場',     la:24.78508, lo:120.99617},
  {n:'網球場',       la:24.78715, lo:120.99470},
  // 餐飲
  {n:'第一餐廳',     la:24.78670, lo:120.99968},
  {n:'第二餐廳',     la:24.78912, lo:120.99698},
  // 宿舍
  {n:'竹軒宿舍',     la:24.78953, lo:120.99818},
  {n:'研一舍',       la:24.78979, lo:120.99747},
  {n:'研三舍',       la:24.78374, lo:120.99708},
  {n:'七舍',         la:24.78573, lo:120.99941},
  {n:'八舍',         la:24.78528, lo:120.99967},
  {n:'九舍',         la:24.78985, lo:120.99672},
  {n:'十舍',         la:24.79000, lo:120.99673},
  {n:'十一舍',       la:24.79007, lo:120.99744},
  {n:'十二舍',       la:24.78423, lo:120.99564},
  {n:'十三舍',       la:24.78385, lo:120.99626},
  {n:'女二舍',       la:24.78456, lo:120.99964},
  {n:'百川書院',     la:24.78385, lo:120.99708},
  // 校門/地標
  {n:'正門(北大門)', la:24.78924, lo:120.99993},
  {n:'南大門',       la:24.78452, lo:120.99340}
];
var ZONE_R = 130.0;
function _hav(la1, lo1, la2, lo2) {
  var R = 6371000, dLa = (la2 - la1) * Math.PI / 180, dLo = (lo2 - lo1) * Math.PI / 180,
      a = Math.sin(dLa / 2) * Math.sin(dLa / 2) +
          Math.cos(la1 * Math.PI / 180) * Math.cos(la2 * Math.PI / 180) *
          Math.sin(dLo / 2) * Math.sin(dLo / 2);
  return 2 * R * Math.asin(Math.sqrt(a));
}
function _zoneOf(la, lo) {
  var best = null, bd = 1e12;
  for (var i = 0; i < ZONES.length; i++) {
    var d = _hav(la, lo, ZONES[i].la, ZONES[i].lo);
    if (d < bd) { bd = d; best = ZONES[i]; }
  }
  return (bd <= ZONE_R) ? best.n : ('校園周邊 ' + la.toFixed(4) + ', ' + lo.toFixed(4));
}
// situation -> numeric code so the "activity timeline" chart can plot it.
var SIT_CODE = {NORMAL: 0, FALL: 1};

// stamp every telemetry message with the server's receive time so the dashboard
// can show a real "last sync" (TB's lastActivityTime persists too lazily for this).
msg.last_seen = new Date().getTime();

if (typeof msg.situation !== 'undefined') {
  var sit = msg.situation;
  var hrMax = +msg.hr_max || 0;
  var aboveBase = +msg.hr_above_baseline || 0;
  var stillness = (typeof msg.post_impact_stillness !== 'undefined')
                  ? +msg.post_impact_stillness : 1.0;

  var esc = 'NONE';
  if (sit === 'FALL') {
    var crashed = (aboveBase > HR_CRASH_ABOVE) || (hrMax > MEDICAL_HR_MAX)
                  || (stillness < STILL_EPS);
    esc = crashed ? 'SOS_COLLAPSE' : 'ASK_OK';
  }
  msg.escalation = esc;
  msg.is_emergency = (esc === 'SOS_COLLAPSE');
  msg.risk_score = esc === 'SOS_COLLAPSE' ? 1.0 :
                   (esc === 'ASK_OK' ? 0.5 : 0.0);
  msg.hazard = esc !== 'NONE';
  msg.emergency = msg.is_emergency;

  // mirror to the dashboard "latest" fields
  msg.last_situation = sit;
  msg.last_escalation = esc;
  msg.last_hr_max = hrMax;
  msg.situation_code = (SIT_CODE[sit] != null) ? SIT_CODE[sit] : 0;

  // personal HR baseline (resting) for the "HR vs baseline" trend chart:
  // baseline = current mean HR - how far it is above baseline.
  if (typeof msg.hr_mean !== 'undefined' && typeof msg.hr_above_baseline !== 'undefined') {
    var b = (+msg.hr_mean) - (+msg.hr_above_baseline);
    if (!isNaN(b) && b > 0) { msg.hr_baseline = Math.round(b); }
  }

  // named zone from GPS -> shown in profile location, map tooltip, alarm details.
  if (typeof msg.lat !== 'undefined' && typeof msg.lon !== 'undefined') {
    msg.zone = _zoneOf(+msg.lat, +msg.lon);
  }

  // RECOVERY: situation back to NORMAL only refreshes the *live* state so the
  // map / wearer cards turn green again. It does NOT clear any alarm — an open
  // fall incident stays ACTIVE until a guardian resolves it in the console, so
  // it can never vanish before anyone sees it. `recovered` lets the console
  // badge a still-open alarm as "已自行恢復 (wearer has since stood up)".
  if (sit === 'NORMAL') {
    msg.recovered = true;
    msg.last_recovery_at = new Date().getTime();
  }
}
return {msg: msg, metadata: metadata, msgType: msgType};
"""


def _node(name: str, type_: str, x: int, y: int, config: dict) -> dict:
    return {
        "additionalInfo": {"description": "", "layoutX": x, "layoutY": y},
        "type": type_,
        "name": name,
        "debugSettings": {"failuresEnabled": False, "allEnabled": False},
        "configurationVersion": 0,
        "configuration": config,
    }


def build_rule_chain_metadata(
    chain_id: str,
    *,
    fcm_relay_url: str = "",
    fcm_relay_secret: str = "",
) -> dict:
    """Build the GuardianRules metadata — the orchestration brain.

    Topology (TB does escalation, alarms, delayed upgrade, ack):
        Input → MsgTypeSwitch
           ├ Post telemetry → JS Transform (computes escalation) → Save Timeseries
           │     ├→ Filter SOS_COLLAPSE → Alarm CRITICAL
           │     ├→ Filter ASK_OK       → Alarm WARNING → Delay 20s → Re-check
           │     │                                          → still not OK → Alarm CRITICAL
           │     └→ Filter ack_seen    → save rescue_state + Acknowledge alarm
           └ Post attributes → Save Client Attributes

    Alarm LIFECYCLE — alarms are NEVER auto-cleared by the rule chain. A fall
    raises an alarm that stays ACTIVE until a human (guardian console / TB
    operator) explicitly resolves it. When the wearer recovers (situation back
    to NORMAL) the JS transform only flips the *live* state green on the map /
    cards; the open incident remains in the alarm list so it can't silently
    vanish before anyone sees it. (Earlier versions cleared on the first NORMAL
    telemetry, which made alarms disappear within ~5 s of a fall.)

    Connections are wired by node NAME (see `idx` below), so adding/removing a
    node never requires renumbering integer indices.

    When `fcm_relay_secret` is set, every alarm node's *Created* output (never
    Updated — that's the natural de-dup) also POSTs the alarm to the local
    fcm_relay.py, which pushes an FCM notification so guardians get a system
    notification even with the phone app closed.
    """
    nodes = []

    # 0 — Save Timeseries (terminal)
    nodes.append(_node(
        "Save Timeseries",
        "org.thingsboard.rule.engine.telemetry.TbMsgTimeseriesNode",
        910, 80,
        {
            "defaultTTL": 0,
            "useServerTs": False,
            "processingSettings": {"type": "ON_EVERY_MESSAGE"},
        },
    ))

    # 1 — Save Client Attributes
    nodes.append(_node(
        "Save Client Attributes",
        "org.thingsboard.rule.engine.telemetry.TbMsgAttributesNode",
        910, 200,
        {
            "scope": "CLIENT_SCOPE",
            "notifyDevice": True,
            "sendAttributesUpdatedNotification": False,
            "updateAttributesOnlyOnValueChange": True,
            "processingSettings": {"type": "ON_EVERY_MESSAGE"},
        },
    ))

    # 2 — Message Type Switch (FIRST NODE)
    nodes.append(_node(
        "Message Type Switch",
        "org.thingsboard.rule.engine.filter.TbMsgTypeSwitchNode",
        290, 140,
        {"version": 0},
    ))

    # 3 — JS Transform (enrich telemetry)
    nodes.append(_node(
        "Enrich derived telemetry",
        "org.thingsboard.rule.engine.transform.TbTransformMsgNode",
        540, 80,
        {"jsScript": JS_TRANSFORM},
    ))

    # Filter: collapse (medical emergency)
    nodes.append(_node(
        "Filter: escalation==SOS_COLLAPSE",
        "org.thingsboard.rule.engine.filter.TbJsFilterNode",
        910, 320,
        {"jsScript": "return msg.escalation === 'SOS_COLLAPSE' || msg.emergency === true;"},
    ))

    # Filter: suspected fall awaiting response (escalation==ASK_OK)
    nodes.append(_node(
        "Filter: escalation==ASK_OK",
        "org.thingsboard.rule.engine.filter.TbJsFilterNode",
        910, 560,
        {"jsScript": "return msg.escalation === 'ASK_OK';"},
    ))

    # Create Alarm: MedicalCollapse
    nodes.append(_node(
        "Alarm: MedicalCollapse",
        "org.thingsboard.rule.engine.action.TbCreateAlarmNode",
        1200, 320,
        {
            "alarmType": "MedicalCollapse",
            "severity": "CRITICAL",
            "propagate": True,
            "propagateToOwner": True,
            "propagateToTenant": False,
            "useMessageAlarmData": False,
            "overwriteAlarmDetails": False,
            "dynamicSeverity": False,
            "alarmDetailsBuildJs": (
                "var details = {};\n"
                "details.situation = msg.situation;\n"
                "details.escalation = msg.escalation;\n"
                "details.hr_max = msg.hr_max;\n"
                "details.zone = msg.zone;\n"
                "details.worker_id = msg.worker_id;\n"
                "details.session_id = msg.session_id;\n"
                "return details;"
            ),
            "relationTypes": [],
        },
    ))

    # Create Alarm: FallSuspected
    nodes.append(_node(
        "Alarm: FallSuspected",
        "org.thingsboard.rule.engine.action.TbCreateAlarmNode",
        1200, 560,
        {
            "alarmType": "FallSuspected",
            "severity": "WARNING",
            "propagate": True,
            "propagateToOwner": True,
            "propagateToTenant": False,
            "useMessageAlarmData": False,
            "overwriteAlarmDetails": False,
            "dynamicSeverity": False,
            "alarmDetailsBuildJs": (
                "var details = {};\n"
                "details.situation = msg.situation;\n"
                "details.hr_max = msg.hr_max;\n"
                "details.zone = msg.zone;\n"
                "details.worker_id = msg.worker_id;\n"
                "return details;"
            ),
            "relationTypes": [],
        },
    ))

    # ----- B2: delayed time-based escalation (ASK_OK -> 20s -> still down -> COLLAPSE) -----

    # Delay 20s (keyed per originator)
    nodes.append(_node(
        "Delay 20s (fall response window)",
        "org.thingsboard.rule.engine.delay.TbMsgDelayNode",
        1500, 560,
        {
            "useMetadataPeriodInSecondsPatterns": False,
            "periodInSeconds": 20,
            "maxPendingMsgs": 1000,
            "periodInSecondsPattern": "",
        },
    ))

    # After the delay, re-check the wearer's CURRENT state via originator
    #      attributes (not the stale delayed msg). The "Save Client Attributes"
    #      branch persists last_situation; if the wearer recovered, last_situation
    #      is NORMAL and we must NOT upgrade. We read it with an originator-
    #      attributes node feeding metadata, then filter on it.
    nodes.append(_node(
        "Filter: still FALL after delay",
        "org.thingsboard.rule.engine.filter.TbJsFilterNode",
        1980, 560,
        {"jsScript": "return metadata.last_situation !== 'NORMAL';"},
    ))

    # Create Alarm: MedicalCollapse (auto-upgraded, no response)
    nodes.append(_node(
        "Alarm: MedicalCollapse (auto-upgrade)",
        "org.thingsboard.rule.engine.action.TbCreateAlarmNode",
        2100, 560,
        {
            "alarmType": "MedicalCollapse",
            "severity": "CRITICAL",
            "propagate": True,
            "propagateToOwner": True,
            "propagateToTenant": False,
            "useMessageAlarmData": False,
            "overwriteAlarmDetails": True,
            "dynamicSeverity": False,
            "alarmDetailsBuildJs": (
                "var details = {};\n"
                "details.situation = msg.situation;\n"
                "details.escalation = 'SOS_COLLAPSE';\n"
                "details.reason = 'no_response_20s';\n"
                "details.hr_max = msg.hr_max;\n"
                "details.zone = msg.zone;\n"
                "details.worker_id = msg.worker_id;\n"
                "return details;"
            ),
            "relationTypes": [],
        },
    ))

    # ----- B1 REMOVED: no auto-clear on recovery -----
    # Alarms are deliberately NOT cleared by the rule chain. A fall incident
    # stays ACTIVE until a guardian resolves it from the console (REST
    # /api/alarm/{id}/clear) or a TB operator clears it from the dashboard
    # alarms table (allowClear=True). Recovery (situation==NORMAL) only greens
    # the live map/cards via the JS transform — see `recovered` above. This
    # fixes alarms vanishing within seconds of a fall, before anyone saw them.

    # ----- B4: wearer acknowledged the guardian is coming (ack_seen) -----

    # Filter: ack_seen telemetry from the wearer
    nodes.append(_node(
        "Filter: ack_seen",
        "org.thingsboard.rule.engine.filter.TbJsFilterNode",
        910, 1000,
        {"jsScript": "return msg.ack_seen === true || msg.ack_seen === 'true';"},
    ))

    # Save rescue_state=guardian_enroute attribute
    nodes.append(_node(
        "Save rescue_state",
        "org.thingsboard.rule.engine.transform.TbTransformMsgNode",
        1200, 1000,
        {"jsScript": (
            "var out = {rescue_state: 'guardian_enroute', "
            "rescue_ack_at: new Date().getTime()};\n"
            "return {msg: out, metadata: metadata, msgType: 'POST_TELEMETRY_REQUEST'};"
        )},
    ))

    # After delay, load the wearer's latest situation attr into metadata so
    #      the upgrade filter sees the CURRENT state (recovered or not).
    nodes.append(_node(
        "Load latest situation (post-delay)",
        "org.thingsboard.rule.engine.metadata.TbGetAttributesNode",
        1650, 560,
        {
            "clientAttributeNames": ["last_situation"],
            "sharedAttributeNames": [],
            "serverAttributeNames": [],
            "latestTsKeyNames": ["last_situation"],
            "tellFailureIfAbsent": False,
            "getLatestValueWithTs": False,
            "fetchTo": "METADATA",
        },
    ))

    # Optional FCM push: POST each newly-Created alarm to the local relay
    # (scripts/fcm_relay.py), which forwards it to Firebase Cloud Messaging.
    # TB runs in Docker, so "localhost" would be the container itself — the
    # default relay URL uses host.docker.internal instead.
    if fcm_relay_url and fcm_relay_secret:
        nodes.append(_node(
            "FCM push relay",
            "org.thingsboard.rule.engine.rest.TbRestApiCallNode",
            1500, 320,
            {
                "restEndpointUrlPattern": fcm_relay_url,
                "requestMethod": "POST",
                "headers": {
                    "Content-Type": "application/json",
                    "X-Relay-Secret": fcm_relay_secret,
                    "X-Device-Name": "${deviceName}",
                },
                "useSimpleClientHttpFactory": False,
                "parseToPlainText": False,
                "ignoreRequestBody": False,
                "enableProxy": False,
                "readTimeoutMs": 5000,
                "maxParallelRequestsCount": 0,
                "useRedisQueueForMsgPersistence": False,
                "trimQueue": False,
                "maxQueueSize": 0,
                "credentials": {"type": "anonymous"},
            },
        ))

    # Wire connections by node NAME (resolved to indices here) so the topology is
    # robust to nodes being added/removed without renumbering every index.
    idx = {node["name"]: i for i, node in enumerate(nodes)}

    def _conn(frm: str, to: str, type_: str) -> dict:
        return {"fromIndex": idx[frm], "toIndex": idx[to], "type": type_}

    connections = [
        # Type Switch routes:
        _conn("Message Type Switch", "Enrich derived telemetry", "Post telemetry"),
        _conn("Message Type Switch", "Save Client Attributes", "Post attributes"),
        # JS transform → save timeseries + all branch filters
        _conn("Enrich derived telemetry", "Save Timeseries", "Success"),
        _conn("Enrich derived telemetry", "Filter: escalation==SOS_COLLAPSE", "Success"),
        _conn("Enrich derived telemetry", "Filter: escalation==ASK_OK", "Success"),
        _conn("Enrich derived telemetry", "Filter: ack_seen", "Success"),
        # Filter → Alarm on True
        _conn("Filter: escalation==SOS_COLLAPSE", "Alarm: MedicalCollapse", "True"),
        _conn("Filter: escalation==ASK_OK", "Alarm: FallSuspected", "True"),
        # B2: ASK_OK original telemetry → delay 20s → load latest situation →
        #     still-FALL filter → auto-upgrade to MedicalCollapse
        _conn("Filter: escalation==ASK_OK", "Delay 20s (fall response window)", "True"),
        _conn("Delay 20s (fall response window)", "Load latest situation (post-delay)", "Success"),
        _conn("Load latest situation (post-delay)", "Filter: still FALL after delay", "Success"),
        _conn("Load latest situation (post-delay)", "Filter: still FALL after delay", "Failure"),
        _conn("Filter: still FALL after delay", "Alarm: MedicalCollapse (auto-upgrade)", "True"),
        # B4: ack_seen → transform to rescue_state telemetry → save timeseries
        _conn("Filter: ack_seen", "Save rescue_state", "True"),
        _conn("Save rescue_state", "Save Timeseries", "Success"),
    ]

    if fcm_relay_url and fcm_relay_secret:
        # Created only — TbCreateAlarmNode emits Updated for an already-ACTIVE
        # alarm of the same type, so this wiring never pushes the same incident
        # twice. The auto-upgrade node creates a *new* MedicalCollapse alarm
        # (different type from the existing FallSuspected), so an escalation
        # still pushes exactly once.
        connections += [
            _conn("Alarm: MedicalCollapse", "FCM push relay", "Created"),
            _conn("Alarm: FallSuspected", "FCM push relay", "Created"),
            _conn("Alarm: MedicalCollapse (auto-upgrade)", "FCM push relay", "Created"),
        ]

    return {
        "ruleChainId": {"entityType": "RULE_CHAIN", "id": chain_id},
        "firstNodeIndex": idx["Message Type Switch"],
        "nodes": nodes,
        "connections": connections,
        "ruleChainConnections": None,
    }


def provision_rule_chain(
    client: TbClient,
    name: str,
    backup_dir: Path,
    *,
    fcm_relay_url: str = "",
    fcm_relay_secret: str = "",
) -> str:
    """Create / replace the named rule chain and set as root.

    Returns the rule chain ID.
    """
    # 1. Backup current root
    chains = client.get("/api/ruleChains?pageSize=100&page=0").get("data", [])
    root = next((c for c in chains if c.get("root")), None)
    if root is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().isoformat(timespec="seconds").replace(":", "-")
        chain_id = root["id"]["id"]
        meta = client.get(f"/api/ruleChain/{chain_id}/metadata")
        backup_path = backup_dir / f"root_{ts}.json"
        backup_path.write_text(
            json.dumps({"chain": root, "metadata": meta}, indent=2),
            encoding="utf-8",
        )
        print(f"  backed up current root '{root['name']}' to {backup_path.name}")

    # 2. Find or create the named chain
    existing = next((c for c in chains if c.get("name") == name), None)
    if existing is not None:
        chain = existing
        print(f"  found rule chain '{name}', will update metadata")
    else:
        chain = client.post(
            "/api/ruleChain",
            {"name": name, "type": "CORE", "debugMode": False},
        )
        print(f"  created rule chain '{name}'")

    chain_id = chain["id"]["id"]

    # 3. POST metadata
    metadata = build_rule_chain_metadata(
        chain_id, fcm_relay_url=fcm_relay_url, fcm_relay_secret=fcm_relay_secret
    )
    client.post("/api/ruleChain/metadata", metadata)
    print(f"  installed {len(metadata['nodes'])} nodes, {len(metadata['connections'])} connections")

    # 4. Set as root
    client.post(f"/api/ruleChain/{chain_id}/root", {})
    print(f"  set '{name}' as root")

    return chain_id


# ---------- Dashboard ----------


def _widget(*, title: str, fqn: str, sizeX: int, sizeY: int, config: dict, type_: str = "latest") -> dict:
    # NB: widget id is filled in by `add()` so it matches the dict key.
    return {
        "id": None,
        "typeFullFqn": f"system.{fqn}",
        "type": type_,
        "sizeX": sizeX,
        "sizeY": sizeY,
        "config": {**config, "title": title},
    }


def _ds_latest(alias_id: str, keys: list[tuple[str, str]], ds_name: str | None = None) -> list[dict]:
    """One latest-value datasource referencing the entity alias.

    keys: list of (name, type) where type is 'timeseries' or 'attribute'.
    """
    return [{
        "type": "entity",
        "name": ds_name,
        "entityAliasId": alias_id,
        "filterId": None,
        "dataKeys": [
            {
                "name": k_name,
                "type": k_type,
                "label": k_name,
                "color": _color_for(i),
                "settings": {},
                "_hash": _new_hash(),
                "funcBody": None,
            }
            for i, (k_name, k_type) in enumerate(keys)
        ],
    }]


_PALETTE = [
    "#00bcd4", "#ff5252", "#ffc107", "#4caf50",
    "#9c27b0", "#03a9f4", "#ff9800", "#e91e63",
    "#8bc34a", "#3f51b5", "#cddc39", "#00bfa5",
]


def _color_for(i: int) -> str:
    return _PALETTE[i % len(_PALETTE)]


def _new_hash() -> float:
    # TB convention: small float in [0,1); inspected on real working dashboards.
    return (uuid.uuid4().int % 1_000_000_000) / 1_000_000_000


def _empty_settings() -> dict:
    return {"showLegend": True, "showTitle": True, "dropShadow": True, "enableFullscreen": True}


# ---- Widget builders ----


def kpi_card(alias_id: str, title: str, key: str, color: str, agg: str | None = None) -> dict:
    """Simple count/value card. agg: 'COUNT' / 'AVG' / 'MAX' / None (latest)."""
    cfg = {
        "datasources": _ds_latest(alias_id, [(key, "timeseries")]),
        "timewindow": {
            "realtime": {"timewindowMs": 86400000},
            "aggregation": {"type": agg or "NONE", "limit": 25000},
        },
        "showTitle": True,
        "backgroundColor": color,
        "color": "rgba(255, 255, 255, 0.87)",
        "padding": "16px",
        "settings": {
            "labelPosition": "top",
            "showLegend": False,
        },
        "title": title,
        "dropShadow": True,
        "enableFullscreen": False,
        "titleStyle": {"fontSize": "16px", "fontWeight": 500},
        "useDashboardTimewindow": False,
        "displayTimewindow": False,
        "actions": {},
        "noDataDisplayMessage": "—",
        "widgetStyle": {},
    }
    return _widget(
        title=title,
        fqn="cards.simple_card",
        sizeX=6,
        sizeY=3,
        config=cfg,
    )


def time_series(alias_id: str, title: str, keys: list[str], timewindow_ms: int = 300000,
                sizeX: int = 12, sizeY: int = 6) -> dict:
    cfg = {
        "datasources": _ds_latest(alias_id, [(k, "timeseries") for k in keys]),
        "timewindow": {
            "realtime": {"timewindowMs": timewindow_ms},
            "aggregation": {"type": "NONE", "limit": 25000},
        },
        "showTitle": True,
        "title": title,
        "dropShadow": True,
        "enableFullscreen": True,
        "showLegend": True,
        "useDashboardTimewindow": False,
        "displayTimewindow": True,
        "settings": {
            "shadowSize": 4,
            "fontColor": "#545454",
            "fontSize": 10,
            "xaxis": {"showLabels": True, "color": "#545454"},
            "yaxis": {"showLabels": True, "color": "#545454"},
            "grid": {
                "verticalLines": True,
                "horizontalLines": True,
                "outlineWidth": 1,
                "color": "#000",
                "tickColor": "#000",
                "backgroundColor": "transparent",
            },
            "stack": False,
            "smoothLines": True,
            "thresholdsLineWidth": 2,
            "comparisonEnabled": False,
            "showTooltip": True,
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(
        title=title,
        fqn="charts.basic_timeseries",
        sizeX=sizeX,
        sizeY=sizeY,
        config=cfg,
        type_="timeseries",
    )


def latest_table(alias_id: str, title: str, keys: list[str]) -> dict:
    cfg = {
        "datasources": _ds_latest(alias_id, [(k, "timeseries") for k in keys]),
        "timewindow": {"realtime": {"timewindowMs": 60000}},
        "showTitle": True,
        "title": title,
        "dropShadow": True,
        "enableFullscreen": True,
        "showLegend": False,
        "useDashboardTimewindow": False,
        "settings": {
            "enableSelectColumnDisplay": True,
            "enableSearch": False,
            "displayPagination": False,
            "defaultSortOrder": "-Timestamp",
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(
        title=title,
        fqn="cards.attributes_card",
        sizeX=12,
        sizeY=6,
        config=cfg,
    )


def alarms_table(
    alias_id: str,
    title: str,
    *,
    sizeX: int = 12,
    sizeY: int = 6,
    status_list: list[str] | None = None,
    timewindow_ms: int = 86400000,
) -> dict:
    """Alarms table aligned with TB 4.3 Thermostats reference config.

    status_list: ["ACTIVE_UNACK", "ACTIVE_ACK"] for active-only; empty/None = ANY.
    """
    cfg = {
        "timewindow": {
            "realtime": {"interval": 1000, "timewindowMs": timewindow_ms},
            "aggregation": {"type": "NONE", "limit": 200},
        },
        "showTitle": True,
        "backgroundColor": "rgb(255, 255, 255)",
        "color": "rgba(0, 0, 0, 0.87)",
        "padding": "4px",
        "settings": {
            "enableSelection": False,
            "enableSearch": True,
            "displayDetails": True,
            "allowAcknowledgment": True,
            "allowClear": True,
            "allowAssign": True,
            "displayComments": True,
            "displayPagination": True,
            "defaultPageSize": 10,
            "defaultSortOrder": "-createdTime",
            "enableSelectColumnDisplay": False,
            "alarmsTitle": title,
            "enableFilter": True,
        },
        "title": title,
        "dropShadow": True,
        "enableFullscreen": False,
        "titleStyle": {"fontSize": "16px", "fontWeight": 400, "padding": "5px 10px 5px 2px"},
        "useDashboardTimewindow": False,
        "showLegend": False,
        "alarmSource": {
            "type": "entity",
            "name": "alarms",
            "entityAliasId": alias_id,
            "filterId": None,
            "dataKeys": [
                {"name": "createdTime", "type": "alarm", "label": "Created",
                 "color": "#2196f3", "settings": {}, "_hash": _new_hash()},
                {"name": "originator", "type": "alarm", "label": "Originator",
                 "color": "#4caf50", "settings": {}, "_hash": _new_hash()},
                {"name": "type", "type": "alarm", "label": "Type",
                 "color": "#f44336", "settings": {}, "_hash": _new_hash()},
                {"name": "severity", "type": "alarm", "label": "Severity",
                 "color": "#ffc107", "settings": {}, "_hash": _new_hash()},
                {"name": "status", "type": "alarm", "label": "Status",
                 "color": "#607d8b", "settings": {}, "_hash": _new_hash()},
            ],
        },
        "alarmsPollingInterval": 5,
        "showTitleIcon": False,
        "titleIcon": None,
        "iconColor": "rgba(0, 0, 0, 0.87)",
        "iconSize": "24px",
        "titleTooltip": "",
        "widgetStyle": {},
        "displayTimewindow": True,
        "actions": {},
        "datasources": [],
        "alarmsMaxCountLoad": 0,
        "alarmsFetchSize": 100,
        "alarmFilterConfig": {
            "statusList": status_list or [],
            "severityList": [],
            "typeList": [],
            "searchPropagatedAlarms": True,
        },
        "widgetCss": "",
        "pageSize": 1024,
        "noDataDisplayMessage": "",
    }
    return _widget(
        title=title,
        fqn="alarm_widgets.alarms_table",
        sizeX=sizeX,
        sizeY=sizeY,
        config=cfg,
        type_="alarm",
    )


def entities_table(
    alias_id: str,
    title: str,
    columns: list[tuple[str, str, str | None, int | None]],
    *,
    sizeX: int = 12,
    sizeY: int = 6,
    entities_title: str | None = None,
    entity_name_column_title: str = "Worker",
    color_status_keys: tuple[str, ...] = (),
    numeric_thresholds: dict[str, tuple[float, float]] | None = None,
) -> dict:
    """Entities admin table aligned with TB 4.3 Thermostats reference config.

    columns: list of (key_name, key_type, units, decimals) tuples.
    color_status_keys: keys to render as a coloured status dot.
        true/SAFE/ACTIVE_UNACK → green; false/null → grey; other → orange.
    numeric_thresholds: { key_name: (yellow_min, red_min) } — colour the cell
        text green/yellow/red based on the numeric value (e.g. bpm).
    """
    numeric_thresholds = numeric_thresholds or {}
    data_keys = []
    for i, (kname, ktype, units, decimals) in enumerate(columns):
        is_status = kname in color_status_keys
        is_numeric_thresh = kname in numeric_thresholds
        key = {
            "name": kname,
            "type": ktype,
            "label": kname,
            "color": _color_for(i),
            "settings": {
                "columnWidth": "0px",
                "useCellStyleFunction": is_status or is_numeric_thresh,
                "useCellContentFunction": is_status,
            },
            "_hash": _new_hash(),
        }
        if units is not None:
            key["units"] = units
        if decimals is not None:
            key["decimals"] = decimals
        if is_status:
            # HTML entity (●) — proven to render in TB 4.3 (Thermostats uses this).
            key["settings"]["cellContentFunction"] = "value = '&#11044;'; return value;"
            key["settings"]["cellStyleFunction"] = (
                "var v = value; "
                "var color = (v === true || v === 'true' || v === 'SAFE' || v === 'ACTIVE_UNACK') ? 'rgb(39,134,34)' : "
                "(v === false || v === 'false' || v == null || v === '') ? 'rgb(180,180,180)' : 'rgb(255,107,53)'; "
                "return {color: color, fontSize: '18px'};"
            )
        elif is_numeric_thresh:
            yellow_min, red_min = numeric_thresholds[kname]
            key["settings"]["cellStyleFunction"] = (
                f"var n = +value; "
                f"if (isNaN(n) || n === 0) return {{color: 'rgb(120,120,120)'}}; "
                f"var color = n >= {red_min} ? 'rgb(220,38,38)' : "
                f"n >= {yellow_min} ? 'rgb(245,158,11)' : 'rgb(34,134,34)'; "
                f"return {{color: color, fontWeight: 600}};"
            )
        data_keys.append(key)

    cfg = {
        "showTitle": True,
        "backgroundColor": "rgb(255, 255, 255)",
        "color": "rgba(0, 0, 0, 0.87)",
        "padding": "4px",
        "settings": {
            "enableSearch": False,
            "displayPagination": True,
            "defaultPageSize": 10,
            "defaultSortOrder": "entityName",
            "displayEntityName": True,
            "displayEntityType": False,
            "enableSelectColumnDisplay": False,
            "entitiesTitle": entities_title or title,
            "displayEntityLabel": False,
            "entityNameColumnTitle": entity_name_column_title,
        },
        "title": title,
        "dropShadow": True,
        "enableFullscreen": False,
        "titleStyle": {"fontSize": "16px", "fontWeight": 400, "padding": "5px 10px 5px 10px"},
        "showLegend": False,
        "datasources": [
            {
                "type": "entity",
                "name": None,
                "entityAliasId": alias_id,
                "dataKeys": data_keys,
            }
        ],
        "showTitleIcon": False,
        "titleIcon": None,
        "iconColor": "rgba(0, 0, 0, 0.87)",
        "iconSize": "24px",
        "titleTooltip": "",
        "widgetStyle": {},
        "actions": {},
    }
    return _widget(
        title=title,
        fqn="cards.entities_table",
        sizeX=sizeX,
        sizeY=sizeY,
        config=cfg,
    )


def pie_chart(alias_id: str, title: str, keys: list[str], agg: str = "MAX") -> dict:
    cfg = {
        "datasources": _ds_latest(alias_id, [(k, "timeseries") for k in keys]),
        "timewindow": {
            "realtime": {"timewindowMs": 86400000},
            "aggregation": {"type": agg, "limit": 25000},
        },
        "showTitle": True,
        "title": title,
        "dropShadow": True,
        "enableFullscreen": True,
        "showLegend": True,
        "useDashboardTimewindow": True,
        "displayTimewindow": True,
        "settings": {
            "radius": 0.8,
            "tiltAngle": 30,
            "labels": {"show": True, "stroke": False},
            "lineColors": {"show": False},
            "showPercentages": True,
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(
        title=title,
        fqn="charts.pie",
        sizeX=8,
        sizeY=7,
        config=cfg,
    )


def bar_chart(alias_id: str, title: str, keys: list[str]) -> dict:
    cfg = {
        "datasources": _ds_latest(alias_id, [(k, "timeseries") for k in keys]),
        "timewindow": {
            "realtime": {"timewindowMs": 86400000},
            "aggregation": {"type": "COUNT", "limit": 25000},
        },
        "showTitle": True,
        "title": title,
        "dropShadow": True,
        "enableFullscreen": True,
        "showLegend": True,
        "useDashboardTimewindow": True,
        "settings": {"stack": False, "shadowSize": 4},
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(
        title=title,
        fqn="charts.bars",
        sizeX=12,
        sizeY=6,
        config=cfg,
        type_="timeseries",
    )


def radial_gauge(alias_id: str, title: str, key: str, max_value: float = 100) -> dict:
    cfg = {
        "datasources": _ds_latest(alias_id, [(key, "timeseries")]),
        "timewindow": {"realtime": {"timewindowMs": 60000}},
        "showTitle": True,
        "title": title,
        "dropShadow": True,
        "enableFullscreen": True,
        "useDashboardTimewindow": False,
        "settings": {
            "minValue": 0,
            "maxValue": max_value,
            "unitTitle": "",
            "majorTicksCount": 5,
            "highlights": [
                {"from": 0, "to": 0.3 * max_value, "color": "rgba(76, 175, 80, 0.5)"},
                {"from": 0.3 * max_value, "to": 0.6 * max_value, "color": "rgba(255, 193, 7, 0.5)"},
                {"from": 0.6 * max_value, "to": max_value, "color": "rgba(244, 67, 54, 0.5)"},
            ],
            "showBorder": True,
            "animationDuration": 800,
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(
        title=title,
        fqn="analogue_gauges.radial_gauge_canvas_gauges",
        sizeX=8,
        sizeY=6,
        config=cfg,
    )


def _map_keys() -> list[dict]:
    # NB: labels are kept identical to the key names — the map's label/tooltip/
    # marker functions all read `data['<label>']`, so a label that differs from
    # the name (the old 'type'/'esc' aliases) silently breaks those lookups.
    keys = [
        ("lat", "timeseries"), ("lon", "timeseries"),
        ("active", "timeseries"), ("entity_type", "timeseries"),
        ("bpm", "timeseries"), ("last_escalation", "timeseries"),
        ("last_situation", "timeseries"), ("zone", "timeseries"),
        ("battery", "timeseries"), ("worn", "timeseries"),
        ("resident_name", "attribute"),
    ]
    return [
        {"name": n, "type": t, "label": n,
         "color": _color_for(i), "settings": {}, "_hash": _new_hash()}
        for i, (n, t) in enumerate(keys)
    ]


# 陽明交大 光復校區 (NYCU Guangfu campus, Hsinchu)
NYCU_CENTER = "24.78686,120.99681"

# ---- Map marker / label / tooltip functions ----
# Big SVG pin (44px, white ring + drop shadow) so people actually spot it on the
# map; glyph + colour encode the state: 🟢♥ wearer normal, 🟠/🔴 ! fall states,
# 🔵 G guardian. Inactive entities return a 1×1 transparent image (hidden).
_MAP_PIN_JS = (
    "var d = data || {};"
    "function uri(s){return 'data:image/svg+xml;charset=UTF-8,'+encodeURIComponent(s);}"
    "if (d['active'] === false || d['active'] === 'false') {"
    " return uri(\"<svg xmlns='http://www.w3.org/2000/svg' width='1' height='1'></svg>\"); }"
    "var c = '#16a34a', t = '\\u2665';"
    "if (d['entity_type'] === 'guardian') { c = '#2563eb'; t = 'G'; }"
    "else if (d['last_escalation'] === 'SOS_COLLAPSE') { c = '#dc2626'; t = '!'; }"
    "else if (d['last_escalation'] === 'ASK_OK') { c = '#f59e0b'; t = '!'; }"
    "var s = \"<svg xmlns='http://www.w3.org/2000/svg' width='44' height='52' viewBox='0 0 44 52'>\""
    " + \"<defs><filter id='sh' x='-30%' y='-20%' width='160%' height='150%'>\""
    " + \"<feDropShadow dx='0' dy='2' stdDeviation='2' flood-opacity='0.35'/></filter></defs>\""
    " + \"<path filter='url(#sh)' d='M22 1.5C11.8 1.5 3.5 9.8 3.5 20c0 13 18.5 30 18.5 30s18.5-17 18.5-30C40.5 9.8 32.2 1.5 22 1.5z'\""
    " + \" fill='\" + c + \"' stroke='#ffffff' stroke-width='2.5'/>\""
    " + \"<circle cx='22' cy='20' r='11' fill='#ffffff'/>\""
    " + \"<text x='22' y='25' text-anchor='middle' font-family='Roboto,Arial,sans-serif'\""
    " + \" font-size='14' font-weight='800' fill='\" + c + \"'>\" + t + \"</text>\""
    " + \"</svg>\";"
    "return uri(s);"
)

# White name chip under the pin: resident name (or id) + live ♥bpm for wearers,
# guardian id for guardians. Border colour repeats the pin's state colour.
_MAP_LABEL_JS = (
    "var d = data || {};"
    "if (d['active'] === false || d['active'] === 'false') { return ''; }"
    "var c = '#16a34a';"
    "if (d['entity_type'] === 'guardian') { c = '#2563eb'; }"
    "else if (d['last_escalation'] === 'SOS_COLLAPSE') { c = '#dc2626'; }"
    "else if (d['last_escalation'] === 'ASK_OK') { c = '#f59e0b'; }"
    "var nm = String(d['entityName'] || '');"
    "var isW = nm.indexOf('Wearer_') === 0;"
    "var disp = isW ? (d['resident_name'] || nm.replace('Wearer_','')) : nm.replace('Guardian_','');"
    "var bpm = (isW && d['bpm'] != null) ? ' \\u2665' + Math.round(d['bpm']) : '';"
    "return '<div style=\"background:#fff;border:2px solid ' + c + ';border-radius:11px;"
    "padding:1px 8px;font-family:Roboto,\\'Noto Sans TC\\',sans-serif;font-size:12px;font-weight:700;"
    "color:#0f172a;box-shadow:0 1px 4px rgba(0,0,0,0.3);white-space:nowrap;\">'"
    " + disp + '<span style=\"color:' + c + ';\">' + bpm + '</span></div>';"
)

# Click tooltip = mini status card in Chinese: name + state pill, then
# situation / HR / named zone / battery / HR-sync rows (wearer) or duty row
# (guardian). All values are live datasource keys.
_MAP_TOOLTIP_JS = (
    "var d = data || {};"
    "var nm = String(d['entityName'] || '');"
    "var isW = nm.indexOf('Wearer_') === 0;"
    "var disp = isW ? (d['resident_name'] || nm.replace('Wearer_','')) : nm.replace('Guardian_','');"
    "var c = '#16a34a', st = '\\u72c0\\u614b\\u6b63\\u5e38 Normal';"  # 狀態正常
    "if (!isW) { c = '#2563eb'; st = '\\u5b88\\u8b77\\u8005 Guardian'; }"  # 守護者
    "else if (d['last_escalation'] === 'SOS_COLLAPSE') { c = '#dc2626'; st = '\\ud83c\\udd98 \\u5012\\u5730 Collapse'; }"  # 🆘 倒地
    "else if (d['last_escalation'] === 'ASK_OK') { c = '#f59e0b'; st = '\\u26a0\\ufe0f \\u7591\\u4f3c\\u8dcc\\u5012 Fall?'; }"  # ⚠️ 疑似跌倒
    "function r(l, v){ return '<div style=\"display:flex;justify-content:space-between;gap:18px;"
    "padding:3px 0;border-bottom:1px solid #eef2f7;font-size:12px;\">"
    "<span style=\"color:#64748b;\">' + l + '</span><b>' + v + '</b></div>'; }"
    "var rows = '';"
    "if (isW) {"
    " rows += r('\\u5fc3\\u7387 HR', (d['bpm'] != null ? Math.round(d['bpm']) + ' bpm' : '—'));"  # 心率
    " rows += r('\\u4f4d\\u7f6e Location', '\\ud83d\\udccd ' + (d['zone'] || '—'));"  # 位置 📍
    " rows += r('\\u624b\\u9336\\u96fb\\u91cf Battery', (d['battery'] != null ? Math.round(d['battery']) + '%' : '—'));"  # 手錶電量
    " var wv = d['worn'];"
    " rows += r('\\u4f69\\u6234\\u72c0\\u614b Worn', (wv===true||wv==='true') ? '\\u2705 \\u914d\\u6234\\u4e2d'"  # 佩戴狀態 ✅ 配戴中
    " : ((wv===false||wv==='false') ? '\\u26a0\\ufe0f \\u672a\\u540c\\u6b65\\u5fc3\\u7387' : '—'));"  # ⚠️ 未同步心率
    "} else {"
    " var act = (d['active']===true||d['active']==='true');"
    " rows += r('\\u52e4\\u52d9 Duty', act ? '\\ud83d\\udfe2 \\u503c\\u73ed\\u4e2d On duty' : '\\u96e2\\u7dda Offline');"  # 勤務 🟢 值班中 / 離線
    "}"
    "return '<div style=\"font-family:Roboto,\\'Noto Sans TC\\',sans-serif;min-width:190px;padding:2px;\">'"
    " + '<div style=\"display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px;\">'"
    " + '<b style=\"font-size:14px;\">' + disp + '</b>'"
    " + '<span style=\"background:' + c + '1f;color:' + c + ';font-size:11px;font-weight:700;"
    "padding:2px 9px;border-radius:10px;white-space:nowrap;\">' + st + '</span></div>'"
    " + rows + '</div>';"
)


def workers_map(workers_alias_id: str, title: str = "Workers map (live)",
                sizeX: int = 24, sizeY: int = 10,
                guardians_alias_id: str | None = None,
                center: str | None = None) -> dict:
    """OpenStreetMap with wearers (green) and, if given, guardians (blue) on the
    SAME map. Colour by entity_type; alarming wearers turn red."""
    datasources = [{
        "type": "entity", "name": "Wearers", "entityAliasId": workers_alias_id,
        "filterId": None, "dataKeys": _map_keys(),
    }]
    if guardians_alias_id:
        datasources.append({
            "type": "entity", "name": "Guardians", "entityAliasId": guardians_alias_id,
            "filterId": None, "dataKeys": _map_keys(),
        })
    cfg = {
        "datasources": datasources,
        "timewindow": {"realtime": {"timewindowMs": 86400000}, "aggregation": {"type": "NONE", "limit": 25000}},
        "showTitle": True,
        "title": title,
        "dropShadow": True,
        "enableFullscreen": True,
        "useDashboardTimewindow": False,
        "settings": {
            "latKeyName": "lat",
            "lngKeyName": "lon",
            # Big state-coloured SVG pins (see _MAP_PIN_JS) instead of the tiny
            # default dot, plus a name+bpm chip and a Chinese mini status card
            # on click. colorFunction stays as a fallback for renderers that
            # ignore the image function.
            "useMarkerImageFunction": True,
            "markerImageFunction": _MAP_PIN_JS,
            "markerImageSize": 44,
            "showLabel": True,
            "useLabelFunction": True,
            "label": "${entityName}",
            "labelFunction": _MAP_LABEL_JS,
            "useColorFunction": True,
            "colorFunction": (
                "if (data && (data['active'] === false || data['active'] === 'false')) { return 'rgba(0,0,0,0)'; } "
                "if (data && (data['entity_type'] === 'guardian')) { return '#2563eb'; } "
                "if (data && data['last_escalation'] === 'SOS_COLLAPSE') { return '#dc2626'; } "
                "if (data && data['last_escalation'] === 'ASK_OK') { return '#f59e0b'; } "
                "return '#16a34a';"
            ),
            "showTooltip": True,
            "showTooltipAction": "click",
            "autocloseTooltip": True,
            "useTooltipFunction": True,
            "tooltipFunction": _MAP_TOOLTIP_JS,
            "tooltipPattern": (
                "<b>${entityName}</b><br/>"
                "位置 zone: ${zone}<br/>"
                "bpm: ${bpm}<br/>"
                "esc: ${last_escalation}"
            ),
            "defaultZoomLevel": 16,
            "useDefaultCenterPosition": bool(center),
            "defaultCenterPosition": center or "0,0",
            "fitMapBounds": True,
            "mapProvider": "OpenStreetMap.Mapnik",
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(
        title=title,
        fqn="maps_v2.openstreetmap",
        sizeX=sizeX,
        sizeY=sizeY,
        config=cfg,
    )


def html_card(title: str, html: str, sizeX: int = 24, sizeY: int = 3) -> dict:
    cfg = {
        "datasources": [],
        "timewindow": {"realtime": {"timewindowMs": 60000}},
        "showTitle": False,
        "title": title,
        "dropShadow": False,
        "enableFullscreen": False,
        "settings": {"cardHtml": html, "cardCss": ""},
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(
        title=title,
        fqn="cards.html_card",
        sizeX=sizeX,
        sizeY=sizeY,
        config=cfg,
    )


# ---- Care-console widgets (avatar digital-twin + one-click call) ----

# markdown_card JS value function: data[0]['<label>'] + data[0]['entityName'].
_AVATAR_JS = (
    "var d = data[0] || {};"
    "var name = (d['entityName'] || 'Wearer_?').replace('Wearer_','');"
    "var bpm = (d['bpm'] != null) ? Math.round(d['bpm'])"
    "          : (d['hr_max'] != null ? Math.round(d['hr_max']) : '--');"
    "var sit = d['last_situation'] || 'NORMAL';"
    "var esc = d['last_escalation'] || 'NONE';"
    "var color = '#16a34a', label = '正常 Normal', icon='🟢';"
    "if (esc === 'SOS_COLLAPSE') { color='#dc2626'; label='倒地 Collapse'; icon='🆘'; }"
    "else if (esc === 'ASK_OK') { color='#f59e0b'; label='疑似跌倒 Fall?'; icon='⚠️'; }"
    "var ini = name.replace('W-','').slice(-2);"
    "var hb = (d['hr_above_baseline'] != null) ? ('HR ↑' + Math.round(d['hr_above_baseline']) + ' vs 基線') : '心率正常';"
    "return '<div class=\"gv\">'"
    "+ '<div class=\"gv-av\" style=\"border-color:'+color+';box-shadow:0 0 0 4px '+color+'22;\"><span>'+ini+'</span></div>'"
    "+ '<div class=\"gv-meta\">'"
    "+ '<div class=\"gv-nm\">'+name+' <span class=\"gv-tag\">配戴者</span></div>'"
    "+ '<div class=\"gv-bpm\" style=\"color:'+color+';\">'+bpm+'<small>bpm</small></div>'"
    "+ '<div class=\"gv-bd\" style=\"background:'+color+';\">'+icon+' '+label+'</div>'"
    "+ '<div class=\"gv-sub\">'+hb+'</div>'"
    "+ '</div></div>';"
)

_AVATAR_CSS = (
    ".gv{display:flex;align-items:center;gap:16px;height:100%;padding:10px 16px;"
    "box-sizing:border-box;font-family:Roboto,system-ui,sans-serif;container-type:size;}"
    ".gv-av{width:clamp(48px,17.2cqmin,66px);height:clamp(48px,17.2cqmin,66px);border-radius:50%;border:3px solid #16a34a;display:flex;"
    "align-items:center;justify-content:center;background:#0c1320;flex:0 0 auto;}"
    ".gv-av span{color:#fff;font-size:clamp(16px,5.7cqmin,22px);font-weight:700;}"
    ".gv-meta{display:flex;flex-direction:column;gap:4px;min-width:0;}"
    ".gv-nm{font-size:clamp(12px,4.2cqmin,16px);font-weight:700;color:#0c1320;}"
    ".gv-tag{font-size:clamp(9px,2.6cqmin,10px);font-weight:600;color:#64748b;background:#e2e8f0;"
    "padding:1px clamp(4px,1.6cqmin,6px);border-radius:clamp(6px,2.1cqmin,8px);margin-left:4px;}"
    ".gv-bpm{font-size:clamp(22px,7.8cqmin,30px);font-weight:800;line-height:1;}"
    ".gv-bpm small{font-size:clamp(9px,3.1cqmin,12px);font-weight:600;margin-left:3px;color:#64748b;}"
    ".gv-bd{align-self:flex-start;color:#fff;font-size:clamp(9px,3.1cqmin,12px);font-weight:700;"
    "padding:2px clamp(9px,2.6cqmin,10px);border-radius:clamp(9px,2.6cqmin,10px);}"
    ".gv-sub{font-size:clamp(9px,2.9cqmin,11px);color:#64748b;font-weight:600;}"
)


# Dense vitals KPI grid (2x2) + status strip — both markdown_card value functions.
_VITALS_JS = (
    "var d = data[0] || {};"
    "function n(v,dp){ return (v!=null && !isNaN(v)) ? Number(v).toFixed(dp||0) : '--'; }"
    "var hrmax=d['hr_max'], hb=d['hr_above_baseline'], conf=d['situation_confidence'], still=d['post_impact_stillness'];"
    "var hrc=(hrmax>=150?'#dc2626':(hrmax>=110?'#ea580c':'#16a34a'));"
    "var hbc=(hb>=40?'#dc2626':(hb>=25?'#ea580c':'#16a34a'));"
    "function t(l,v,u,c){ return '<div class=\"vt\"><div class=\"vl\">'+l+'</div><div class=\"vv\" style=\"color:'+(c||'#0c1320')+'\">'+v+'<small>'+(u||'')+'</small></div></div>'; }"
    "return '<div class=\"vg\">'"
    "+ t('最高心率 Max HR', n(hrmax,0), ' bpm', hrc)"
    "+ t('心率高於基線 ΔHR', (hb!=null?(hb>0?'+':'')+n(hb,0):'--'), ' bpm', hbc)"
    "+ t('判讀信心 Confidence', (conf!=null?Math.round(conf*100):'--'), ' %', '#0891b2')"
    "+ t('撞擊後靜止 Stillness', n(still,2), '', '#64748b')"
    "+ '</div>';"
)

_VITALS_CSS = (
    ".vg{display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr 1fr;gap:10px;"
    "height:100%;padding:10px;box-sizing:border-box;font-family:Roboto,system-ui,sans-serif;container-type:size;}"
    ".vt{background:#f1f5f9;border-radius:clamp(7px,1.8cqmin,10px);padding:clamp(6px,1.4cqmin,8px) clamp(9px,2.2cqmin,12px);display:flex;flex-direction:column;"
    "justify-content:center;}"
    ".vl{font-size:clamp(9px,2cqmin,11px);color:#64748b;font-weight:600;margin-bottom:2px;}"
    ".vv{font-size:clamp(19px,4.7cqmin,26px);font-weight:800;line-height:1.05;}"
    ".vv small{font-size:clamp(9px,2cqmin,11px);font-weight:600;margin-left:2px;color:#94a3b8;}"
)

_STATUS_JS = (
    "var d = data[0] || {};"
    "var rs = d['rescue_state'];"
    "var rsTxt = rs==='guardian_enroute' ? '🏃 守護者前往中' : (rs ? rs : '— 無進行中救援');"
    "var rsC = rs==='guardian_enroute' ? '#16a34a' : '#64748b';"
    "var act = (d['active']===true||d['active']==='true') ? '🟢 連線中' : '⚪ 離線';"
    "function ts(v){ if(v==null) return '—'; var t=Number(v); if(isNaN(t)) return String(v); var dt=new Date(t); return dt.toLocaleString(); }"
    "function it(l,v,c){ return '<div class=\"si\"><span class=\"sl\">'+l+'</span><span class=\"sv\" style=\"color:'+(c||'#0c1320')+'\">'+v+'</span></div>'; }"
    "return '<div class=\"sg\">'"
    "+ it('救援狀態 Rescue', rsTxt, rsC)"
    "+ it('裝置 Device', act, '#0c1320')"
    "+ it('守護者確認 Ack', (d['guardian_ack']||'—'), '#0c1320')"
    "+ it('最近恢復 Recovered', ts(d['last_recovery_at']), '#0c1320')"
    "+ '</div>';"
)

_STATUS_CSS = (
    ".sg{display:flex;flex-direction:column;justify-content:center;gap:9px;height:100%;"
    "padding:10px 16px;box-sizing:border-box;font-family:Roboto,system-ui,sans-serif;container-type:size;}"
    ".si{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #eef2f7;"
    "padding-bottom:clamp(5px,3.3cqmin,7px);}"
    ".sl{font-size:clamp(9px,5.7cqmin,12px);color:#64748b;font-weight:600;}"
    ".sv{font-size:clamp(11px,6.1cqmin,13px);font-weight:700;}"
)


def _md_panel(alias_id: str, title: str, keys: list[str], js: str, css: str,
              sizeX: int, sizeY: int) -> dict:
    cfg = {
        "datasources": _ds_latest(alias_id, [(k, "timeseries") for k in keys]),
        "timewindow": {"realtime": {"timewindowMs": 86400000}},
        "showTitle": True,
        "title": title,
        "dropShadow": True,
        "enableFullscreen": False,
        "settings": {
            "useMarkdownTextFunction": True,
            "applyDefaultMarkdownStyle": False,
            "markdownTextPattern": "",
            "markdownTextFunction": js,
            "markdownCss": css,
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(title=title, fqn="cards.markdown_card", sizeX=sizeX, sizeY=sizeY, config=cfg)


def vitals_panel(alias_id: str, sizeX: int = 8, sizeY: int = 7) -> dict:
    return _md_panel(alias_id, "生理指標 Vitals",
                     ["hr_max", "hr_above_baseline", "situation_confidence",
                      "post_impact_stillness"],
                     _VITALS_JS, _VITALS_CSS, sizeX, sizeY)


def status_strip(alias_id: str, sizeX: int = 16, sizeY: int = 3) -> dict:
    return _md_panel(alias_id, "救援與狀態 Rescue & status",
                     ["rescue_state", "active", "guardian_ack", "last_recovery_at"],
                     _STATUS_JS, _STATUS_CSS, sizeX, sizeY)


# ---- Resident profile card (TB Assisted-Living style: avatar + resident data) ----
# One clean light card = the dashboard centrepiece. No "call" button here — the
# wearer should never be phoned to be told to do something; dispatch lives on the
# guardian rows instead. This card is a calm identity panel: live status + named
# location + device state. (Age / chronic history / emergency contact rows were
# dropped on request — they were demo-only filler.)
_PROFILE_JS = (
    "var d = data[0] || {};"
    "var nm = (d['entityName']||'Wearer_?').replace('Wearer_','');"
    "var rn = d['resident_name'] || nm;"
    "var bpm = d['bpm']!=null?Math.round(d['bpm']):(d['hr_max']!=null?Math.round(d['hr_max']):'--');"
    "var sit = d['last_situation']||'NORMAL';"
    "var esc = d['last_escalation']||'NONE';"
    "var zone = d['zone']||'—';"
    "var batt = (d['battery']!=null)?Math.round(d['battery']):'--';"
    "var chg = (d['charging']===true||d['charging']==='true');"
    "var wv = d['worn'];"
    # worn=false really means "no recent HR sample" (skin-contact proxy), so say
    # that instead of accusing the resident of taking the watch off.
    "var wornTxt = (wv===true||wv==='true')?'✅ 配戴中 On-wrist':((wv===false||wv==='false')?'⚠️ 未同步心率 No HR sync':'—');"
    "var act = (d['active']===true||d['active']==='true');"
    "var color='#16a34a', sLabel='正常', sFull='狀態正常 Normal', risk='低', riskC='#16a34a';"
    "if(esc==='SOS_COLLAPSE'){color='#dc2626';sLabel='倒地';sFull='倒地 Collapse';risk='危急';riskC='#dc2626';}"
    "else if(esc==='ASK_OK'){color='#f59e0b';sLabel='疑似跌倒';sFull='疑似跌倒 Fall?';risk='中';riskC='#f59e0b';}"
    "var ini = (d['resident_name']? rn.charAt(0) : nm.replace('W-','').slice(-2));"
    "var battIcon = (batt!=='--' && batt<=20)?'🪫':'🔋';"
    "var la = d['last_seen'] || d['lastActivityTime'];"
    "function ago(t){var s=Math.max(0,Math.round((Date.now()-Number(t))/1000));"
    " if(s<60) return s+' 秒前'; if(s<3600) return Math.round(s/60)+' 分鐘前';"
    " if(s<86400) return Math.round(s/3600)+' 小時前'; return Math.round(s/86400)+' 天前';}"
    "var sync = (la!=null)?ago(la):(act?'連線中 Online':'離線 Offline');"
    "function m(l,v,u,c){return '<div class=\"m\"><div class=\"ml\">'+l+'</div><div class=\"mv\" style=\"color:'+(c||'#0f172a')+'\">'+v+'<small>'+(u||'')+'</small></div></div>';}"
    "function r(l,v){return '<div class=\"dr\"><span class=\"dl\">'+l+'</span><span class=\"dv\">'+v+'</span></div>';}"
    "return '<div class=\"pc\">'"
    "+ '<div class=\"hd\"><div class=\"av\" style=\"border-color:'+color+';box-shadow:0 0 0 5px '+color+'1f;\"><span>'+ini+'</span><i class=\"dot\" style=\"background:'+(act?'#16a34a':'#94a3b8')+'\"></i></div></div>'"
    "+ '<div class=\"nm\">'+rn+'</div>'"
    "+ '<div class=\"sb\">配戴者 The resident · '+(act?'在線 Online':'離線 Offline')+'</div>'"
    "+ '<div class=\"pill\" style=\"background:'+color+'1f;color:'+color+';\"><i class=\"pd\"></i>'+sFull+'</div>'"
    "+ '<div class=\"mg\">'+m('心率 HR',bpm,' bpm',color)+m('情境',sLabel,'',null)+m('風險',risk,'',riskC)+'</div>'"
    "+ '<div class=\"dg\">'"
    "+   r('目前位置 Location','📍 '+zone)"
    "+   r('手錶電量 Battery',battIcon+' '+batt+'%'+(chg?' ⚡':''))"
    "+   r('佩戴狀態 Worn',wornTxt)"
    "+   r('最後同步 Sync',sync)"
    "+ '</div>'"
    "+ '</div>';"
)

_PROFILE_CSS = (
    ".pc{height:100%;box-sizing:border-box;container-type:size;padding:18px;font-family:Roboto,'Noto Sans TC',system-ui,sans-serif;"
    "color:#0f172a;display:flex;flex-direction:column;text-align:center;}"
    ".hd{display:flex;justify-content:center;}"
    ".av{position:relative;width:clamp(58px,11.8cqmin,80px);height:clamp(58px,11.8cqmin,80px);border-radius:50%;border:3px solid #16a34a;"
    "background:linear-gradient(135deg,#dbeafe,#bfdbfe);display:flex;align-items:center;justify-content:center;}"
    ".av span{color:#1e40af;font-size:clamp(19px,3.8cqmin,26px);font-weight:800;}"
    ".dot{position:absolute;right:3px;bottom:3px;width:clamp(12px,2.4cqmin,16px);height:clamp(12px,2.4cqmin,16px);border-radius:50%;border:3px solid #fff;}"
    ".nm{font-size:clamp(15px,2.9cqmin,20px);font-weight:700;margin-top:clamp(8px,1.6cqmin,11px);}"
    ".sb{font-size:clamp(9px,1.8cqmin,12px);color:#64748b;margin-top:2px;}"
    ".pill{display:inline-flex;align-items:center;gap:clamp(4px,0.9cqmin,6px);align-self:center;margin-top:clamp(7px,1.3cqmin,9px);font-size:clamp(9px,1.8cqmin,12px);"
    "font-weight:700;padding:4px clamp(10px,2.1cqmin,14px);border-radius:clamp(10px,2.1cqmin,14px);}"
    ".pd{width:clamp(5px,1cqmin,7px);height:clamp(5px,1cqmin,7px);border-radius:50%;background:currentColor;}"
    ".mg{display:grid;grid-template-columns:1fr 1fr 1fr;gap:clamp(7px,1.5cqmin,10px);margin-top:clamp(12px,2.4cqmin,16px);}"
    ".m{background:#f8fafc;border:1px solid #eef2f7;border-radius:clamp(9px,1.8cqmin,12px);padding:clamp(7px,1.5cqmin,10px) clamp(4px,0.9cqmin,6px);}"
    ".ml{font-size:clamp(9px,1.6cqmin,11px);color:#64748b;font-weight:600;}"
    ".mv{font-size:clamp(15px,2.9cqmin,20px);font-weight:800;line-height:1.1;margin-top:3px;}"
    ".mv small{font-size:clamp(9px,1.6cqmin,11px);color:#94a3b8;font-weight:600;margin-left:1px;}"
    ".dg{margin-top:clamp(10px,2.1cqmin,14px);text-align:left;}"
    ".dr{display:flex;justify-content:space-between;border-bottom:1px solid #eef2f7;padding:clamp(5px,1cqmin,7px) 0;font-size:clamp(10px,1.9cqmin,13px);}"
    ".dr:last-child{border-bottom:none;}"
    ".dl{color:#64748b;}.dv{font-weight:600;}"
)


def profile_card(alias_id: str, sizeX: int = 9, sizeY: int = 11) -> dict:
    cfg = {
        "datasources": _ds_latest(alias_id, [
            ("bpm", "timeseries"), ("hr_max", "timeseries"),
            ("hr_above_baseline", "timeseries"), ("situation_confidence", "timeseries"),
            ("last_situation", "timeseries"), ("last_escalation", "timeseries"),
            ("active", "timeseries"), ("zone", "timeseries"),
            # battery + worn + last-sync are REAL device state (telemetry /
            # TB-managed): battery/charging/worn from the watch every 5 s,
            # lastActivityTime from TB.
            ("battery", "timeseries"), ("charging", "timeseries"),
            ("worn", "timeseries"), ("last_seen", "timeseries"),
            ("lastActivityTime", "attribute"),
            # resident display name (set by a care admin; provisioner seeds it)
            ("resident_name", "attribute"),
        ]),
        "timewindow": {"realtime": {"timewindowMs": 86400000}},
        "showTitle": False,
        "title": "Resident profile",
        "dropShadow": True,
        "enableFullscreen": False,
        "settings": {
            "useMarkdownTextFunction": True,
            "applyDefaultMarkdownStyle": False,
            "markdownTextPattern": "",
            "markdownTextFunction": _PROFILE_JS,
            "markdownCss": _PROFILE_CSS,
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(title="Resident profile", fqn="cards.markdown_card",
                   sizeX=sizeX, sizeY=sizeY, config=cfg)


# ---- Today's aggregate stats KPI strip (4 tiles) ----
# Reads pre-aggregated stat_* attributes (the rule chain / a daily job can update
# them; the provisioner seeds demo values). These are *summaries*, deliberately
# different from the live HR on the profile card — so nothing is duplicated.
_STATS_JS = (
    "var d = data[0] || {};"
    "function num(v,dflt){ return (v!=null && !isNaN(v)) ? v : dflt; }"
    "var falls = num(d['stat_falls_today'], 0);"
    "var rec = num(d['stat_avg_recovery_s'], 0);"
    "var self = num(d['stat_self_recovery_pct'], 0);"
    "var actHr = num(d['stat_active_hours'], 0);"
    "function tile(l,v,u,sub,ic,bg,c){"
    " return '<div class=\"k\"><div class=\"kt\"><span class=\"kl\">'+l+'</span>'"
    " + '<span class=\"ki\" style=\"background:'+bg+';color:'+c+'\">'+ic+'</span></div>'"
    " + '<div class=\"kv\">'+v+'<small>'+(u||'')+'</small></div>'"
    " + '<div class=\"ks\">'+sub+'</div></div>'; }"
    "return '<div class=\"kg\">'"
    "+ tile('近24h 跌倒', falls, '', '跌倒/倒地告警數', '⚠', 'rgba(220,38,38,.12)', '#dc2626')"
    "+ tile('平均處理時間', rec, 's', '自跌倒到守護者解除 (clearTs−startTs)', '⏱', 'rgba(22,163,74,.12)', '#16a34a')"
    "+ tile('已解除率', self, '%', '已解除 ÷ 跌倒告警', '↺', 'rgba(37,99,235,.12)', '#2563eb')"
    "+ tile('活動時間', actHr, 'hr', '近12h 正常活動(非跌倒)', '🚶', 'rgba(8,145,178,.12)', '#0891b2')"
    "+ '</div>';"
)

_STATS_CSS = (
    ".kg{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;height:100%;padding:6px 4px;"
    "box-sizing:border-box;font-family:Roboto,'Noto Sans TC',system-ui,sans-serif;container-type:size;}"
    ".k{background:#fff;border:1px solid #eef2f7;border-radius:clamp(9px,7.4cqmin,12px);padding:clamp(9px,7.4cqmin,12px) clamp(12px,9.9cqmin,16px);display:flex;"
    "flex-direction:column;justify-content:center;gap:5px;}"
    ".kt{display:flex;align-items:center;justify-content:space-between;}"
    ".kl{font-size:clamp(9px,7.4cqmin,12px);color:#64748b;font-weight:600;}"
    ".ki{width:clamp(22px,18.5cqmin,30px);height:clamp(22px,18.5cqmin,30px);border-radius:clamp(6px,5.6cqmin,9px);display:flex;align-items:center;justify-content:center;font-size:clamp(10px,8.6cqmin,14px);}"
    ".kv{font-size:clamp(19px,15.4cqmin,25px);font-weight:800;line-height:1;color:#0f172a;}"
    ".kv small{font-size:clamp(9px,7.4cqmin,12px);color:#94a3b8;font-weight:600;margin-left:2px;}"
    ".ks{font-size:clamp(9px,6.8cqmin,11px);color:#94a3b8;}"
)


def stats_strip(alias_id: str, sizeX: int = 24, sizeY: int = 3) -> dict:
    cfg = {
        "datasources": _ds_latest(alias_id, [
            ("stat_falls_today", "attribute"), ("stat_avg_recovery_s", "attribute"),
            ("stat_self_recovery_pct", "attribute"), ("stat_active_hours", "attribute"),
        ]),
        "timewindow": {"realtime": {"timewindowMs": 86400000}},
        "showTitle": False,
        "title": "今日統計 Today",
        "dropShadow": False,
        "enableFullscreen": False,
        "settings": {
            "useMarkdownTextFunction": True,
            "applyDefaultMarkdownStyle": False,
            "markdownTextPattern": "",
            "markdownTextFunction": _STATS_JS,
            "markdownCss": _STATS_CSS,
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(title="今日統計 Today", fqn="cards.markdown_card",
                   sizeX=sizeX, sizeY=sizeY, config=cfg)


# ---- Guardians card: clean on-call roster (name · on-duty · distance) ----
# Display-only (markdown cards can't handle clicks in this CE build), so no dead
# "dispatch" button — the actual paging is automatic via the Notification Center
# rule. Uses the native TB title bar for visual consistency with the other cards.
_GUARDIANS_JS = (
    # __WLAT__/__WLON__ are only the provision-time FALLBACK; the wearer's live
    # lat/lon ride in as a second datasource (rows named Wearer_*) so the
    # distance keeps updating as either side moves.
    "var WLAT=__WLAT__, WLON=__WLON__;"
    "for (var w=0;w<data.length;w++){ var wd=data[w]||{};"
    " if(String(wd['entityName']||'').indexOf('Wearer_')===0 && wd['lat']!=null && wd['lon']!=null){"
    "   WLAT=+wd['lat']; WLON=+wd['lon']; break; } }"
    "function hav(la1,lo1,la2,lo2){var R=6371000,dLa=(la2-la1)*Math.PI/180,dLo=(lo2-lo1)*Math.PI/180,"
    "a=Math.sin(dLa/2)*Math.sin(dLa/2)+Math.cos(la1*Math.PI/180)*Math.cos(la2*Math.PI/180)*Math.sin(dLo/2)*Math.sin(dLo/2);"
    "return 2*R*Math.asin(Math.sqrt(a));}"
    "var pal=['#16a34a','#2563eb','#7c3aed','#0891b2'];"
    "var rows='', g=0;"
    "for (var i=0;i<data.length;i++){"
    " var d=data[i]||{};"
    " var en=String(d['entityName']||'guardian');"
    " if(en.indexOf('Wearer_')===0){ continue; }"
    " var gn=en.replace('Guardian_','');"
    " var act=(d['active']===true||d['active']==='true');"
    " var c=pal[g%pal.length];"
    " var dist='—';"
    " if(d['lat']!=null && d['lon']!=null){ var m=hav(+d['lat'],+d['lon'],WLAT,WLON);"
    "   dist=(m<1000)?(Math.round(m)+' m'):((m/1000).toFixed(1)+' km'); }"
    " var dot=act?'#16a34a':'#94a3b8';"
    " g+=1;"
    " rows += '<div class=\"gr\">'"
    "   + '<span class=\"gd\" style=\"background:'+c+'1f;border:2px solid '+c+';color:'+c+'\">G'+g+'</span>'"
    "   + '<div class=\"gi\"><div class=\"gn\">'+gn+'</div>'"
    "   +   '<div class=\"gm\"><i class=\"gdot\" style=\"background:'+dot+'\"></i>'+(act?'值班中 On duty':'離線')+' · 距配戴者 '+dist+'</div></div>'"
    "   + '</div>';"
    "}"
    "if(!rows){ rows='<div class=\"gm\" style=\"padding:14px 18px\">尚無守護者 GPS 上線</div>'; }"
    "return '<div class=\"gw\"><div class=\"glist\">'+rows+'</div>'"
    "+ '<div class=\"gf\">🔔 告警觸發 → 自動通知派發給值班守護者</div></div>';"
)

_GUARDIANS_CSS = (
    # Responsive: container-type:size makes 1cqmin = 1% of the card's smaller
    # side, so every clamp(min, K*cqmin, max) scales the content down with the
    # card on a 1080p screen instead of overflowing (max = original 2K px).
    ".gw{height:100%;box-sizing:border-box;font-family:Roboto,'Noto Sans TC',system-ui,sans-serif;"
    "color:#0f172a;display:flex;flex-direction:column;container-type:size;}"
    ".glist{flex:1;display:flex;flex-direction:column;justify-content:center;gap:4px;padding:clamp(4px,2.0cqmin,6px) 0;}"
    ".gr{display:flex;align-items:center;gap:clamp(9px,4.0cqmin,12px);padding:clamp(8px,3.7cqmin,11px) clamp(13px,6.0cqmin,18px);}"
    ".gd{width:clamp(27px,12.8cqmin,38px);height:clamp(27px,12.8cqmin,38px);border-radius:50%;display:flex;align-items:center;justify-content:center;"
    "font-weight:700;font-size:clamp(11px,4.4cqmin,13px);flex:0 0 auto;}"
    ".gi{flex:1;min-width:0;}"
    ".gn{font-size:clamp(11px,5.0cqmin,15px);font-weight:600;}"
    ".gm{font-size:clamp(9px,4.0cqmin,12px);color:#64748b;display:flex;align-items:center;gap:clamp(4px,2.0cqmin,6px);margin-top:2px;}"
    ".gdot{width:clamp(6px,2.7cqmin,8px);height:clamp(6px,2.7cqmin,8px);border-radius:50%;display:inline-block;flex:0 0 auto;}"
    ".gf{padding:clamp(8px,3.7cqmin,11px) clamp(13px,6.0cqmin,18px);border-top:1px solid #eef2f7;font-size:clamp(9px,3.7cqmin,11px);color:#2563eb;}"
)


def guardians_card(guardians_alias_id: str, wearers_alias_id: str,
                   wearer_lat: float, wearer_lon: float,
                   sizeX: int = 8, sizeY: int = 6) -> dict:
    js = _GUARDIANS_JS.replace("__WLAT__", repr(wearer_lat)).replace("__WLON__", repr(wearer_lon))
    cfg = {
        # Guardians roster + the wearer's live position (distance reference).
        "datasources": _ds_latest(guardians_alias_id, [
            ("lat", "timeseries"), ("lon", "timeseries"), ("active", "timeseries"),
        ]) + _ds_latest(wearers_alias_id, [
            ("lat", "timeseries"), ("lon", "timeseries"),
        ]),
        "timewindow": {"realtime": {"timewindowMs": 86400000}},
        "showTitle": True,
        "title": "守護者 Guardians",
        "dropShadow": True,
        "enableFullscreen": False,
        "titleStyle": {"fontSize": "16px", "fontWeight": 400, "padding": "5px 10px 5px 10px"},
        "settings": {
            "useMarkdownTextFunction": True,
            "applyDefaultMarkdownStyle": False,
            "markdownTextPattern": "",
            "markdownTextFunction": js,
            "markdownCss": _GUARDIANS_CSS,
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(title="守護者 Guardians", fqn="cards.markdown_card",
                   sizeX=sizeX, sizeY=sizeY, config=cfg)


# ---- Functional dispatch: native two-segment button (custom action) ----
# markdown can't click, but the native button widgets CAN — they share the
# `actionWidget` base that already fires updateDashboardState (the tabs prove it),
# and that base also handles `custom` actions. So each segment writes
# guardian_cmd=dispatch to the WEARER's shared scope (the watch polls it). Single
# wearer → embed the wearer device id in the action JS.
def _dispatch_js(wearer_device_id: str, guardian: str) -> str:
    return (
        "widgetContext.attributeService.saveEntityAttributes("
        f"{{entityType:'DEVICE', id:'{wearer_device_id}'}}, 'SHARED_SCOPE', ["
        "{key:'guardian_cmd', value:'dispatch'},"
        f"{{key:'guardian_cmd_by', value:'{guardian}'}},"
        "{key:'guardian_cmd_ts', value: Date.now()}"
        f"]).subscribe(function(){{ widgetContext.showSuccessToast('已指派 {guardian} 前往配戴者'); }});"
    )


def dispatch_segment(wearer_device_id: str, g_left: str, g_right: str,
                     sizeX: int = 8, sizeY: int = 2) -> dict:
    """Two-segment 'dispatch guardian' button — each segment writes the dispatch
    command for that guardian to the wearer's shared scope (native click)."""
    settings = {
        "initialState": {
            "action": "DO_NOTHING", "defaultValue": True,
            "getAttribute": {"key": "state", "scope": None},
            "getTimeSeries": {"key": "state"},
            "getAlarmStatus": {"severityList": None, "typeList": None},
            "dataToValue": {"type": "NONE", "compareToValue": True,
                            "dataToValueFunction": "return data;"},
            "executeRpc": {"method": None, "requestTimeout": None,
                           "requestPersistent": None, "persistentPollingInterval": None},
        },
        "leftButtonClick": {"type": "custom", "customFunction": _dispatch_js(wearer_device_id, g_left),
                            "openRightLayout": False, "setEntityId": False, "stateEntityParamName": None},
        "rightButtonClick": {"type": "custom", "customFunction": _dispatch_js(wearer_device_id, g_right),
                             "openRightLayout": False, "setEntityId": False, "stateEntityParamName": None},
        "disabledState": {
            "action": "DO_NOTHING", "defaultValue": False,
            "getAttribute": {"key": "state", "scope": None},
            "getTimeSeries": {"key": "state"},
            "getAlarmStatus": {"severityList": None, "typeList": None},
            "dataToValue": {"type": "NONE", "compareToValue": True,
                            "dataToValueFunction": "return data;"},
        },
        "appearance": {
            "layout": "squared", "autoScale": False,
            "cardBorder": 1, "cardBorderColor": "#bfdbfe",
            # Short, no-latin labels so the fixed-size native button text never
            # clips on a 1080p screen (the command/toast still use the full id).
            "leftAppearance": _seg_appearance(g_left.replace("guardian", "守護者"), "directions_run"),
            "rightAppearance": _seg_appearance(g_right.replace("guardian", "守護者"), "directions_run"),
            "selectedStyle": {"mainColor": "#FFFFFF", "backgroundColor": "#2563eb",
                              "customStyle": {"enabled": None, "hovered": None, "disabled": None}},
            "unselectedStyle": {"mainColor": "#2563eb", "backgroundColor": "#eff6ff",
                                "customStyle": {"enabled": None, "hovered": None, "disabled": None}},
        },
    }
    cfg = {
        "targetDeviceAliases": [],
        "showTitle": True,
        "title": "指派守護者前往 Dispatch",
        "backgroundColor": "#FFFFFF01",
        "color": "rgba(0, 0, 0, 0.87)",
        "padding": "0px",
        "titleStyle": {"fontSize": "16px", "fontWeight": 400, "padding": "5px 10px 5px 10px"},
        "settings": settings,
        "dropShadow": True,
        "enableFullscreen": False,
        "widgetStyle": {},
        "actions": {},
        "borderRadius": "10px",
        "configMode": "advanced",
    }
    return _widget(title="指派守護者前往 Dispatch", fqn="two_segment_button",
                   sizeX=sizeX, sizeY=sizeY, config=cfg)


# ---- Dispatch status (display-only) — shows the result of a dispatch click ----
_DISPATCH_STATUS_JS = (
    "var d = data[0] || {};"
    "var by = d['guardian_cmd_by'];"
    "var ts = d['guardian_cmd_ts'];"
    "var rs = d['rescue_state'];"
    "var line, color, ic;"
    "if (rs==='guardian_enroute'){ color='#16a34a'; ic='🏃'; line='守護者前往中 En route'; }"
    "else if (by){ color='#2563eb'; ic='🚑'; line='已指派 '+by; }"
    "else { color='#94a3b8'; ic='🕓'; line='目前無派遣 No active dispatch'; }"
    "var ago='';"
    "if (ts){ var s=Math.max(0,Math.round((Date.now()-Number(ts))/1000));"
    "  ago = s<60?(s+' 秒前'):(Math.round(s/60)+' 分鐘前'); }"
    "return '<div class=\"dsx\"><span class=\"dic\" style=\"background:'+color+'1f;color:'+color+'\">'+ic+'</span>'"
    "+ '<div class=\"dtx\"><div class=\"dl\">派遣狀態 Dispatch status</div>'"
    "+ '<div class=\"dv\" style=\"color:'+color+'\">'+line+(ago?(' · '+ago):'')+'</div></div></div>';"
)

_DISPATCH_STATUS_CSS = (
    ".dsx{display:flex;align-items:center;gap:12px;height:100%;box-sizing:border-box;padding:10px 16px;"
    "font-family:Roboto,'Noto Sans TC',system-ui,sans-serif;container-type:size;}"
    ".dic{width:clamp(27px,23.5cqmin,38px);height:clamp(27px,23.5cqmin,38px);border-radius:clamp(7px,6.2cqmin,10px);display:flex;align-items:center;justify-content:center;"
    "font-size:clamp(13px,10.5cqmin,17px);flex:0 0 auto;}"
    ".dtx{min-width:0;}"
    ".dl{font-size:clamp(9px,6.8cqmin,11px);color:#64748b;font-weight:600;}"
    ".dv{font-size:clamp(11px,9.3cqmin,15px);font-weight:700;margin-top:2px;}"
)


def dispatch_status(alias_id: str, sizeX: int = 8, sizeY: int = 2) -> dict:
    cfg = {
        "datasources": _ds_latest(alias_id, [
            ("guardian_cmd_by", "attribute"), ("guardian_cmd_ts", "attribute"),
            ("rescue_state", "timeseries"),
        ]),
        "timewindow": {"realtime": {"timewindowMs": 86400000}},
        "showTitle": False,
        "title": "派遣狀態 Dispatch status",
        "dropShadow": True,
        "enableFullscreen": False,
        "settings": {
            "useMarkdownTextFunction": True,
            "applyDefaultMarkdownStyle": False,
            "markdownTextPattern": "",
            "markdownTextFunction": _DISPATCH_STATUS_JS,
            "markdownCss": _DISPATCH_STATUS_CSS,
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(title="派遣狀態 Dispatch status", fqn="cards.markdown_card",
                   sizeX=sizeX, sizeY=sizeY, config=cfg)


# ---- Activity timeline as a STATE CHART (not a number line) ----
# A care worker can't read a line bouncing between 0 and 1. The native state
# chart (charts.state_chart) draws a stepped/filled band over time, and the
# y-axis ticksFormatter relabels the codes as 正常/跌倒.
_SIT_TICKS = (
    "if (value === 0) return '正常';"
    "if (value === 1) return '跌倒';"
    "return '';"
)


def state_timeline(alias_id: str, title: str, key: str,
                   timewindow_ms: int = 43200000, sizeX: int = 12, sizeY: int = 5) -> dict:
    cfg = {
        "datasources": [{
            "type": "entity", "name": None, "entityAliasId": alias_id, "filterId": None,
            "dataKeys": [{
                "name": key, "type": "timeseries", "label": "情境 Situation",
                "color": "#0891b2",
                "settings": {"showLines": True, "fillLines": True, "fillLinesOpacity": 0.22,
                             "showPoints": False, "lineWidth": 2, "steppedLine": True},
                "_hash": _new_hash(), "funcBody": None,
            }],
        }],
        "timewindow": {
            "realtime": {"timewindowMs": timewindow_ms},
            "aggregation": {"type": "NONE", "limit": 25000},
        },
        "showTitle": True,
        "title": title,
        "dropShadow": True,
        "enableFullscreen": True,
        "useDashboardTimewindow": False,
        "displayTimewindow": True,
        "settings": {
            "stack": False,
            "fontSize": 11,
            "fontColor": "#545454",
            "showTooltip": True,
            "tooltipIndividual": False,
            "smoothLines": False,
            "shadowSize": 4,
            "showLegend": False,
            "grid": {"verticalLines": True, "horizontalLines": True, "outlineWidth": 1,
                     "color": "#545454", "backgroundColor": None, "tickColor": "#DDDDDD"},
            "xaxis": {"title": None, "showLabels": True, "color": "#545454"},
            "yaxis": {"min": -0.3, "max": 1.3, "title": None, "showLabels": True,
                      "color": "#545454", "tickSize": 1, "tickDecimals": 0,
                      "ticksFormatter": _SIT_TICKS},
            "tooltipValueFormatter": _SIT_TICKS,
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(title=title, fqn="charts.state_chart", sizeX=sizeX, sizeY=sizeY,
                   config=cfg, type_="timeseries")


_CALL_JS = (
    "var d = data[0] || {};"
    "var name = (d['entityName'] || '').replace('Wearer_','');"
    "return '<div class=\"gvcall\">📞 呼叫 '+name+' 手錶</div>';"
)

_CALL_CSS = (
    ".gvcall{display:flex;align-items:center;justify-content:center;height:100%;cursor:pointer;"
    "background:#1a2540;color:#38d3ff;font-weight:700;font-size:14px;border-radius:8px;"
    "border:1px solid #38d3ff;font-family:Roboto,system-ui,sans-serif;}"
)

# Custom action (one-click): write a 'call' command to the wearer's shared scope.
# The watch polls guardian_cmd and rings on 'call' (see wear_os_app _pollGuardianCommand).
_CALL_ACTION_JS = (
    "if (entityId) {"
    " widgetContext.attributeService.saveEntityAttributes(entityId, 'SHARED_SCOPE', ["
    "{key:'guardian_cmd', value:'call'},"
    "{key:'guardian_cmd_by', value:'dashboard'},"
    "{key:'guardian_cmd_ts', value: Date.now()}"
    "]).subscribe(function(){"
    " widgetContext.showSuccessToast('已呼叫 ' + (entityName||'').replace('Wearer_','') + ' 的手錶');"
    " });"
    "}"
)


def _single_entity_alias(alias_id: str, device_id: str, alias_name: str) -> dict:
    return {
        "id": alias_id,
        "alias": alias_name,
        "filter": {
            "type": "singleEntity",
            "singleEntity": {"entityType": "DEVICE", "id": device_id},
        },
    }


def avatar_card(alias_id: str, title: str, sizeX: int = 8, sizeY: int = 5) -> dict:
    cfg = {
        "datasources": _ds_latest(alias_id, [
            ("bpm", "timeseries"), ("hr_max", "timeseries"),
            ("hr_above_baseline", "timeseries"), ("last_situation", "timeseries"),
            ("last_escalation", "timeseries"), ("last_hr_max", "timeseries"),
            ("active", "timeseries"),
        ]),
        "timewindow": {"realtime": {"timewindowMs": 86400000}},
        "showTitle": False,
        "title": title,
        "dropShadow": True,
        "enableFullscreen": False,
        "settings": {
            "useMarkdownTextFunction": True,
            "applyDefaultMarkdownStyle": False,
            "markdownTextPattern": "",
            "markdownTextFunction": _AVATAR_JS,
            "markdownCss": _AVATAR_CSS,
        },
        "actions": {},
        "widgetStyle": {},
    }
    return _widget(title=title, fqn="cards.markdown_card", sizeX=sizeX, sizeY=sizeY, config=cfg)


def call_card(alias_id: str, wearer_name: str, sizeX: int = 8, sizeY: int = 2) -> dict:
    cfg = {
        "datasources": _ds_latest(alias_id, [("active", "timeseries")]),
        "timewindow": {"realtime": {"timewindowMs": 86400000}},
        "showTitle": False,
        "title": f"Call {wearer_name}",
        "dropShadow": False,
        "enableFullscreen": False,
        "settings": {
            "useMarkdownTextFunction": True,
            "applyDefaultMarkdownStyle": False,
            "markdownTextPattern": "",
            "markdownTextFunction": _CALL_JS,
            "markdownCss": _CALL_CSS,
        },
        "actions": {
            "elementClick": [{
                "id": str(uuid.uuid4()),
                "name": "呼叫手錶 Call watch",
                "icon": "call",
                "type": "custom",
                "customFunction": _CALL_ACTION_JS,
            }],
        },
        "widgetStyle": {},
    }
    return _widget(title=f"Call {wearer_name}", fqn="cards.markdown_card", sizeX=sizeX, sizeY=sizeY, config=cfg)


# ---- Two-page tabs (replaces the old top header) ----
# IMPORTANT: cards.markdown_card / cards.html_card declare NO actionSources, so
# any elementClick action on them is silently ignored — they're display-only.
# The native `buttons.two_segment_button` (fqn system.two_segment_button) DOES
# handle clicks internally and its left/right segments default to the built-in
# `updateDashboardState` action — exactly a two-tab switch. One widget per page,
# with the active segment preselected via initialState.defaultValue.
def _seg_appearance(label: str, icon: str) -> dict:
    return {
        "showLabel": True,
        "label": label,
        "labelFont": {"family": "Roboto", "weight": "600", "style": "normal",
                      "size": 14, "sizeUnit": "px", "lineHeight": "16px"},
        "showIcon": True, "icon": icon, "iconSize": 16, "iconSizeUnit": "px",
    }


def _seg_click(target_state: str) -> dict:
    return {
        "type": "updateDashboardState",
        "targetDashboardStateId": target_state,
        "openRightLayout": False,
        "setEntityId": False,
        "stateEntityParamName": None,
    }


def segment_tabs(active: str, sizeX: int = 8, sizeY: int = 2) -> dict:
    """Two-segment toggle = the page tabs. left→Overview state, right→Trends."""
    left_selected = active == "default"
    settings = {
        "initialState": {
            "action": "DO_NOTHING",
            "defaultValue": left_selected,   # true selects the LEFT segment
            "getAttribute": {"key": "state", "scope": None},
            "getTimeSeries": {"key": "state"},
            "getAlarmStatus": {"severityList": None, "typeList": None},
            "dataToValue": {"type": "NONE", "compareToValue": True,
                            "dataToValueFunction": "return data;"},
            "executeRpc": {"method": None, "requestTimeout": None,
                           "requestPersistent": None, "persistentPollingInterval": None},
        },
        "leftButtonClick": _seg_click("default"),
        "rightButtonClick": _seg_click("history"),
        "disabledState": {
            "action": "DO_NOTHING", "defaultValue": False,
            "getAttribute": {"key": "state", "scope": None},
            "getTimeSeries": {"key": "state"},
            "getAlarmStatus": {"severityList": None, "typeList": None},
            "dataToValue": {"type": "NONE", "compareToValue": True,
                            "dataToValueFunction": "return data;"},
        },
        "appearance": {
            # autoScale=False keeps the label/icon at their real size — otherwise
            # TB scales them to fill the widget and the tab text becomes huge.
            "layout": "squared", "autoScale": False,
            "cardBorder": 1, "cardBorderColor": "#e2e8f0",
            "leftAppearance": _seg_appearance("總覽", "dashboard"),
            "rightAppearance": _seg_appearance("趨勢與告警", "show_chart"),
            "selectedStyle": {"mainColor": "#FFFFFF", "backgroundColor": "#2563eb",
                              "customStyle": {"enabled": None, "hovered": None, "disabled": None}},
            "unselectedStyle": {"mainColor": "#475569", "backgroundColor": "#FFFFFF",
                                "customStyle": {"enabled": None, "hovered": None, "disabled": None}},
        },
    }
    cfg = {
        "targetDeviceAliases": [],
        "showTitle": False,
        "backgroundColor": "#FFFFFF01",
        "color": "rgba(0, 0, 0, 0.87)",
        "padding": "0px",
        "settings": settings,
        "title": "Tabs",
        "dropShadow": False,
        "enableFullscreen": False,
        "widgetStyle": {},
        "actions": {},
        "borderRadius": "10px",
        "configMode": "advanced",
    }
    return _widget(title="Tabs", fqn="two_segment_button", sizeX=sizeX, sizeY=sizeY, config=cfg)


# ---- Dashboard assembly ----


def build_dashboard(dashboard_name: str, wearers: list[dict] | None = None) -> dict:
    """v3 redesign: single-page, wearer-centric live ops.

    `wearers`: optional list of {"wearer_id","device_id","name"} used to build a
    per-wearer "care console" row — an avatar digital-twin card (live HR +
    status colour) + a one-click "call the watch" button each.

    Two roles, not one confusing 'guardian device + workers' mix:
      - Wearers (type=wearer): the protected people; their devices carry
        HR / situation / GPS and raise the alarms. All widgets read from a
        single multi-entity alias over device type 'wearer'.
      - Guardians: phone users (customer users) who open this dashboard /
        their own control-centre app — they are viewers, not telemetry devices.

    Uses only widget types validated against TB 4.3 working dashboards:
    cards.html_card, cards.entities_table, charts.basic_timeseries,
    alarm_widgets.alarms_table, maps_v2.openstreetmap.
    """
    wearers = wearers or []
    workers_alias_id = str(uuid.uuid4())
    guardians_alias_id = str(uuid.uuid4())
    widgets: dict[str, dict] = {}
    layouts: dict[str, dict[str, dict]] = {"default": {}, "history": {}}
    wearer_aliases: dict[str, dict] = {}  # per-wearer single-entity aliases

    def add(w: dict, *, col: int, row: int, state: str = "default") -> None:
        wid = str(uuid.uuid4())
        w["id"] = wid
        widgets[wid] = w
        lay = layouts[state]
        lay[wid] = {
            "sizeX": w["sizeX"], "sizeY": w["sizeY"],
            "row": row, "col": col,
            "mobileOrder": len(lay) + 1, "mobileHeight": w["sizeY"],
        }

    # Two pages (dashboard states) switched by a slim tab bar — replaces the old
    # top header and avoids one long scroll. Each page is ~one screen tall.
    if wearers:
        primary = wearers[0]
        p_alias = str(uuid.uuid4())
        wearer_aliases[p_alias] = _single_entity_alias(
            p_alias, primary["device_id"], primary["name"])

        # ===== Page 1 「總覽 Overview」: where + who/now + today =====
        add(segment_tabs("default", sizeX=8, sizeY=1), col=0, row=0, state="default")
        add(workers_map(workers_alias_id, "即時位置 Live location · 陽明交大 光復校區",
                        sizeX=15, sizeY=8,
                        guardians_alias_id=guardians_alias_id, center=NYCU_CENTER),
            col=0, row=1, state="default")
        add(profile_card(p_alias, sizeX=9, sizeY=8), col=15, row=1, state="default")
        add(stats_strip(p_alias, sizeX=24, sizeY=2), col=0, row=9, state="default")

        # ===== Page 2 「趨勢與告警 Trends & alarms」: charts + history + roster =====
        add(segment_tabs("history", sizeX=8, sizeY=1), col=0, row=0, state="history")
        add(time_series(p_alias, "心率趨勢 vs 個人基線 HR vs baseline",
                        ["bpm", "hr_baseline"], timewindow_ms=43200000,
                        sizeX=12, sizeY=5), col=0, row=1, state="history")
        add(state_timeline(p_alias, "情境時間軸 Activity timeline", "situation_code",
                           timewindow_ms=43200000, sizeX=12, sizeY=5),
            col=12, row=1, state="history")
        # Right column stacks 守護者(4) + 指派(2) + 派遣狀態(2) = 8 rows, so the
        # alarms table on the left is 8 rows tall too — both columns end at row 13.
        add(alarms_table(workers_alias_id, "告警歷史 Alarm history",
                         sizeX=16, sizeY=8, status_list=[]),
            col=0, row=6, state="history")
        add(guardians_card(guardians_alias_id, workers_alias_id,
                           24.78686, 120.99681, sizeX=8, sizeY=4),
            col=16, row=6, state="history")
        add(dispatch_segment(primary["device_id"], "guardian1", "guardian2",
                             sizeX=8, sizeY=2), col=16, row=10, state="history")
        add(dispatch_status(p_alias, sizeX=8, sizeY=2), col=16, row=12, state="history")
    else:
        add(workers_map(workers_alias_id, "即時位置 Live location · 陽明交大", sizeX=15, sizeY=9,
                        guardians_alias_id=guardians_alias_id, center=NYCU_CENTER),
            col=0, row=0)
        add(alarms_table(workers_alias_id, "告警歷史 Alarm history",
                         sizeX=9, sizeY=9, status_list=[]), col=15, row=0)

    def _grid() -> dict:
        return {
            "backgroundColor": "#eef2f6",
            "color": "rgba(15,23,42,0.87)",
            "columns": 24,
            "margin": 10,
            "outerMargin": True,
            "backgroundSizeMode": "100%",
            "autoFillHeight": False,
            "mobileAutoFillHeight": False,
            "mobileRowHeight": 70,
            "layoutType": "default",
        }

    states = {
        "default": {
            "name": "總覽 Overview",
            "root": True,
            "layouts": {"main": {"widgets": layouts["default"], "gridSettings": _grid()}},
        },
    }
    if wearers:
        states["history"] = {
            "name": "趨勢與告警 Trends & alarms",
            "root": False,
            "layouts": {"main": {"widgets": layouts["history"], "gridSettings": _grid()}},
        }

    entity_aliases = {
        guardians_alias_id: {
            "id": guardians_alias_id,
            "alias": "Guardians",
            "filter": {
                "type": "deviceType",
                "resolveMultiple": True,
                "deviceNameFilter": "",
                "deviceTypes": ["guardian"],
            },
        },
        workers_alias_id: {
            "id": workers_alias_id,
            "alias": "Wearers",
            "filter": {
                "type": "deviceType",
                "resolveMultiple": True,
                "deviceNameFilter": "",
                "deviceTypes": ["wearer"],
            },
        },
    }
    entity_aliases.update(wearer_aliases)  # per-wearer single-entity (avatar/call cards)

    return {
        "name": dashboard_name,
        "title": dashboard_name,
        "configuration": {
            "description": (
                "Wearable Guardian v3.1 — wearer-centric live ops dashboard "
                "(配戴者 telemetry + alarms; 守護者 = customer users). "
                "Auto-provisioned by scripts/provision_thingsboard.py."
            ),
            "widgets": widgets,
            "states": states,
            "entityAliases": entity_aliases,
            "filters": {},
            "timewindow": {
                "displayValue": "",
                "hideInterval": False,
                "hideAggregation": False,
                "hideAggInterval": False,
                "hideTimezone": False,
                "selectedTab": 0,
                "realtime": {
                    "realtimeType": "LAST_INTERVAL",
                    "interval": 1000,
                    "timewindowMs": 86400000,
                },
                "aggregation": {"type": "NONE", "limit": 25000},
            },
            "settings": {
                "stateControllerId": "entity",
                "showTitle": False,
                "showDashboardsSelect": True,
                "showEntitiesSelect": True,
                "showDashboardTimewindow": True,
                "showDashboardExport": True,
                "toolbarAlwaysOpen": True,
            },
        },
    }


def provision_dashboard(client: TbClient, dashboard_name: str,
                        wearers: list[dict] | None = None) -> str:
    """Create or overwrite the named dashboard. Returns dashboard ID."""
    found = client.get(f"/api/tenant/dashboards?pageSize=100&page=0").get("data", [])
    existing = next((d for d in found if d.get("title") == dashboard_name), None)

    body = build_dashboard(dashboard_name, wearers)
    if existing is not None:
        body["id"] = existing["id"]
        print(f"  updating existing dashboard '{dashboard_name}'")
    else:
        print(f"  creating dashboard '{dashboard_name}'")

    saved = client.post("/api/dashboard", body)
    return saved["id"]["id"]


# ---------- Notification Center (care paging) ----------
# When a wearer raises an alarm, page the on-call guardians via TB's native
# Notification Center (WEB bell; Email/Teams can be enabled in TB settings).
# Recipients = ORIGINATOR_ENTITY_OWNER_USERS: since wearer devices are owned by
# the Guardian Ops customer, an alarm on a wearer resolves to that customer's
# users — i.e. the guardians — with no per-customer wiring.

NOTIF_TARGET_NAME = "On-call Guardians 護理值班"
NOTIF_TEMPLATE_NAME = "Wearer alarm → guardian page"
NOTIF_RULE_NAME = "Page guardians on wearer alarm"


def _find_by_name(client: TbClient, path: str, name: str):
    data = client.get(f"{path}?pageSize=200&page=0").get("data", [])
    return next((x for x in data if x.get("name") == name), None)


def ensure_notification_target(client: TbClient) -> str:
    existing = _find_by_name(client, "/api/notification/targets", NOTIF_TARGET_NAME)
    if existing:
        return existing["id"]["id"]
    body = {
        "name": NOTIF_TARGET_NAME,
        "configuration": {
            "type": "PLATFORM_USERS",
            "description": "Guardians (customer users who own the alarming wearer)",
            "usersFilter": {"type": "ORIGINATOR_ENTITY_OWNER_USERS"},
        },
    }
    return client.post("/api/notification/target", body)["id"]["id"]


def ensure_notification_template(client: TbClient) -> str:
    existing = _find_by_name(client, "/api/notification/templates", NOTIF_TEMPLATE_NAME)
    if existing:
        return existing["id"]["id"]
    body = {
        "name": NOTIF_TEMPLATE_NAME,
        "notificationType": "ALARM",
        "configuration": {
            "deliveryMethodsTemplates": {
                "WEB": {
                    "method": "WEB",
                    "enabled": True,
                    "subject": "🚑 ${alarmType} · ${alarmSeverity}",
                    "body": "配戴者 ${alarmOriginatorName} 觸發 ${alarmType}"
                            "（${alarmSeverity}）— 請立即查看並前往。",
                    "additionalConfig": {
                        "icon": {"enabled": True, "icon": "emergency", "color": "#d32f2f"},
                        "actionButtonConfig": {"enabled": False},
                    },
                },
            },
        },
    }
    return client.post("/api/notification/template", body)["id"]["id"]


def ensure_notification_rule(client: TbClient, template_id: str, target_id: str) -> str:
    existing = _find_by_name(client, "/api/notification/rules", NOTIF_RULE_NAME)
    if existing:
        return existing["id"]["id"]
    body = {
        "name": NOTIF_RULE_NAME,
        "enabled": True,
        "templateId": {"entityType": "NOTIFICATION_TEMPLATE", "id": template_id},
        "triggerType": "ALARM",
        "triggerConfig": {
            "triggerType": "ALARM",
            "alarmTypes": ["MedicalCollapse", "FallSuspected"],
            "alarmSeverities": None,   # any severity for our 2 alarm types
            "notifyOn": ["CREATED"],
            "clearRule": None,
        },
        "recipientsConfig": {
            "triggerType": "ALARM",
            "escalationTable": {"0": [target_id]},
        },
        "additionalConfig": {
            "description": "Page on-call guardians when a wearer raises an alarm",
        },
    }
    return client.post("/api/notification/rule", body)["id"]["id"]


def provision_notifications(client: TbClient) -> None:
    target_id = ensure_notification_target(client)
    template_id = ensure_notification_template(client)
    ensure_notification_rule(client, template_id, target_id)
    print("  notification rule ready: wearer alarm → guardian WEB page")


# ---------- Main ----------


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="http://localhost:18080")
    p.add_argument("--user", default="tenant@thingsboard.org")
    p.add_argument("--password", default="tenant")
    p.add_argument("--dashboard-name", default="Guardian Live Ops")
    p.add_argument("--purge-old-dashboard", action="store_true",
                   help="Delete the legacy 'Smart Warehouse Posture Ops v2.0' dashboard if present.")
    p.add_argument("--rule-chain-name", default="GuardianRules")
    p.add_argument("--wearers", "--workers", dest="wearers", default="W-001",
                   help="Comma-separated wearer IDs to provision as TB devices (type=wearer). "
                        "Any other Wearer_* device is removed.")
    p.add_argument("--worker-tokens-path", default="data/worker_tokens.json")
    p.add_argument("--guardian-tokens-path", default="data/guardian_tokens.json",
                   help="Where to write each guardian's own GPS device token.")
    p.add_argument("--customer-title", default="Guardian Ops",
                   help="Customer that owns the wearer devices; guardian users belong to it.")
    p.add_argument("--guardians", default="guardian1@guardian.local,guardian2@guardian.local",
                   help="Comma-separated guardian (monitor) login emails to create as CUSTOMER_USER.")
    p.add_argument("--guardian-password", default="changeme",
                   help="Password set for every newly-created guardian user. Override this; do not ship the default.")
    p.add_argument("--legacy-guardian-device", default="Guardian_Device_001",
                   help="Old single 'guardian device' to delete (no longer used in the wearer model).")
    p.add_argument("--fcm-relay-url", default="http://host.docker.internal:9090/notify",
                   help="FCM relay endpoint as seen FROM the ThingsBoard container "
                        "(scripts/fcm_relay.py on the docker host).")
    p.add_argument("--fcm-relay-secret", default="",
                   help="Shared secret for the FCM relay (its FCM_RELAY_SECRET). "
                        "Empty (default) = no FCM push node is provisioned.")
    p.add_argument("--skip-rule-chain", action="store_true")
    p.add_argument("--skip-dashboard", action="store_true")
    p.add_argument("--skip-notifications", action="store_true",
                   help="Skip Notification Center setup (care paging on wearer alarms).")
    p.add_argument("--skip-wearers", "--skip-workers", dest="skip_wearers", action="store_true")
    p.add_argument("--skip-customer", action="store_true")
    args = p.parse_args()

    backup_dir = Path(__file__).resolve().parent / "tb_backups"

    print(f"-> login {args.user} @ {args.base}")
    client = TbClient(args.base, args.user, args.password)

    # The legacy 'Guardian_Device_001' conflated 'a person' with 'the inference
    # sink' and showed up on the map like a phantom worker. In the wearer model
    # the inference telemetry + alarms live on each wearer's own device, so the
    # single guardian device is removed.
    if args.legacy_guardian_device.strip():
        try:
            old = client.get(f"/api/tenant/devices?deviceName={args.legacy_guardian_device}")
            client.delete(f"/api/device/{old['id']['id']}")
            print(f"-> deleted legacy guardian device '{args.legacy_guardian_device}'")
        except SystemExit as exc:
            if "404" in str(exc):
                print(f"-> legacy guardian device '{args.legacy_guardian_device}' absent (ok)")
            else:
                raise

    wearer_device_ids: dict[str, str] = {}
    if not args.skip_wearers and args.wearers.strip():
        wearer_ids = [w.strip() for w in args.wearers.split(",") if w.strip()]
        tokens_path = Path(__file__).resolve().parent.parent / args.worker_tokens_path
        print(f"-> provision wearer devices {wearer_ids}")
        _, wearer_device_ids = ensure_wearer_devices(client, wearer_ids, tokens_path)
        # Remove the legacy Worker_<id> devices replaced by Wearer_<id>.
        for wid in wearer_ids:
            try:
                old = client.get(f"/api/tenant/devices?deviceName=Worker_{wid}")
                client.delete(f"/api/device/{old['id']['id']}")
                print(f"  deleted legacy device Worker_{wid}")
            except SystemExit as exc:
                if "404" not in str(exc):
                    raise
        # Prune any extra Wearer_* device not in the requested set (single-wearer
        # demo: drop the leftover Wearer_W-002 / W-003 etc.).
        keep = {f"Wearer_{wid}" for wid in wearer_ids}
        all_devs = client.get("/api/tenant/devices?pageSize=1000&page=0").get("data", [])
        for dev in all_devs:
            nm = dev.get("name", "")
            if nm.startswith("Wearer_") and nm not in keep:
                client.delete(f"/api/device/{dev['id']['id']}")
                print(f"  removed extra wearer device {nm}")
        # Seed resident metadata + demo stats + an initial NORMAL point so the
        # care-console cards (profile / today-stats / map / charts) aren't blank.
        for did in wearer_device_ids.values():
            seed_wearer_profile(client, did)
        if wearer_device_ids:
            print(f"  seeded resident profile + stats on {len(wearer_device_ids)} wearer(s)")

    if not args.skip_rule_chain:
        print(f"-> provision rule chain '{args.rule_chain_name}'")
        provision_rule_chain(
            client,
            args.rule_chain_name,
            backup_dir,
            fcm_relay_url=args.fcm_relay_url,
            fcm_relay_secret=args.fcm_relay_secret,
        )
        if args.fcm_relay_secret:
            print(f"  FCM push relay node wired (alarm Created → {args.fcm_relay_url})")
        else:
            print("  FCM push relay node skipped (no --fcm-relay-secret)")

    dash_id = None
    if not args.skip_dashboard:
        print(f"-> provision dashboard '{args.dashboard_name}'")
        wearers_meta = [
            {"wearer_id": wid, "device_id": did, "name": f"Wearer_{wid}"}
            for wid, did in wearer_device_ids.items()
        ]
        dash_id = provision_dashboard(client, args.dashboard_name, wearers_meta)
        print(f"  dashboard id={dash_id} (+{len(wearers_meta)} avatar/call cards)")

    if not args.skip_notifications:
        print("-> provision care paging (Notification Center)")
        provision_notifications(client)

    # Customer (owns wearers) + guardian (monitor) users that log in from the phone.
    if not args.skip_customer:
        print(f"-> provision customer '{args.customer_title}' + guardian users")
        customer_id = ensure_customer(client, args.customer_title)
        for did in wearer_device_ids.values():
            assign_device_to_customer(client, customer_id, did)
        if wearer_device_ids:
            print(f"  assigned {len(wearer_device_ids)} wearer devices to customer")
        if dash_id is not None:
            assign_dashboard_to_customer(client, customer_id, dash_id)
            print("  assigned dashboard to customer")
        emails = [e.strip() for e in args.guardians.split(",") if e.strip()]
        created = []
        for email in emails:
            new = ensure_customer_user(
                client, customer_id, email, args.guardian_password,
                first_name="Guardian", last_name=email.split("@")[0],
            )
            if new:
                created.append(email)

        # Each guardian also gets a device carrying THEIR OWN GPS (a user can't
        # hold telemetry). Guardian position becomes first-class TB data.
        g_tokens_path = Path(__file__).resolve().parent.parent / args.guardian_tokens_path
        print(f"-> provision guardian GPS devices {[e.split('@')[0] for e in emails]}")
        _, guardian_device_ids = ensure_guardian_devices(client, emails, g_tokens_path)
        for did in guardian_device_ids.values():
            assign_device_to_customer(client, customer_id, did)

        # Seed each guardian's GPS near the campus so both roles appear on the
        # shared live map even without the phone app running (demo). The phone
        # overwrites this with the guardian's real GPS once it logs in.
        campus_pts = [(24.78820, 120.99870), (24.78540, 120.99480), (24.78760, 120.99560)]
        for i, did in enumerate(guardian_device_ids.values()):
            glat, glon = campus_pts[i % len(campus_pts)]
            client.post(
                f"/api/plugins/telemetry/DEVICE/{did}/timeseries/ANY?scope=ANY",
                {"lat": glat, "lon": glon, "active": True, "entity_type": "guardian"},
            )
        print(f"  seeded {len(guardian_device_ids)} guardian GPS points on campus")

        # B3: relate each guardian USER -Manages-> every wearer DEVICE, so alarms
        # can propagate along the relation and the phone can show "my wearers".
        cust_users = client.get(
            f"/api/customer/{customer_id}/users?pageSize=500&page=0"
        ).get("data", [])
        email_to_uid = {u["email"]: u["id"]["id"] for u in cust_users}
        rel_count = 0
        for email in emails:
            uid = email_to_uid.get(email)
            if not uid:
                continue
            for wdid in wearer_device_ids.values():
                try:
                    ensure_relation(client, uid, "USER", wdid, "DEVICE", "Manages")
                    rel_count += 1
                except SystemExit:
                    pass
        print(f"  created {rel_count} guardian→wearer relations")

        print("\n[guardian logins] (phone app → 守護者登入)")
        for email in emails:
            note = "password=" + args.guardian_password if email in created else "(existing — password unchanged)"
            print(f"  {email}  {note}")

    if dash_id is not None:
        print(f"\n[OK] dashboard ready at: {args.base}/dashboards/{dash_id}")
        print(f"     tenant admin: {args.user} / {args.password}")

    if args.purge_old_dashboard:
        legacy = "Smart Warehouse Posture Ops v2.0"
        print(f"-> purging legacy dashboard '{legacy}'")
        found = client.get("/api/tenant/dashboards?pageSize=100&page=0").get("data", [])
        old = next((d for d in found if d.get("title") == legacy), None)
        if old is not None:
            client.delete(f"/api/dashboard/{old['id']['id']}")
            print(f"  deleted legacy dashboard id={old['id']['id']}")
        else:
            print("  (not found, nothing to purge)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
