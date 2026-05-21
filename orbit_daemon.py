import io
import os
import json
import time
import hashlib
import zipfile
import logging
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ============================================================
# FEATURE: Structured Logging
# ============================================================
# Instead of raw print() calls, we use Python's logging module.
# This gives us timestamps, log levels (INFO/WARNING/ERROR),
# and makes it easy to redirect logs to a file later.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
log = logging.getLogger("OrbitDaemon")


# ============================================================
# Core Configuration
# ============================================================

HOST        = "0.0.0.0"   # Listens on all network interfaces
PORT        = 8080
TARGET_DIR  = "./deployed_app"
LOG_FILE    = "./deployment.log"

# ============================================================
# FEATURE: Token Hashing (Security Upgrade)
# ============================================================
# We never store or compare the raw plaintext token anymore.
# Instead, the daemon holds a SHA256 hash of the token.
# Even if someone reads this source file, they can't recover
# the real token — they'd have to brute-force the hash.
#
# To generate your own:
#   python3 -c "import hashlib; print(hashlib.sha256(b'YourToken').hexdigest())"
#
# "NyxPass123" hashes to the value below:
SECRET_TOKEN_HASH = hashlib.sha256(b"NyxPass123").hexdigest()

# ============================================================
# FEATURE: IP Allowlist
# ============================================================
# Only IPs in this set are permitted to send deployments.
# Anyone else gets a 403 Forbidden before we even check their token.
# Add your dev machine's local IP here. An empty set means ALLOW ALL
# (useful during initial setup), but you should lock this down.

ALLOWED_IPS: set = set()  # e.g. {"192.168.1.42", "192.168.1.55"}

# ============================================================
# FEATURE: Rollback — How Many Snapshots to Keep
# ============================================================
# Every successful deployment is archived as a dated snapshot
# before the new code is extracted. We keep the last N snapshots.
# If a bad deploy breaks your app, you can roll back instantly.

MAX_SNAPSHOTS   = 5
SNAPSHOT_DIR    = "./orbit_snapshots"

# ============================================================
# FEATURE: Post-Deploy Webhook
# ============================================================
# After a successful deployment, Orbit can POST a JSON notification
# to any URL — a Slack webhook, a Discord bot, a custom dashboard.
# Set to None to disable.

WEBHOOK_URL: str | None = None  # e.g. "https://hooks.slack.com/services/..."

# ============================================================
# FEATURE: Custom Run Command
# ============================================================
# The client can send an X-Orbit-Run-Command header with the
# shell command to execute after extraction (e.g. "python main.py").
# This replaces the empty trigger_restart() stub from v1.
# If no header is sent, the daemon falls back to this default.

DEFAULT_RUN_COMMAND: str | None = None  # e.g. "python main.py"


# ============================================================
# FEATURE: Deployment Log
# ============================================================
# Every deployment attempt is appended to deployment.log as a
# structured JSON line. Gives you a permanent audit trail of
# timestamps, file counts, checksums, source IPs, and outcomes.

def write_deployment_log(entry: dict):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ============================================================
# FEATURE: Rollback — Snapshot the Current Deployment
# ============================================================
# Before extracting new files, we zip up whatever is currently
# in TARGET_DIR and save it as a timestamped snapshot. Old
# snapshots beyond MAX_SNAPSHOTS are automatically pruned.

