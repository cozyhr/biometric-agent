#!/usr/bin/env python3
"""
CozyHR local biometric sync agent (SaaS edition).

Runs on a machine INSIDE a company's local network. It is the SAME agent for
every CozyHR customer: it identifies the tenant purely by the API key, pulls
that tenant's device list FROM THE SERVER (no hardcoded IPs), reads new punches
from each ZKTeco device, pushes them to CozyHR, and reports a heartbeat so the
tenant can see device/agent status in the app.

Setup (identical for every customer):
    pip install pyzk requests
    export COZY_API_KEY="ch_live_xxx"          # Developers -> API keys (scopes: attendance.read + attendance.write)
    export COZY_BASE_URL="https://cozyhr.com"  # optional, this is the default
    python sync_agent.py                        # polls forever
    python sync_agent.py --once                 # single pass (good for cron)

Each company registers its devices in CozyHR (Time -> Devices) with the device
IP, port, direction (IN/OUT) and a device code. The agent reads that config at
runtime. If the server returns no devices, it falls back to LOCAL_FALLBACK below.
"""

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
    from zk import ZK
except ImportError:
    sys.exit("Missing dependencies. Run: pip install pyzk requests")

AGENT_VERSION = "1.2.0"
BASE_URL = os.environ.get("COZY_BASE_URL", "https://cozyhr.com").rstrip("/")
API_KEY = os.environ.get("COZY_API_KEY", "").strip()
POLL_SECONDS = int(os.environ.get("COZY_POLL_SECONDS", "60"))
AGENT_ID = os.environ.get("COZY_AGENT_ID", socket.gethostname())
STATE_FILE = Path(__file__).resolve().parent / "state.json"

PUNCH_ENDPOINT = f"{BASE_URL}/api/public/v1/attendance/punches"
DEVICES_ENDPOINT = f"{BASE_URL}/api/public/v1/attendance/devices"
EMPLOYEES_ENDPOINT = f"{BASE_URL}/api/public/v1/employees"

# Optional fallback used only if the server returns no devices (e.g. first run
# before devices are registered in the app). Leave empty for pure SaaS use.
LOCAL_FALLBACK: list[dict] = [
    # {"name": "IN Device", "ip": "192.168.29.201", "port": 4370, "direction": "IN", "deviceCode": "IN-201"},
    # {"name": "OUT Device", "ip": "192.168.29.202", "port": 4370, "direction": "OUT", "deviceCode": "OUT-202"},
]


