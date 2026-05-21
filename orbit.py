import os
import io
import sys
import json
import hashlib
import zipfile
import argparse
import requests
from pathlib import Path
from tqdm import tqdm

# ============================================================
# FEATURE: Deployment Profiles via orbit.config.json
# ============================================================
# Instead of hardcoding your IP/token in the script, we load
# them from a config file. This means you can have multiple
# profiles (e.g. "tablet", "server", "staging") and switch
# between them with --profile. No more editing source code!
#
# Example orbit.config.json:
# {
#   "default": {
#     "ip": "192.168.1.71",
#     "port": "8080",
#     "token": "NyxPass123"
#   },
#   "staging": {
#     "ip": "192.168.1.99",
#     "port": "9090",
#     "token": "StagingToken456"
#   }
# }

def load_config(profile: str) -> dict:
    config_path = Path("orbit.config.json")
    example_path = Path("orbit.config.json.example")
    
    if not config_path.exists():
        print("❌ Error: 'orbit.config.json' not found!")
        if example_path.exists():
            print("💡 Setup Hint: Copy 'orbit.config.json.example' to 'orbit.config.json' and fill in your device IPs/Tokens.")
        else:
            print("💡 Setup Hint: Create an 'orbit.config.json' file in the root directory to manage your deployment profiles.")
        print("🛑 Aborting execution.")
        sys.exit(1)  # Cleanly halt the script
        
    try:
        with open(config_path, "r") as f:
            full_config = json.load(f)
            
        if profile not in full_config:
            print(f"❌ Error: Profile '{profile}' not found inside orbit.config.json.")
            print(f"Available profiles: {list(full_config.keys())}")
            sys.exit(1)
            
        return full_config[profile]
        
    except json.JSONDecodeError:
        print("❌ Error: 'orbit.config.json' contains invalid JSON syntax!")
        sys.exit(1)
    print(f"✅ Loaded profile: '{profile}'")
    return config[profile]


# ============================================================
# FEATURE: .orbitignore Support
# ============================================================
# Just like .gitignore, you can drop a .orbitignore file in
# your project root to list folders/files to exclude from the
# zip. This keeps the ignore list out of your source code and
# makes it easy to customize per project.

def load_orbitignore() -> set:
    base_ignored = {".git", "__pycache__", "node_modules", "dist", "build", "deployed_app"}
    orbitignore_path = Path(".orbitignore")
    if orbitignore_path.exists():
        with open(orbitignore_path) as f:
            extra = {line.strip() for line in f if line.strip() and not line.startswith("#")}
        print(f"📄 .orbitignore loaded: {extra}")
        return base_ignored | extra
    return base_ignored


# ============================================================
# FEATURE: Pre-Deploy Health Check
# ============================================================
# Before we waste time zipping up your whole project, we ping
# the daemon first. If it's unreachable, we fail immediately
# with a clear message instead of timing out after a big upload.
# The daemon's /health endpoint responds to GET requests.

def health_check(daemon_url: str, token: str) -> bool:
    print(f"🩺 Pinging daemon at {daemon_url}/health ...")
    try:
        r = requests.get(
            f"{daemon_url}/health",
            headers={"X-Orbit-Token": token},
            timeout=5
        )
        if r.status_code == 200:
            print("✅ Daemon is alive and ready.")
            return True
        else:
            print(f"❌ Daemon responded with status {r.status_code}.")
            return False
    except requests.exceptions.RequestException as e:
        print(f"❌ Could not reach daemon: {e}")
        return False


# ============================================================
# FEATURE: Delta / Diff Deployments
# ============================================================
# Instead of rezipping your entire workspace every time, we
# compute a SHA256 hash of each file and compare it against
# a local cache file (orbit_hashes.json). Only files that
# have changed since the last successful deploy get packaged.
# This makes repeated deployments dramatically faster.

HASH_CACHE_FILE = Path("orbit_hashes.json")

def compute_file_hash(file_path: str) -> str:
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def load_hash_cache() -> dict:
    if HASH_CACHE_FILE.exists():
        with open(HASH_CACHE_FILE) as f:
            return json.load(f)
    return {}

