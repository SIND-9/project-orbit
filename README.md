# 🚀 Orbit — Lightweight Local Deployment Tool

Orbit is a two-part Python deployment system for pushing code from your development machine to a remote device (e.g. a local server, tablet, or SBC) over your LAN. It consists of a **client** (`orbit.py`) that packages and ships your project, and a **daemon** (`orbit_daemon.py`) that receives, verifies, and deploys it.

No Docker. No CI/CD pipeline. Just Python.

---

## How It Works

```
[ Your Dev Machine ]                  [ Remote Device ]
   orbit.py          ──── HTTPS ────▶  orbit_daemon.py
   - Reads config                       - Authenticates token
   - Zips changed files                 - Verifies checksum
   - Sends to daemon                    - Snapshots old deploy
                                        - Extracts new files
                                        - Restarts your app
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install requests tqdm
```

### 2. Configure the client

Copy the example config and fill in your device's IP, port, and token:

```bash
cp orbit.config.json.example orbit.config.json
```

```json
{
  "default": {
    "ip": "192.168.1.71",
    "port": "8080",
    "token": "YourSecretToken"
  }
}
```

### 3. Configure the daemon

On your remote device, open `orbit_daemon.py` and set your hashed token:

```bash
python3 -c "import hashlib; print(hashlib.sha256(b'YourSecretToken').hexdigest())"
```

Paste the output into `SECRET_TOKEN_HASH` in the daemon file.

### 4. Start the daemon (on remote device)

```bash
python3 orbit_daemon.py
```

### 5. Deploy (from your dev machine)

```bash
python orbit.py
```

---

## Client Usage (`orbit.py`)

```bash
# Standard deploy using the default profile
python orbit.py

# Simulate a deployment without sending anything
python orbit.py --dry-run

# Only send files that changed since the last deploy
python orbit.py --delta

# Use a named profile from orbit.config.json
python orbit.py --profile staging

# Combine flags freely
python orbit.py --delta --profile tablet --dry-run
```

### CLI Flags

| Flag | Description |
|---|---|
| `--profile <name>` | Config profile to use (default: `"default"`) |
| `--dry-run` | Preview what would be deployed without sending |
| `--delta` | Only deploy files changed since last successful deploy |

---

## Features

### Deployment Profiles
Define multiple targets (e.g. `tablet`, `staging`, `server`) in a single `orbit.config.json`. Switch between them with `--profile`. No more hardcoded IPs.

### Pre-Deploy Health Check
Before zipping anything, the client pings the daemon's `/health` endpoint. If the daemon is unreachable, the deploy aborts immediately with a clear error — no wasted time on a big upload that goes nowhere.

### Delta / Diff Deployments (`--delta`)
The client computes a SHA256 hash of every file and compares it against a local cache (`orbit_hashes.json`). Only new or modified files are packaged. The cache is only updated after the daemon confirms a successful deploy.

### Checksum Verification
The client computes a SHA256 hash of the zip payload and sends it as `X-Orbit-Checksum`. The daemon recomputes the hash independently and rejects any payload where they don't match, protecting against corruption on shaky networks.

### Deployment Manifest
Every zip includes a `deployment.manifest.json` that lands in your `deployed_app` folder. It records the deploy timestamp, profile name, delta mode status, and the list of files included.

### Rollback Snapshots
Before extracting new files, the daemon zips the current `deployed_app` directory and saves it as a timestamped snapshot in `orbit_snapshots/`. The last 5 snapshots are retained automatically (configurable via `MAX_SNAPSHOTS`).

### IP Allowlist
Restrict which machines can push deployments by populating `ALLOWED_IPS` in the daemon. Any connection from an unlisted IP is rejected with `403 Forbidden` before the token is even checked.

### Token Security
The daemon never stores the plaintext token — only its SHA256 hash. Even if someone reads the daemon source, they can't recover the real token.

### Post-Deploy Webhook
Configure a `WEBHOOK_URL` in the daemon to receive a JSON notification after every successful deployment. Works with Slack, Discord, or any custom endpoint.

### Custom Run Command
Send an `X-Orbit-Run-Command` header (or set `DEFAULT_RUN_COMMAND` in the daemon) to automatically restart your application after each deploy. The daemon kills the old process by name and launches the new one as a background process.

### File Size Limits
The daemon inspects the zip before extraction. It rejects payloads where any single file exceeds 50 MB or the total uncompressed size exceeds 200 MB, preventing accidental disk-filling uploads.

### `.orbitignore` Support
Create a `.orbitignore` file in your project root (same syntax as `.gitignore`) to exclude folders and files from packaging. Combines with built-in defaults (`.git`, `__pycache__`, `node_modules`, `dist`, `build`).

### Structured Deployment Log
Every deploy attempt (success or failure) is appended to `deployment.log` on the daemon as a JSON line, including timestamp, source IP, file count, checksum, and outcome.

---

## File Reference

| File | Role |
|---|---|
| `orbit.py` | Client — packages and ships your project |
| `orbit_daemon.py` | Daemon — receives, verifies, and deploys |
| `orbit.config.json` | Your local deployment profiles (not committed) |
| `orbit.config.json.example` | Template to copy from |
| `.orbitignore` | Files/folders to exclude from packaging |
| `orbit_hashes.json` | Local hash cache for delta deployments (auto-generated) |
| `deployment.log` | Append-only deploy audit log (on remote device) |
| `orbit_snapshots/` | Rollback archive directory (on remote device) |
| `deployed_app/` | Extracted deployment target (on remote device) |

---

## Security Notes

- Keep `orbit.config.json` out of version control — add it to `.gitignore`.
- Use a strong, random token. Generate one with: `python3 -c "import secrets; print(secrets.token_hex(32))"`
- Enable `ALLOWED_IPS` on the daemon in any environment beyond your personal LAN.
- The daemon binds to `0.0.0.0` by default. On a shared network, the IP allowlist is your primary access control.

---

## Requirements

- Python 3.10+
- `requests`
- `tqdm`

Both scripts use only standard library modules on the daemon side. The client requires the two packages above.

---

## License

MIT
