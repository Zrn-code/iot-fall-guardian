"""FCM push relay for the Wearable Guardian.

ThingsBoard's rule chain has no way to mint the OAuth tokens the FCM HTTP v1
API requires, so alarm pushes hop through this tiny relay instead:

    rule chain "FCM push relay" node (alarm Created only)
        → POST http://host.docker.internal:9090/notify
        → firebase-admin sends an FCM notification message to a topic
        → Android shows a system notification even when the app is closed.

The phone app subscribes to the topic at startup and renders the message on
its high-importance `guardian_alerts` channel (heads-up banner + vibration).

One-time setup:
    .venv\\Scripts\\pip.exe install firebase-admin
    Firebase console → Project settings → Service accounts →
        Generate new private key → save as data/firebase-service-account.json
        (gitignored — never commit it)

Run:
    $env:FCM_RELAY_SECRET="<same secret passed to provision_thingsboard.py>"
    python scripts/fcm_relay.py
        --port 9090
        --topic guardian-alerts
        --sa-key data/firebase-service-account.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, messaging

NOTIFICATION_CHANNEL_ID = "guardian_alerts"  # created by the phone app's MainActivity
DEDUP_CAPACITY = 100


def _log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


class RelayHandler(BaseHTTPRequestHandler):
    # Filled in by main() before the server starts.
    secret: str = ""
    topic: str = ""
    seen_alarm_ids: OrderedDict[str, None] = OrderedDict()

    def _respond(self, code: int, message: str) -> None:
        body = json.dumps({"status": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler naming)
        if self.path == "/health":
            self._respond(200, "ok")
        else:
            self._respond(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/notify":
            self._respond(404, "not found")
            return
        if self.headers.get("X-Relay-Secret") != self.secret:
            self._respond(401, "bad secret")
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            alarm = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._respond(400, "invalid json")
            return

        alarm_type = alarm.get("type", "Alarm")
        severity = alarm.get("severity", "UNKNOWN")
        # The rule chain fills this header from the ${deviceName} metadata
        # pattern — more reliable than originatorName inside the alarm JSON.
        device_name = self.headers.get("X-Device-Name") or alarm.get("originatorName") or "未知配戴者"

        # Created-only wiring already prevents repeats; this LRU is a backstop
        # against rule-chain retries delivering the same alarm twice.
        alarm_id = (alarm.get("id") or {}).get("id")
        if alarm_id:
            if alarm_id in self.seen_alarm_ids:
                _log(f"duplicate alarm {alarm_id} ({alarm_type}) — skipped")
                self._respond(200, "duplicate")
                return
            self.seen_alarm_ids[alarm_id] = None
            while len(self.seen_alarm_ids) > DEDUP_CAPACITY:
                self.seen_alarm_ids.popitem(last=False)

        message = messaging.Message(
            topic=self.topic,
            notification=messaging.Notification(
                title=f"🚑 {alarm_type}（{severity}）",
                body=f"配戴者 {device_name} 觸發 {alarm_type}，請立即查看並前往。",
            ),
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id=NOTIFICATION_CHANNEL_ID,
                    default_sound=True,
                    default_vibrate_timings=True,
                ),
            ),
        )
        try:
            message_id = messaging.send(message)
        except Exception as e:  # noqa: BLE001 — report any FCM failure to TB as 502
            _log(f"FCM send failed for {alarm_type} ({device_name}): {e}")
            self._respond(502, "fcm send failed")
            return
        _log(f"pushed {alarm_type} ({severity}) for {device_name} → {message_id}")
        self._respond(200, "sent")

    def log_message(self, format: str, *args) -> None:  # silence default access log
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=9090)
    parser.add_argument("--topic", default="guardian-alerts")
    parser.add_argument(
        "--sa-key",
        default="data/firebase-service-account.json",
        help="Firebase service-account key JSON (gitignored).",
    )
    parser.add_argument(
        "--secret",
        default=os.environ.get("FCM_RELAY_SECRET", ""),
        help="Shared secret the rule chain sends in X-Relay-Secret (env FCM_RELAY_SECRET).",
    )
    args = parser.parse_args()

    if not args.secret:
        sys.exit("Set FCM_RELAY_SECRET (or pass --secret); refusing to run an unauthenticated relay.")
    sa_path = Path(args.sa_key)
    if not sa_path.is_file():
        sys.exit(
            f"Service-account key not found: {sa_path}\n"
            "Firebase console > Project settings > Service accounts > Generate new private key"
        )

    firebase_admin.initialize_app(credentials.Certificate(str(sa_path)))

    RelayHandler.secret = args.secret
    RelayHandler.topic = args.topic
    # 0.0.0.0 so the ThingsBoard container can reach us via host.docker.internal.
    server = ThreadingHTTPServer(("0.0.0.0", args.port), RelayHandler)
    _log(f"FCM relay listening on 0.0.0.0:{args.port} → topic '{args.topic}'")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("shutting down")


if __name__ == "__main__":
    main()
