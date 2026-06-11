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

## Backfill a date range (re-pull history)
Need older punches, or to re-import a day? Sync a specific window — this pulls
**every** punch in that range still on the device and **does not touch** the
incremental cursor, so normal auto-sync keeps working afterwards.

CLI:
```bash
python sync_agent.py --from 2026-06-08                 # from that date 00:00 to now
python sync_agent.py --from 2026-06-08 --to 2026-06-10 # an explicit window
python sync_agent.py --from 2026-06-11T09:00           # date + time also accepted
```

Desktop app: use the **Backfill range** row — enter *From* / *To* (`YYYY-MM-DD`)
and click **Sync range**.

> Only data still in the device's memory can be backfilled — eSSL/ZKTeco units
> overwrite the oldest punches once full.
