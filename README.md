# CozyHR Biometric Sync Agent

Connect ESSL / ZKTeco IN/OUT fingerprint devices on your local network to
[CozyHR](https://cozyhr.com). Reads punches and enrolled users from the devices
and pushes them to your CozyHR workspace.

- **Desktop app (recommended)** - `cozyhr_biometric_desktop.py` - a simple GUI.
- **Headless CLI** - `sync_agent.py` - for servers / cron.

## Run from source
```bash
pip install -r requirements.txt
python cozyhr_biometric_desktop.py
python sync_agent.py --sync-users
python sync_agent.py
```

## Prebuilt apps
Each release publishes ready-to-run binaries for macOS, Windows and Linux on the Releases page.

## What you need
1. A CozyHR API key (Developers -> API keys; scopes attendance.read, attendance.write, employees.write).
2. This machine on the same LAN as the devices (ZK devices use TCP port 4370).
