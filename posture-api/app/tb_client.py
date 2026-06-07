"""ThingsBoard integration: HTTP push and MQTT RPC bridge."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable

import httpx

logger = logging.getLogger("tb-client")
logger.setLevel(logging.INFO)


class TbConfig:
    def __init__(self) -> None:
        self.api_base = os.environ.get(
            "TB_API_BASE", "http://thingsboard-ce:8080"
        ).rstrip("/")
        self.device_token = os.environ.get("TB_DEVICE_TOKEN", "").strip()
        self.mqtt_host = os.environ.get("TB_MQTT_HOST", "thingsboard-ce")
        self.mqtt_port = int(os.environ.get("TB_MQTT_PORT", "1883"))


def push_telemetry(cfg: TbConfig, payload: dict[str, Any]) -> bool:
    """Push telemetry to TB using the configured device token (HTTP)."""
    if not cfg.device_token:
        return False
    return push_telemetry_token(cfg.api_base, cfg.device_token, payload)


def push_telemetry_token(api_base: str, token: str, payload: dict[str, Any]) -> bool:
    """Push telemetry to TB using an arbitrary device token (HTTP)."""
    if not token:
        return False
    url = f"{api_base.rstrip('/')}/api/v1/{token}/telemetry"
    try:
        resp = httpx.post(url, json=payload, timeout=2.0)
        return resp.status_code < 300
    except httpx.HTTPError as exc:
        logger.warning("TB push failed: %s", exc)
        return False


class WorkerTbBridge:
    """Map worker_id -> TB access token, with a convenient push(worker_id, payload).

    Token map is loaded from a JSON file written by `provision_thingsboard.py`:
        {"W-001": "abc...", "W-002": "def...", ...}
    """

    def __init__(self, api_base: str, tokens: dict[str, str] | None = None) -> None:
        self.api_base = api_base
        self.tokens: dict[str, str] = dict(tokens or {})

    @classmethod
    def from_file(cls, api_base: str, path) -> "WorkerTbBridge":
        try:
            import json as _json
            from pathlib import Path as _Path
            p = _Path(path)
            if not p.exists():
                return cls(api_base, {})
            data = _json.loads(p.read_text(encoding="utf-8"))
            return cls(api_base, data if isinstance(data, dict) else {})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load worker tokens from %s: %s", path, exc)
            return cls(api_base, {})

    def push(self, worker_id: str, payload: dict[str, Any]) -> bool:
        token = self.tokens.get(worker_id)
        if not token:
            return False
        return push_telemetry_token(self.api_base, token, payload)

    def has(self, worker_id: str) -> bool:
        return worker_id in self.tokens

    def count(self) -> int:
        return len(self.tokens)


def start_mqtt_rpc_thread(
    cfg: TbConfig, handlers: dict[str, Callable[[dict], dict]]
) -> threading.Thread | None:
    """Subscribe to TB RPC requests and dispatch by method name."""
    if not cfg.device_token:
        return None
    try:
        import paho.mqtt.client as mqtt  # type: ignore
    except ImportError:
        logger.warning("paho-mqtt not installed; RPC bridge disabled")
        return None

    def _runner() -> None:
        while True:
            try:
                client = mqtt.Client(client_id="posture-api-rpc")
                client.username_pw_set(cfg.device_token)

                def on_connect(c, _u, _f, rc):
                    logger.info("TB MQTT connect rc=%s", rc)
                    if rc == 0:
                        c.subscribe("v1/devices/me/rpc/request/+", qos=1)

                def on_message(c, _u, msg):
                    try:
                        request_id = msg.topic.rsplit("/", 1)[-1]
                        body = json.loads(msg.payload.decode("utf-8"))
                        method = body.get("method", "")
                        params = body.get("params", {}) or {}
                        handler = handlers.get(method)
                        response = (
                            {"error": f"unknown method: {method}"}
                            if handler is None
                            else handler(params)
                        )
                        c.publish(
                            f"v1/devices/me/rpc/response/{request_id}",
                            json.dumps(response),
                            qos=1,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("RPC handler error: %s", exc)

                client.on_connect = on_connect
                client.on_message = on_message
                client.connect(cfg.mqtt_host, cfg.mqtt_port, keepalive=60)
                client.loop_forever()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MQTT loop crashed: %s; retrying in 10s", exc)
                time.sleep(10)

    thread = threading.Thread(target=_runner, name="tb-mqtt-rpc", daemon=True)
    thread.start()
    return thread
