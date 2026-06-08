#!/usr/bin/env python3
"""
CozyHR Biometric Sync — desktop utility.

Cross-platform GUI (Tkinter) to connect ESSL / ZKTeco IN/OUT fingerprint devices
on your local network to CozyHR. Supports ANY number of devices (1, 2, 5, ...),
each with its own direction (IN / OUT / BOTH).

Run:
    pip install pyzk requests
    python cozyhr_biometric_desktop.py
"""

import json
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

try:
    import requests
    from zk import ZK
except ImportError:
    raise SystemExit("Missing dependencies. Run: pip install pyzk requests")

APP_DIR = os.path.join(os.path.expanduser("~"), ".cozyhr-agent")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
STATE_FILE = os.path.join(APP_DIR, "state.json")
AGENT_VERSION = "1.1.0-desktop"
MIN_VALID_YEAR = 2020
DIRECTIONS = ["IN", "OUT", "BOTH"]

DEFAULT_CONFIG = {
    "baseUrl": "https://cozyhr.com",
    "apiKey": "",
    "pollSeconds": 60,
    "devices": [
        {"name": "Main Device", "ip": "192.168.1.201", "port": 4370, "direction": "BOTH", "deviceCode": "DEV-1"},
    ],
}


def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return dict(fallback) if isinstance(fallback, dict) else fallback


def save_json(path, data):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


class SyncCore:
    """Headless logic; logs through a callback. Works for any number of devices."""

    def __init__(self, config, log):
        self.config = config
        self.log = log

    def headers(self):
        return {"Authorization": f"Bearer {self.config['apiKey'].strip()}", "Content-Type": "application/json"}

    def _connect(self, device):
        return ZK(device["ip"], port=int(device.get("port") or 4370), timeout=8, ommit_ping=True).connect()

    def fetch_remote_devices(self):
        """Pull devices registered in CozyHR (Time -> Devices) for this tenant."""
        base = self.config["baseUrl"].rstrip("/")
        res = requests.get(f"{base}/api/public/v1/attendance/devices", headers=self.headers(), timeout=20)
        res.raise_for_status()
        out = []
        for d in res.json().get("devices", []):
            if d.get("ip"):
                out.append({"name": d.get("name") or d["deviceCode"], "ip": d["ip"], "port": d.get("port") or 4370,
                            "direction": (d.get("direction") or "BOTH").upper(), "deviceCode": d["deviceCode"]})
        return out

    def register_devices(self):
        """Push the configured devices to CozyHR so they appear in Time -> Devices with live status."""
        if not self.config["apiKey"].strip():
            return
        base = self.config["baseUrl"].rstrip("/")
        payload = [{
            "deviceCode": d["deviceCode"], "name": d.get("name") or d["deviceCode"], "ip": d["ip"],
            "port": int(d.get("port") or 4370), "direction": (d.get("direction") or "BOTH").upper(), "status": "OK",
        } for d in self.config["devices"] if d.get("ip")]
        if not payload:
            return
        try:
            res = requests.post(f"{base}/api/public/v1/attendance/devices", headers=self.headers(),
                                json={"agentId": f"desktop-{os.getpid()}", "agentVersion": AGENT_VERSION, "devices": payload}, timeout=20)
            if res.status_code in (200, 201):
                data = res.json()
                self.log(f"OK Registered devices in CozyHR ({data.get('created', 0)} new, {data.get('updated', 0)} updated).")
            else:
                self.log(f"Could not register devices in CozyHR ({res.status_code}).")
        except requests.RequestException as error:
            self.log(f"Could not register devices in CozyHR: {error}")

    def test(self):
        for device in self.config["devices"]:
            conn = None
            try:
                conn = self._connect(device)
                self.log(f"OK {device['name']} ({device['ip']}): connected, {len(conn.get_users() or [])} users.")
            except Exception as error:
                self.log(f"FAIL {device['name']} ({device['ip']}): {error}")
            finally:
                if conn:
                    try:
                        conn.disconnect()
                    except Exception:
                        pass
        # Devices that connected: register them in CozyHR so they show in Time -> Devices.
        self.register_devices()

    def import_users(self):
        if not self.config["apiKey"].strip():
            self.log("Set your API key first.")
            return
        seen = {}
        for device in self.config["devices"]:
            conn = None
            try:
                conn = self._connect(device)
                users = conn.get_users() or []
            except Exception as error:
                self.log(f"FAIL {device['name']}: {error}")
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
            self.log("No enrolled users found.")
            return
        self.log(f"Importing {len(payload)} user(s)...")
        created = skipped = 0
        base = self.config["baseUrl"].rstrip("/")
        for start in range(0, len(payload), 200):
            res = requests.post(f"{base}/api/public/v1/employees", headers=self.headers(), json={"employees": payload[start:start + 200]}, timeout=30)
            data = res.json()
            if res.status_code not in (200, 201):
                self.log(f"FAIL import rejected ({res.status_code}): {str(data)[:200]}")
                return
            created += int(data.get("created", 0))
            skipped += int(data.get("skipped", 0))
        self.log(f"OK Users imported: {created} created, {skipped} existed.")

    def sync_punches(self, state, baseline_now=True):
        if not self.config["apiKey"].strip():
            self.log("Set your API key first.")
            return
        base = self.config["baseUrl"].rstrip("/")
        for device in self.config["devices"]:
            code = device["deviceCode"]
            direction = (device.get("direction") or "BOTH").upper()
            last_iso = state.get(code)
            last_time = datetime.fromisoformat(last_iso) if last_iso else None

            conn = None
            try:
                conn = self._connect(device)
                conn.disable_device()
                records = conn.get_attendance() or []
            except Exception as error:
                self.log(f"FAIL {device['name']}: {error}")
                continue
            finally:
                if conn:
                    try:
                        conn.enable_device()
                        conn.disconnect()
                    except Exception:
                        pass

            clean = [r for r in records if r.timestamp.year >= MIN_VALID_YEAR]
            if last_time is None and baseline_now:
                # First run: sync from the START OF TODAY (skip old history, keep today's punches).
                last_time = datetime.combine(datetime.now().date(), datetime.min.time())
                self.log(f"{code}: first run — syncing today's punches (from {last_time.date()}).")

            fresh = sorted([r for r in clean if last_time is None or r.timestamp > last_time], key=lambda r: r.timestamp)
            if not fresh:
                self.log(f"{code}: no new punches.")
                continue
            newest, sent = last_time, 0
            for record in fresh:
                # Dedicated IN/OUT devices set direction; a BOTH device uses the device's punch flag.
                punch_type = direction if direction in ("IN", "OUT") else ("OUT" if getattr(record, "punch", 0) else "IN")
                try:
                    res = requests.post(f"{base}/api/public/v1/attendance/punches", headers=self.headers(),
                                        json={"employeeCode": str(record.user_id), "punchType": punch_type,
                                              "punchTimestamp": record.timestamp.isoformat(), "deviceCode": code}, timeout=20)
                except requests.RequestException as error:
                    self.log(f"FAIL {code}: network error: {error}")
                    break
                if res.status_code in (200, 201, 404):
                    if res.status_code != 404:
                        sent += 1
                    if newest is None or record.timestamp > newest:
                        newest = record.timestamp
                else:
                    self.log(f"FAIL {code}: rejected ({res.status_code}) {res.text[:120]}")
                    break
            if newest is not None:
                state[code] = newest.isoformat()
                save_json(STATE_FILE, state)
            self.log(f"OK {code}: pushed {sent}/{len(fresh)}.")
        self.register_devices()