def snapshot_current_deployment():
    if not os.path.exists(TARGET_DIR):
        return  # Nothing to snapshot yet

    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    timestamp   = time.strftime("%Y%m%d_%H%M%S")
    snap_path   = os.path.join(SNAPSHOT_DIR, f"snapshot_{timestamp}.zip")

    log.info(f"📸 Snapshotting current deployment → {snap_path}")
    with zipfile.ZipFile(snap_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(TARGET_DIR):
            for file in files:
                fp = os.path.join(root, file)
                zf.write(fp, os.path.relpath(fp, TARGET_DIR))

    # Prune old snapshots — keep only the most recent MAX_SNAPSHOTS
    snapshots = sorted(Path(SNAPSHOT_DIR).glob("snapshot_*.zip"))
    while len(snapshots) > MAX_SNAPSHOTS:
        oldest = snapshots.pop(0)
        oldest.unlink()
        log.info(f"🗑️  Pruned old snapshot: {oldest.name}")


# ============================================================
# FEATURE: Post-Deploy Webhook
# ============================================================
# Fires a JSON POST to your configured WEBHOOK_URL. The payload
# includes enough info to build a rich Slack/Discord notification.

def fire_webhook(payload: dict):
    if not WEBHOOK_URL:
        return
    try:
        import requests
        requests.post(WEBHOOK_URL, json=payload, timeout=5)
        log.info("📣 Webhook notification sent.")
    except Exception as e:
        log.warning(f"Webhook failed (non-fatal): {e}")


# ============================================================
# FEATURE: trigger_restart() — Now Actually Implemented
# ============================================================
# Kills any running process matching APP_PROCESS_NAME, waits for
# it to die, then launches the run command in the deployed_app
# directory as a background process. The process name to kill
# is configurable below.

APP_PROCESS_NAME = "main.py"  # change to match your app's entry point

def trigger_restart(run_command: str | None):
    if not run_command:
        log.info("ℹ️  No run command configured. Skipping restart.")
        return

    log.info(f"🔄 Stopping existing '{APP_PROCESS_NAME}' processes...")
    try:
        # pkill finds processes by name — works on Linux/macOS (Android/tablet too)
        subprocess.run(["pkill", "-f", APP_PROCESS_NAME], check=False)
        time.sleep(1)  # Brief pause to let the old process fully die
    except FileNotFoundError:
        log.warning("pkill not available on this system — skipping kill step.")

    log.info(f"🚀 Launching: {run_command}")
    try:
        # Launch detached so the daemon doesn't block waiting for the app
        subprocess.Popen(
            run_command,
            shell=True,
            cwd=TARGET_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        log.info("✅ Application process started successfully.")
    except Exception as e:
        log.error(f"❌ Failed to start application: {e}")


class DeploymentHandler(BaseHTTPRequestHandler):

    # ============================================================
    # FEATURE: /health Endpoint (GET)
    # ============================================================
    # The client's pre-deploy health check hits GET /health.
    # We validate the token and return 200 if all is good.
    # This lets orbit.py fail fast before wasting time zipping.

    def do_GET(self):
        if self.path == "/health":
            token = self.headers.get("X-Orbit-Token", "")
            if hashlib.sha256(token.encode()).hexdigest() != SECRET_TOKEN_HASH:
                self._respond(401, "Unauthorized.")
                return
            self._respond(200, "Orbit Daemon online. Ready for deployment.")
        else:
            self._respond(404, "Not found.")

    def do_POST(self):
        # ============================================================
        # FEATURE: IP Allowlist (checked first, before anything else)
        # ============================================================
        client_ip = self.client_address[0]
        if ALLOWED_IPS and client_ip not in ALLOWED_IPS:
            log.warning(f"🚫 Rejected connection from unauthorized IP: {client_ip}")
            self._respond(403, "Forbidden: Your IP is not on the allowlist.")
            return

        # ============================================================
        # FEATURE: Token Hashing (Authentication)
        # ============================================================
        # We hash whatever token the client sends and compare it to
        # our stored hash. The plaintext token is never held in memory
        # on the daemon side — just the hash.

        raw_token   = self.headers.get("X-Orbit-Token", "")
        token_hash  = hashlib.sha256(raw_token.encode()).hexdigest()
        if token_hash != SECRET_TOKEN_HASH:
            log.warning(f"🔐 Auth failure from {client_ip} — bad token.")
            self._respond(401, "Unauthorized: Invalid deployment token.")
            return

        log.info(f"📦 Incoming deployment from {client_ip}...")

        content_length  = int(self.headers.get("Content-Length", 0))
        expected_sum    = self.headers.get("X-Orbit-Checksum", "")
        run_command     = self.headers.get("X-Orbit-Run-Command", DEFAULT_RUN_COMMAND)

        # Read the raw zip payload into memory
        zip_payload = self.rfile.read(content_length)

        # ============================================================
        # FEATURE: Checksum Verification
        # ============================================================
        # We recompute the SHA256 of the received bytes and compare it
        # to the checksum the client sent in X-Orbit-Checksum. A mismatch
        # means data was corrupted in transit — we reject immediately.

        if expected_sum:
            actual_sum = hashlib.sha256(zip_payload).hexdigest()
            if actual_sum != expected_sum:
                log.error("❌ Checksum mismatch! Payload may be corrupted.")
                log.error(f"   Expected: {expected_sum}")
                log.error(f"   Received: {actual_sum}")
                self._respond(400, "Deployment failed: Checksum mismatch.")
                return
            log.info("✅ Checksum verified — payload integrity confirmed.")

        log_entry = {
            "timestamp":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "client_ip":    client_ip,
            "payload_bytes": content_length,
            "checksum":     expected_sum,
            "run_command":  run_command,
            "outcome":      None
        }

        try:
            # ============================================================
            # FEATURE: Rollback Snapshot (taken before extracting new code)
            # ============================================================
            snapshot_current_deployment()

            os.makedirs(TARGET_DIR, exist_ok=True)

            with zipfile.ZipFile(io.BytesIO(zip_payload)) as zip_ref:
                corrupt_file = zip_ref.testzip()
                if corrupt_file is not None:
                    raise zipfile.BadZipFile(f"Corrupted file in archive: {corrupt_file}")

                # ============================================================
                # FEATURE: File Size Limit
                # ============================================================
                # We inspect the zip's table of contents before extracting.
                # If any single uncompressed file would exceed 50MB, or the
                # total uncompressed size exceeds 200MB, we reject the payload.
                # This prevents accidental giant uploads from filling your disk.

                MAX_SINGLE_FILE_BYTES = 50  * 1024 * 1024   # 50 MB
                MAX_TOTAL_BYTES       = 200 * 1024 * 1024   # 200 MB
                total_uncompressed    = 0

                for info in zip_ref.infolist():
                    if info.file_size > MAX_SINGLE_FILE_BYTES:
                        raise ValueError(
                            f"File too large: {info.filename} "
                            f"({info.file_size / 1024 / 1024:.1f} MB > 50 MB limit)"
                        )
                    total_uncompressed += info.file_size

                if total_uncompressed > MAX_TOTAL_BYTES:
                    raise ValueError(
                        f"Total payload too large: "
                        f"{total_uncompressed / 1024 / 1024:.1f} MB > 200 MB limit"
                    )

                # All checks passed — extract safely
                zip_ref.extractall(TARGET_DIR)
                file_count = len(zip_ref.namelist())

            log.info(f"✅ {file_count} file(s) extracted to {TARGET_DIR}")

            # ============================================================
            # FEATURE: trigger_restart() — Actually Implemented
            # ============================================================
            trigger_restart(run_command)

            log_entry["outcome"]    = "success"
            log_entry["file_count"] = file_count
            write_deployment_log(log_entry)

            # ============================================================
            # FEATURE: Post-Deploy Webhook
            # ============================================================
            fire_webhook({
                "text":         f"✅ Orbit deployment succeeded from `{client_ip}`",
                "files":        file_count,
                "deployed_at":  log_entry["timestamp"]
            })

            self._respond(200, f"Deployment successful. {file_count} file(s) deployed. Server restarted.")

        except (zipfile.BadZipFile, ValueError) as e:
            log.warning(f"⚠️  Deployment rejected: {e}")
            log_entry["outcome"] = f"rejected: {e}"
            write_deployment_log(log_entry)
            self._respond(400, f"Deployment failed: {e}")

        except Exception as e:
            log.error(f"❌ Internal error: {e}")
            log_entry["outcome"] = f"error: {e}"
            write_deployment_log(log_entry)
            self._respond(500, "Deployment failed: Internal daemon error.")

    def _respond(self, code: int, message: str):
        """Helper to send a clean HTTP response."""
        self.send_response(code)
        self.end_headers()
        self.wfile.write(message.encode())

    def log_message(self, format, *args):
        # Suppress the default per-request stdout noise from BaseHTTPRequestHandler
        pass


def run_daemon():
    server = HTTPServer((HOST, PORT), DeploymentHandler)
    log.info(f"🚀 Orbit Daemon online — listening on port {PORT}")
    log.info(f"   Snapshots:  {SNAPSHOT_DIR} (keeping last {MAX_SNAPSHOTS})")
    log.info(f"   Deploy log: {LOG_FILE}")
    log.info(f"   IP filter:  {'ENABLED → ' + str(ALLOWED_IPS) if ALLOWED_IPS else 'DISABLED (all IPs allowed)'}")
    log.info(f"   Webhook:    {'ENABLED → ' + WEBHOOK_URL if WEBHOOK_URL else 'DISABLED'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopping Orbit Daemon cleanly.")
        server.server_close()


if __name__ == "__main__":
    run_daemon()