def save_hash_cache(cache: dict):
    with open(HASH_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


# ============================================================
# FEATURE: Checksum Verification
# ============================================================
# After building the zip in memory, we compute its SHA256 hash
# and send it in the request header as X-Orbit-Checksum.
# The daemon independently computes the hash of what it received
# and rejects the payload if they don't match. This protects
# against corruption during transmission over shaky networks.

def compute_zip_checksum(zip_data: bytes) -> str:
    return hashlib.sha256(zip_data).hexdigest()


def package_and_deploy(profile: str, dry_run: bool, delta: bool):
    config = load_config(profile)
    TARGET_IP    = config["ip"]
    TARGET_PORT  = config["port"]
    SECRET_TOKEN = config["token"]
    daemon_url   = f"http://{TARGET_IP}:{TARGET_PORT}"
    current_dir  = os.getcwd()

    # ============================================================
    # FEATURE: Pre-Deploy Health Check (applied here)
    # ============================================================
    if not dry_run:
        if not health_check(daemon_url, SECRET_TOKEN):
            print("🛑 Aborting deployment. Fix connectivity before retrying.")
            sys.exit(1)

    print(f"\n📦 Scanning workspace: {current_dir}")

    ignored_items   = load_orbitignore()
    skipped_files   = {"orbit.py", "orbit_daemon.py", "Project_Pulse.exe",
                       "config.ini", "orbit.config.json", "orbit_hashes.json",
                       ".orbitignore"}

    # ============================================================
    # FEATURE: Delta Deployments (applied here)
    # ============================================================
    hash_cache     = load_hash_cache() if delta else {}
    new_hash_cache = {}
    changed_files  = []

    # First pass: discover which files are new or changed
    for root, dirs, files in os.walk(current_dir):
        dirs[:] = [d for d in dirs if d not in ignored_items]
        for file in files:
            if file in skipped_files:
                continue
            file_path    = os.path.join(root, file)
            relative_path = os.path.relpath(file_path, current_dir)
            file_hash    = compute_file_hash(file_path)
            new_hash_cache[relative_path] = file_hash

            if delta and hash_cache.get(relative_path) == file_hash:
                print(f"   ⏭️  Unchanged (skipped): {relative_path}")
            else:
                changed_files.append((file_path, relative_path))

    if not changed_files:
        print("\n✨ No changes detected since last deployment. Nothing to send.")
        return

    print(f"\n⚡ Compressing {len(changed_files)} file(s) into memory stream...")

    # ============================================================
    # FEATURE: Deployment Manifest
    # ============================================================
    # We build a deployment.manifest.json and include it INSIDE
    # the zip itself. When the daemon unpacks the archive, this
    # file lands in the deployed_app folder and gives you a clear
    # audit trail: when it was deployed, from where, and what changed.

    import time
    manifest = {
        "deployed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": profile,
        "delta_mode": delta,
        "files": [rp for _, rp in changed_files]
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for file_path, relative_path in changed_files:
            zip_file.write(file_path, relative_path)
            print(f"   ➔ Packaged: {relative_path}")
        # Inject the manifest into the zip
        zip_file.writestr("deployment.manifest.json", manifest_bytes)

    zip_buffer.seek(0)
    zip_data = zip_buffer.getvalue()

    # ============================================================
    # FEATURE: Checksum (computed here, sent in header below)
    # ============================================================
    # Ensure we capture the exact byte value of the zip buffer,
    # then calculate the SHA256 string cleanly from the raw bytes.
    zip_data = zip_buffer.getvalue()
    payload_checksum = hashlib.sha256(zip_data).hexdigest()
    print(f"\n🔐 Payload checksum (SHA256): {payload_checksum[:16]}...")
    print(f"✨ Payload ready. Total size: {len(zip_data) / 1024:.2f} KB")

    # ============================================================
    # FEATURE: Dry-Run Mode
    # ============================================================
    # --dry-run stops here. You get a full preview of everything
    # that WOULD happen (files packaged, sizes, target) without
    # actually sending anything to the daemon. Great for sanity-
    # checking before a big deploy.

    if dry_run:
        print("\n🔵 [DRY RUN] Simulation complete. No data was transmitted.")
        print(f"   Would deploy {len(changed_files)} file(s) to {daemon_url}")
        print(f"   Checksum would be: {payload_checksum}")
        return

    # Headers cleanly pulled from the loaded config profile.
    custom_headers = {
        "X-Orbit-Token":    SECRET_TOKEN,       # ← from orbit.config.json
        "X-Orbit-Checksum": payload_checksum,   # ← daemon will verify this
        "Content-Type":     "application/zip"
    }

    try:
        # Pass zip_data directly (raw bytes) to keep the pipeline stable
        # and avoid any stream-header corruption that chunked generators
        # can introduce. Swap back to tqdm once the raw pipeline is verified.
        print(f"\n🌐 Initiating secure handshake with deployment daemon...")
        response = requests.post(
            daemon_url,
            data=zip_data,
            headers=custom_headers,
            timeout=15
        )

        if response.status_code == 200:
            print("\n🟢 [SUCCESS] Deployment fully processed by remote daemon!")
            print(f"🖥️  Server Response: {response.text}")
            # Only update our local hash cache after a confirmed successful deploy
            save_hash_cache(new_hash_cache)
            print("💾 Local file hash cache updated for next delta deploy.")
        else:
            print(f"\n🔴 [FAILURE] Transmission rejected.")
            print(f"⚠️  Status: {response.status_code} | Details: {response.text}")

    except requests.exceptions.RequestException as e:
        print(f"\n❌ Network error: Could not reach Orbit Daemon. Details: {e}")


if __name__ == "__main__":
    # ============================================================
    # FEATURE: CLI Flags via argparse
    # ============================================================
    # All the features above are now wired to clean CLI flags:
    #
    #   python orbit.py                          # standard deploy, default profile
    #   python orbit.py --dry-run                # simulate, don't send
    #   python orbit.py --delta                  # only send changed files
    #   python orbit.py --profile staging        # use a different config profile
    #   python orbit.py --delta --profile tablet # combine flags freely

    parser = argparse.ArgumentParser(description="Orbit Deployment Tool")
    parser.add_argument("--profile",  default="default",        help="Config profile to use from orbit.config.json")
    parser.add_argument("--dry-run",  action="store_true",       help="Simulate deployment without sending data")
    parser.add_argument("--delta",    action="store_true",       help="Only deploy files changed since last deploy")
    args = parser.parse_args()

    try:
        package_and_deploy(
            profile=args.profile,
            dry_run=args.dry_run,
            delta=args.delta
        )
    except KeyboardInterrupt:
        print("\nDeployment aborted by developer request.")