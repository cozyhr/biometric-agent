#!/usr/bin/env python3
"""
CozyHR Biometric Sync — desktop utility.

A simple cross-platform GUI (Tkinter) for connecting ESSL / ZKTeco IN/OUT
fingerprint devices on your local network to CozyHR. It can:

  • Test device connections
  • Import enrolled users as employees (onboarding)
  • Sync punches (incremental, baseline = now by default; optional backfill)
  • Run continuously in the background

Run:
    pip install pyzk requests
    python cozyhr_biometric_desktop.py

Package to a standalone app (optional):
    pip install pyinstaller
    pyinstaller --onefile --windowed --name "CozyHR Biometric Sync" cozyhr_biometric_desktop.py

Notes learned from real ESSL devices:
  • IN vs OUT comes from WHICH device a punch is on (the device 'punch' flag is
    unreliable), so each device has a fixed direction.
  • Some devices report bad clock timestamps (year 2000) — those are skipped.
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
AGENT_VERSION = "1.0.0-desktop"
MIN_VALID_YEAR = 2020

DEFAULT_CONFIG = {
    "baseUrl": "https://cozyhr.com",
    "apiKey": "",
    "pollSeconds": 60,
    "devices": [
        {"name": "IN Device", "ip": "192.168.29.201", "port": 4370, "direction": "IN", "deviceCode": "IN-201"},
        {"name": "OUT Device", "ip": "192.168.29.202", "port": 4370, "direction": "OUT", "deviceCode": "OUT-202"},
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
    """Headless logic shared by all buttons; logs through a callback."""

    def __init__(self, config, log):
        self.config = config
        self.log = log

    def headers(self):
        return {"Authorization": f"Bearer {self.config['apiKey'].strip()}", "Content-Type": "application/json"}

    def _connect(self, device):
        zk = ZK(device["ip"], port=int(device.get("port") or 4370), timeout=8, ommit_ping=True)
        return zk.connect()

    def test(self):
        ok = True
        for device in self.config["devices"]:
            conn = None
            try:
                conn = self._connect(device)
                users = conn.get_users() or []
                self.log(f"OK {device['name']} ({device['ip']}): connected — {len(users)} users enrolled.")
            except Exception as error:
                ok = False
                self.log(f"FAIL {device['name']} ({device['ip']}): {error}")
            finally:
                if conn:
                    try:
                        conn.disconnect()
                    except Exception:
                        pass
        return ok

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
                self.log(f"FAIL {device['name']}: could not read users: {error}")
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
        self.log(f"Importing {len(payload)} user(s) as employees…")
        created = skipped = 0
        for start in range(0, len(payload), 200):
            chunk = payload[start:start + 200]
            try:
                res = requests.post(f"{self.config['baseUrl'].rstrip('/')}/api/public/v1/employees", headers=self.headers(), json={"employees": chunk}, timeout=30)
                data = res.json()
            except requests.RequestException as error:
                self.log(f"FAIL import failed: {error}")
                return
            if res.status_code not in (200, 201):
                self.log(f"FAIL import rejected ({res.status_code}): {str(data)[:200]}")
                return
            created += int(data.get("created", 0))
            skipped += int(data.get("skipped", 0))
        self.log(f"OK Users imported: {created} created, {skipped} already existed.")

    def sync_punches(self, state, baseline_now=True):
        if not self.config["apiKey"].strip():
            self.log("Set your API key first.")
            return
        base_url = self.config["baseUrl"].rstrip("/")
        for device in self.config["devices"]:
            code = device["deviceCode"]
            direction = device["direction"].upper()
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

            # Drop bad-clock records; keep only newer than last sync.
            clean = [r for r in records if r.timestamp.year >= MIN_VALID_YEAR]
            if last_time is None and baseline_now:
                # First run: baseline to the latest record so we don't push years of history.
                state[code] = (max((r.timestamp for r in clean), default=datetime.now())).isoformat()
                save_json(STATE_FILE, state)
                self.log(f"{code}: baseline set ({len(clean)} historical records skipped). New punches will sync from now.")
                continue

            fresh = [r for r in clean if last_time is None or r.timestamp > last_time]
            fresh.sort(key=lambda r: r.timestamp)
            if not fresh:
                self.log(f"{code}: no new punches.")
                continue

            newest = last_time
            sent = 0
            for record in fresh:
                try:
                    res = requests.post(
                        f"{base_url}/api/public/v1/attendance/punches",
                        headers=self.headers(),
                        json={"employeeCode": str(record.user_id), "punchType": direction, "punchTimestamp": record.timestamp.isoformat(), "deviceCode": code},
                        timeout=20,
                    )
                except requests.RequestException as error:
                    self.log(f"FAIL {code}: network error: {error}")
                    break
                if res.status_code in (200, 201) or res.status_code == 404:
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
            self.log(f"OK {code}: pushed {sent}/{len(fresh)} punch(es).")

        # Heartbeat (best-effort).
        try:
            requests.post(
                f"{base_url}/api/public/v1/attendance/devices",
                headers=self.headers(),
                json={"agentId": f"desktop-{os.getpid()}", "agentVersion": AGENT_VERSION, "devices": [{"deviceCode": d["deviceCode"], "status": "OK"} for d in self.config["devices"]]},
                timeout=15,
            )
        except requests.RequestException:
            pass


class App:
    def __init__(self, root):
        self.root = root
        root.title("CozyHR Biometric Sync")
        root.geometry("720x560")
        self.config = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, DEFAULT_CONFIG)}
        self.state = load_json(STATE_FILE, {})
        self.log_queue: queue.Queue = queue.Queue()
        self.running = False
        self._build()
        self._drain_logs()

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="CozyHR Biometric Sync", font=("Helvetica", 15, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(frm, text="Connect your ESSL / ZKTeco IN & OUT devices to CozyHR.").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Label(frm, text="CozyHR URL").grid(row=2, column=0, sticky="w", **pad)
        self.base = ttk.Entry(frm, width=46)
        self.base.insert(0, self.config["baseUrl"])
        self.base.grid(row=2, column=1, columnspan=2, sticky="we", **pad)

        ttk.Label(frm, text="API key").grid(row=3, column=0, sticky="w", **pad)
        self.key = ttk.Entry(frm, width=46, show="*")
        self.key.insert(0, self.config["apiKey"])
        self.key.grid(row=3, column=1, columnspan=2, sticky="we", **pad)

        ttk.Label(frm, text="IN device IP").grid(row=4, column=0, sticky="w", **pad)
        self.in_ip = ttk.Entry(frm, width=20)
        self.in_ip.insert(0, self.config["devices"][0]["ip"])
        self.in_ip.grid(row=4, column=1, sticky="w", **pad)

        ttk.Label(frm, text="OUT device IP").grid(row=5, column=0, sticky="w", **pad)
        self.out_ip = ttk.Entry(frm, width=20)
        self.out_ip.insert(0, self.config["devices"][1]["ip"])
        self.out_ip.grid(row=5, column=1, sticky="w", **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(btns, text="Save", command=self.save).pack(side="left", padx=4)
        ttk.Button(btns, text="Test devices", command=lambda: self.run_async(self.core().test)).pack(side="left", padx=4)
        ttk.Button(btns, text="Import users", command=lambda: self.run_async(self.core().import_users)).pack(side="left", padx=4)
        ttk.Button(btns, text="Sync punches now", command=lambda: self.run_async(lambda: self.core().sync_punches(self.state))).pack(side="left", padx=4)
        self.auto_btn = ttk.Button(btns, text="Start auto-sync", command=self.toggle_auto)
        self.auto_btn.pack(side="left", padx=4)

        self.logbox = tk.Text(frm, height=18, width=86, state="disabled", wrap="word")
        self.logbox.grid(row=7, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        frm.rowconfigure(7, weight=1)
        frm.columnconfigure(1, weight=1)
        self.log("Ready. Save your settings, then Test devices.")

    def core(self):
        self._sync_config_from_form()
        return SyncCore(self.config, self.log)

    def _sync_config_from_form(self):
        self.config["baseUrl"] = self.base.get().strip()
        self.config["apiKey"] = self.key.get().strip()
        self.config["devices"][0]["ip"] = self.in_ip.get().strip()
        self.config["devices"][1]["ip"] = self.out_ip.get().strip()

    def save(self):
        self._sync_config_from_form()
        save_json(CONFIG_FILE, self.config)
        self.log("Settings saved.")

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