class App:
    def __init__(self, root):
        self.root = root
        root.title("CozyHR Biometric Sync")
        root.geometry("780x640")
        self.config = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, DEFAULT_CONFIG)}
        self.state = load_json(STATE_FILE, {})
        self.log_queue: queue.Queue = queue.Queue()
        self.running = False
        self.device_rows = []
        self._build()
        self._drain_logs()

    def _build(self):
        pad = {"padx": 6, "pady": 3}
        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="CozyHR Biometric Sync", font=("Helvetica", 15, "bold")).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(frm, text="Connect any number of ESSL / ZKTeco devices to CozyHR.").grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))

        ttk.Label(frm, text="CozyHR URL").grid(row=2, column=0, sticky="w", **pad)
        self.base = ttk.Entry(frm, width=44)
        self.base.insert(0, self.config["baseUrl"])
        self.base.grid(row=2, column=1, columnspan=3, sticky="we", **pad)

        ttk.Label(frm, text="API key").grid(row=3, column=0, sticky="w", **pad)
        self.key = ttk.Entry(frm, width=44, show="*")
        self.key.insert(0, self.config["apiKey"])
        self.key.grid(row=3, column=1, columnspan=3, sticky="we", **pad)

        # Devices header
        dev_head = ttk.Frame(frm)
        dev_head.grid(row=4, column=0, columnspan=4, sticky="we", pady=(8, 0))
        ttk.Label(dev_head, text="Devices", font=("Helvetica", 12, "bold")).pack(side="left")
        ttk.Button(dev_head, text="+ Add device", command=lambda: self.add_device_row()).pack(side="left", padx=8)
        ttk.Button(dev_head, text="Load from CozyHR", command=self.load_remote).pack(side="left")

        col = ttk.Frame(frm)
        col.grid(row=5, column=0, columnspan=4, sticky="we")
        for i, label in enumerate(["Name", "IP address", "Port", "Direction", "Code", ""]):
            ttk.Label(col, text=label, font=("Helvetica", 9, "bold")).grid(row=0, column=i, padx=4, sticky="w")
        self.devices_frame = ttk.Frame(frm)
        self.devices_frame.grid(row=6, column=0, columnspan=4, sticky="we")
        for device in self.config["devices"]:
            self.add_device_row(device)

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=4, sticky="w", pady=10)
        ttk.Button(btns, text="Save", command=self.save).pack(side="left", padx=4)
        ttk.Button(btns, text="Test devices", command=lambda: self.run_async(self.core().test)).pack(side="left", padx=4)
        ttk.Button(btns, text="Import users", command=lambda: self.run_async(self.core().import_users)).pack(side="left", padx=4)
        ttk.Button(btns, text="Sync punches now", command=lambda: self.run_async(lambda: self.core().sync_punches(self.state))).pack(side="left", padx=4)
        self.auto_btn = ttk.Button(btns, text="Start auto-sync", command=self.toggle_auto)
        self.auto_btn.pack(side="left", padx=4)

        self.logbox = tk.Text(frm, height=12, state="disabled", wrap="word")
        self.logbox.grid(row=8, column=0, columnspan=4, sticky="nsew", pady=(6, 0))
        frm.rowconfigure(8, weight=1)
        frm.columnconfigure(1, weight=1)
        self.log("Ready. Add your devices, Save, then Test devices.")

    def add_device_row(self, device=None):
        device = device or {"name": "", "ip": "", "port": 4370, "direction": "BOTH", "deviceCode": ""}
        row = ttk.Frame(self.devices_frame)
        row.pack(fill="x", pady=2)
        name = ttk.Entry(row, width=16); name.insert(0, device.get("name", "")); name.grid(row=0, column=0, padx=2)
        ip = ttk.Entry(row, width=16); ip.insert(0, device.get("ip", "")); ip.grid(row=0, column=1, padx=2)
        port = ttk.Entry(row, width=6); port.insert(0, str(device.get("port", 4370))); port.grid(row=0, column=2, padx=2)
        direction = ttk.Combobox(row, width=7, values=DIRECTIONS, state="readonly")
        direction.set(device.get("direction", "BOTH")); direction.grid(row=0, column=3, padx=2)
        code = ttk.Entry(row, width=12); code.insert(0, device.get("deviceCode", "")); code.grid(row=0, column=4, padx=2)
        entry = {"frame": row, "name": name, "ip": ip, "port": port, "direction": direction, "code": code}
        ttk.Button(row, text="Remove", width=8, command=lambda: self.remove_device_row(entry)).grid(row=0, column=5, padx=2)
        self.device_rows.append(entry)

    def remove_device_row(self, entry):
        entry["frame"].destroy()
        self.device_rows.remove(entry)

    def read_devices(self):
        devices = []
        for r in self.device_rows:
            ip = r["ip"].get().strip()
            if not ip:
                continue
            devices.append({
                "name": r["name"].get().strip() or ip,
                "ip": ip,
                "port": int(r["port"].get().strip() or 4370),
                "direction": r["direction"].get() or "BOTH",
                "deviceCode": r["code"].get().strip() or ip.replace(".", "-"),
            })
        return devices

    def core(self):
        self.config["baseUrl"] = self.base.get().strip()
        self.config["apiKey"] = self.key.get().strip()
        self.config["devices"] = self.read_devices()
        return SyncCore(self.config, self.log)

    def load_remote(self):
        def work():
            try:
                remote = self.core().fetch_remote_devices()
            except Exception as error:
                self.log(f"Could not load devices from CozyHR: {error}")
                return
            if not remote:
                self.log("No devices with an IP found in CozyHR (add them in Time -> Devices).")
                return
            self.root.after(0, lambda: self._replace_devices(remote))
        self.run_async(work)

    def _replace_devices(self, devices):
        for entry in list(self.device_rows):
            self.remove_device_row(entry)
        for device in devices:
            self.add_device_row(device)
        self.log(f"Loaded {len(devices)} device(s) from CozyHR.")

    def save(self):
        self.config["baseUrl"] = self.base.get().strip()
        self.config["apiKey"] = self.key.get().strip()
        self.config["devices"] = self.read_devices()
        save_json(CONFIG_FILE, self.config)
        self.log(f"Settings saved ({len(self.config['devices'])} device(s)).")

    def log(self, message):
        self.log_queue.put(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def _drain_logs(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.logbox.configure(state="normal")
                self.logbox.insert("end", line + "\n")
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(300, self._drain_logs)

    def run_async(self, fn):
        threading.Thread(target=self._guard(fn), daemon=True).start()

    def _guard(self, fn):
        def wrapped():
            try:
                fn()
            except Exception as error:
                self.log(f"Error: {error}")
        return wrapped

    def toggle_auto(self):
        if self.running:
            self.running = False
            self.auto_btn.config(text="Start auto-sync")
            self.log("Auto-sync stopped.")
            return
        if not self.key.get().strip():
            messagebox.showwarning("API key", "Enter your CozyHR API key first.")
            return
        self.running = True
        self.auto_btn.config(text="Stop auto-sync")
        self.log(f"Auto-sync started (every {self.config['pollSeconds']}s).")
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            try:
                self.core().sync_punches(self.state)
            except Exception as error:
                self.log(f"{error}")
            for _ in range(int(self.config["pollSeconds"])):
                if not self.running:
                    break
                time.sleep(1)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