def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (ValueError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as error:
        log(f"WARN could not save state: {error}")


def fetch_devices() -> list[dict]:
    """Pull this tenant's devices from the server. Falls back to LOCAL_FALLBACK."""
    try:
        response = requests.get(DEVICES_ENDPOINT, headers=auth_headers(), timeout=20)
        if response.status_code == 200:
            devices = response.json().get("devices", [])
            usable = [d for d in devices if d.get("ip")]
            if usable:
                return usable
            log("Server returned no devices with an IP set (register IPs in Time -> Devices). Using local fallback.")
        else:
            log(f"Could not fetch devices ({response.status_code}): {response.text[:160]}. Using local fallback.")
    except requests.RequestException as error:
        log(f"Could not reach server for device list: {error}. Using local fallback.")
    return LOCAL_FALLBACK


def push_punch(employee_code: str, punch_type: str, timestamp: datetime, device_code: str) -> bool:
    try:
        response = requests.post(
            PUNCH_ENDPOINT,
            headers=auth_headers(),
            json={
                "employeeCode": str(employee_code),
                "punchType": punch_type,
                "punchTimestamp": timestamp.isoformat(),
                "deviceCode": device_code,
            },
            timeout=20,
        )
    except requests.RequestException as error:
        log(f"  push failed (network): {error}")
        return False
    if response.status_code in (200, 201):
        return True
    log(f"  push rejected ({response.status_code}): {response.text[:160]}")
    # 404 = employee code not matched; server keeps it in Failed push inbox, so do not block the batch.
    return response.status_code == 404


def send_heartbeat(statuses: list[dict]) -> None:
    try:
        requests.post(
            DEVICES_ENDPOINT,
            headers=auth_headers(),
            json={"agentId": AGENT_ID, "agentVersion": AGENT_VERSION, "devices": statuses},
            timeout=20,
        )
    except requests.RequestException as error:
        log(f"heartbeat failed: {error}")


def sync_device(device: dict, state: dict, window: "tuple[datetime, datetime] | None" = None) -> dict:
    """Sync one device and return its heartbeat status entry.

    Normal mode (window=None): incremental — push punches newer than the saved
    per-device cursor, then advance the cursor.
    Backfill mode (window=(from, to)): push every punch in that date/time range,
    ignoring and NOT touching the cursor, so normal sync keeps working after.
    """
    code = device.get("deviceCode") or device.get("device_code") or device.get("ip")
    direction = (device.get("direction") or "BOTH").upper()
    last_iso = state.get(code)
    last_time = datetime.fromisoformat(last_iso) if last_iso else None

    conn = None
    zk = ZK(device["ip"], port=int(device.get("port") or 4370), timeout=8, ommit_ping=True)
    try:
        conn = zk.connect()
        conn.disable_device()
        records = conn.get_attendance() or []
    except Exception as error:  # pyzk raises broad exceptions
        log(f"{code} ({device['ip']}): connection error: {error}")
        return {"deviceCode": code, "status": "ERROR", "message": str(error)[:200]}
    finally:
        if conn:
            try:
                conn.enable_device()
                conn.disconnect()
            except Exception:
                pass

    # Drop bad-clock records (year < 2020).
    clean = [r for r in records if r.timestamp.year >= 2020]
    if window is not None:
        from_dt, to_dt = window
        fresh = [r for r in clean if from_dt <= r.timestamp <= to_dt]
    else:
        # On first run, baseline to start of today.
        if last_time is None:
            last_time = datetime.combine(datetime.now().date(), datetime.min.time())
        fresh = [r for r in clean if r.timestamp > last_time]
    fresh.sort(key=lambda r: r.timestamp)
    if not fresh:
        log(f"{code}: no punches in range." if window else f"{code}: no new punches.")
        return {"deviceCode": code, "status": "OK", "message": "No punches in range" if window else "No new punches"}

    log(f"{code}: {len(fresh)} punch(es) to push{' (backfill)' if window else ''}.")
    newest = last_time
    sent = 0
    for record in fresh:
        # Dedicated IN/OUT devices set the direction; a BOTH device trusts the device punch state if available.
        punch_type = direction if direction in ("IN", "OUT") else ("OUT" if getattr(record, "punch", 0) else "IN")
        if push_punch(str(record.user_id), punch_type, record.timestamp, code):
            sent += 1
            if newest is None or record.timestamp > newest:
                newest = record.timestamp
        else:
            break
    # Backfill never moves the incremental cursor (it would skip future punches).
    if window is None and newest is not None:
        state[code] = newest.isoformat()
        save_state(state)
    log(f"{code}: pushed {sent}/{len(fresh)}.")
    return {"deviceCode": code, "status": "OK", "message": f"Pushed {sent}/{len(fresh)}"}


def parse_when(value: str, end_of_day: bool = False) -> datetime:
    """Parse 'YYYY-MM-DD' or full ISO ('YYYY-MM-DDTHH:MM[:SS]'). Date-only maps to
    start of day, or end of day when end_of_day=True."""
    text = value.strip()
    if len(text) == 10:  # date only
        text += "T23:59:59" if end_of_day else "T00:00:00"
    return datetime.fromisoformat(text)


def run_backfill(from_dt: datetime, to_dt: datetime) -> None:
    if not API_KEY:
        sys.exit("COZY_API_KEY is not set.")
    state = load_state()
    devices = fetch_devices()
    if not devices:
        log("No devices configured. Register them in CozyHR -> Time -> Devices, then re-run.")
        return
    log(f"Backfill {from_dt.isoformat()} .. {to_dt.isoformat()} across {len(devices)} device(s).")
    statuses = [sync_device(device, state, window=(from_dt, to_dt)) for device in devices]
    send_heartbeat(statuses)


def sync_users() -> None:
    """Onboarding sync: read enrolled users (id + name) from devices and create employees."""
    if not API_KEY:
        sys.exit("COZY_API_KEY is not set.")
    devices = fetch_devices()
    if not devices:
        log("No devices configured to read users from.")
        return

    seen: dict[str, dict] = {}
    for device in devices:
        conn = None
        zk = ZK(device["ip"], port=int(device.get("port") or 4370), timeout=8, ommit_ping=True)
        try:
            conn = zk.connect()
            users = conn.get_users() or []
        except Exception as error:  # pyzk raises broad exceptions
            log(f"{device.get('deviceCode') or device['ip']}: could not read users: {error}")
            continue
        finally:
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    pass
        for user in users:
            code = str(getattr(user, "user_id", "") or getattr(user, "uid", "")).strip()
            name = (getattr(user, "name", "") or "").strip() or f"User {code}"
            if code and code not in seen:
                seen[code] = {"employeeCode": code, "name": name}

    payload = list(seen.values())
    if not payload:
        log("No enrolled users found on the device(s).")
        return

    log(f"Importing {len(payload)} user(s) from device(s)…")
    total_created = 0
    total_skipped = 0
    for start in range(0, len(payload), 200):
        chunk = payload[start : start + 200]
        try:
            response = requests.post(EMPLOYEES_ENDPOINT, headers=auth_headers(), json={"employees": chunk}, timeout=30)
            data = response.json()
        except requests.RequestException as error:
            log(f"  import failed (network): {error}")
            return
        if response.status_code not in (200, 201):
            log(f"  import rejected ({response.status_code}): {str(data)[:200]}")
            return
        total_created += int(data.get("created", 0))
        total_skipped += int(data.get("skipped", 0))
    log(f"User onboarding sync complete: {total_created} created, {total_skipped} already existed.")


def run_once() -> None:
    if not API_KEY:
        sys.exit("COZY_API_KEY is not set. Create one in CozyHR -> Developers (scopes: attendance.read + attendance.write).")
    state = load_state()
    devices = fetch_devices()
    if not devices:
        log("No devices configured. Register them in CozyHR -> Time -> Devices, then re-run.")
        return
    statuses = [sync_device(device, state) for device in devices]
    send_heartbeat(statuses)


def main() -> None:
    parser = argparse.ArgumentParser(description="CozyHR local biometric sync agent")
    parser.add_argument("--once", action="store_true", help="Run a single sync pass and exit (use with cron).")
    parser.add_argument("--sync-users", action="store_true", help="Import enrolled device users as employees (onboarding), then exit.")
    parser.add_argument("--from", dest="from_when", metavar="YYYY-MM-DD[THH:MM]", help="Backfill: push ALL device punches from this date/time (ignores the incremental cursor).")
    parser.add_argument("--to", dest="to_when", metavar="YYYY-MM-DD[THH:MM]", help="Backfill end (default: now).")
    args = parser.parse_args()

    log(f"CozyHR biometric agent {AGENT_VERSION} (agentId={AGENT_ID}) -> {BASE_URL}")
    if args.sync_users:
        sync_users()
        return
    if args.from_when:
        from_dt = parse_when(args.from_when)
        to_dt = parse_when(args.to_when, end_of_day=True) if args.to_when else datetime.now()
        run_backfill(from_dt, to_dt)
        return
    if args.once:
        run_once()
        return
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            log("Stopped.")
            return
        except Exception as error:
            log(f"Unexpected error: {error}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
