#!/usr/bin/env python3
"""Migration script from legacy stoveOnOff.py setup to the new Duepi custom integration.

Usage:
  Migrate (local or remote):
    python3 migrate.py
    python3 migrate.py --ssh user@homeassistant
    python3 migrate.py --ssh user@192.168.1.100 --port 22222

  Rollback:
    python3 migrate.py --rollback
    python3 migrate.py --rollback --ssh root@homeassistant

  Options:
    --ssh HOST        SSH destination (user@host)
    --port PORT       SSH port (default: 22)
    --key PATH        Path to SSH private key
    --config PATH     HA config directory (default: /config)
    --rollback        Restore old files from backup and remove the custom integration
    --no-interactive  Skip confirmation prompts (auto-yes)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ANSI colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_step(n: int, msg: str) -> None:
    print(f"\n{BOLD}{CYAN}[Step {n}]{RESET} {msg}")


def print_ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def print_warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{RESET} {msg}")


def print_err(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


# ---------------------------------------------------------------------------
# SSH remote execution layer
# ---------------------------------------------------------------------------

class RemoteExecutor:
    """Execute file operations on a remote HA instance via SSH."""

    def __init__(self, ssh_host: str, ssh_port: int = 22, ssh_key: str | None = None) -> None:
        self._host = ssh_host
        self._base_cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new"]
        if ssh_port != 22:
            self._base_cmd += ["-p", str(ssh_port)]
        if ssh_key:
            self._base_cmd += ["-i", ssh_key]
        self._base_cmd.append(ssh_host)

    def _run(self, cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self._base_cmd, cmd],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_connection(self) -> bool:
        try:
            result = self._run("echo ok")
            return result.returncode == 0 and "ok" in result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def file_exists(self, path: str) -> bool:
        return self._run(f"test -f {path} && echo yes || echo no").stdout.strip() == "yes"

    def dir_exists(self, path: str) -> bool:
        return self._run(f"test -d {path} && echo yes || echo no").stdout.strip() == "yes"

    def read_file(self, path: str) -> str | None:
        result = self._run(f"cat {path} 2>/dev/null")
        if result.returncode != 0:
            return None
        return result.stdout

    def mkdir(self, path: str) -> None:
        self._run(f"mkdir -p {path}")

    def copy(self, src: str, dst: str) -> None:
        self._run(f"cp -p {src} {dst}")

    def remove(self, path: str) -> None:
        self._run(f"rm -f {path}")

    def remove_dir(self, path: str) -> None:
        self._run(f"rm -rf {path}")


class LocalExecutor:
    """Execute file operations locally."""

    def test_connection(self) -> bool:
        return True

    def file_exists(self, path: str) -> bool:
        return Path(path).is_file()

    def dir_exists(self, path: str) -> bool:
        return Path(path).is_dir()

    def read_file(self, path: str) -> str | None:
        p = Path(path)
        if not p.is_file():
            return None
        return p.read_text()

    def mkdir(self, path: str) -> None:
        Path(path).mkdir(parents=True, exist_ok=True)

    def copy(self, src: str, dst: str) -> None:
        shutil.copy2(src, dst)

    def remove(self, path: str) -> None:
        p = Path(path)
        if p.is_file():
            p.unlink()

    def remove_dir(self, path: str) -> None:
        p = Path(path)
        if p.is_dir():
            shutil.rmtree(p)


# ---------------------------------------------------------------------------
# Migration logic
# ---------------------------------------------------------------------------

def load_env_from_content(content: str) -> dict[str, str]:
    """Parse .env file content into a dict."""
    env: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value:
            env[key] = value
    return env


def detect_old_yaml_entries(content: str) -> dict[str, list[str]]:
    """Detect old Duepi entries in configuration.yaml content."""
    found: dict[str, list[str]] = {"command_line": [], "climate": []}

    for match in re.finditer(
        r"^.*stoveOnOff\.py.*$", content, re.MULTILINE | re.IGNORECASE
    ):
        found["command_line"].append(match.group(0).strip())

    for match in re.finditer(
        r"^.*(?:generic_thermostat|pellet|stove|poele).*$",
        content,
        re.MULTILINE | re.IGNORECASE,
    ):
        line = match.group(0).strip()
        if line not in found["command_line"]:
            found["climate"].append(line)

    return found


def run_rollback(executor: LocalExecutor | RemoteExecutor, ha_config: str, interactive: bool) -> None:
    """Restore backed-up files and optionally remove the new integration."""
    backup_dir = f"{ha_config}/duepi_migration_backup"
    scripts_dir = f"{ha_config}/scripts"
    integration_dir = f"{ha_config}/custom_components/duepi"

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Duepi Pellet Stove — Rollback{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")

    is_remote = isinstance(executor, RemoteExecutor)
    if is_remote:
        print(f"  Mode: {CYAN}SSH remote{RESET} → {executor._host}")
    else:
        print(f"  Mode: {CYAN}Local{RESET}")

    # --- Step 1: Check backup exists ---
    print_step(1, "Checking backup")

    has_backup_script = executor.file_exists(f"{backup_dir}/stoveOnOff.py")
    has_backup_env = executor.file_exists(f"{backup_dir}/.env")

    if not has_backup_script and not has_backup_env:
        print_err(f"No backup found at {backup_dir}/")
        print_warn("Nothing to rollback. The backup is created during migration (step 5).")
        return

    if has_backup_script:
        print_ok(f"Found backup: {backup_dir}/stoveOnOff.py")
    if has_backup_env:
        print_ok(f"Found backup: {backup_dir}/.env")

    # --- Step 2: Restore files ---
    print_step(2, "Restoring old files")

    proceed = True
    if interactive:
        print(f"\n  This will:")
        print(f"    - Restore stoveOnOff.py and .env to {scripts_dir}/")
        print(f"    - Remove the custom integration at {integration_dir}/")
        print(f"    - Remove the backup directory")
        answer = input(f"\n  {BOLD}Proceed with rollback? [y/N]{RESET} ").strip().lower()
        proceed = answer in ("y", "yes")

    if not proceed:
        print_warn("Rollback cancelled.")
        return

    executor.mkdir(scripts_dir)

    for filename in ["stoveOnOff.py", ".env"]:
        src = f"{backup_dir}/{filename}"
        dst = f"{scripts_dir}/{filename}"
        if executor.file_exists(src):
            executor.copy(src, dst)
            print_ok(f"Restored: {src} → {dst}")

    # --- Step 3: Remove new integration ---
    print_step(3, "Removing new custom integration")

    if executor.dir_exists(integration_dir):
        executor.remove_dir(integration_dir)
        print_ok(f"Removed: {integration_dir}/")
    else:
        print_ok("Custom integration not installed (nothing to remove)")

    # --- Step 4: Clean up backup ---
    print_step(4, "Cleaning up backup")

    if executor.dir_exists(backup_dir):
        executor.remove_dir(backup_dir)
        print_ok(f"Removed backup directory: {backup_dir}/")

    # --- Step 5: Next steps ---
    print_step(5, "Next steps")

    print(f"""
  {BOLD}To complete the rollback:{RESET}

  1. {BOLD}Re-add old YAML configuration{RESET} to your configuration.yaml:
     - The {CYAN}command_line{RESET} switch and sensor entries
     - The {CYAN}generic_thermostat{RESET} climate entry (if you had one)
     (See the legacy/ folder in the repo for reference configs)

  2. {BOLD}Remove the Duepi integration{RESET} from Home Assistant (if added):
     Settings → Devices & Services → Duepi Pellet Stove → Delete

  3. {BOLD}Restart Home Assistant{RESET}
     Settings → System → Restart

  4. {BOLD}Verify{RESET} the old stove switch is back:
     Check that {CYAN}switch.pellet_stove{RESET} works correctly
""")

    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"{GREEN}{BOLD}  Rollback complete!{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")


def run_migration(executor: LocalExecutor | RemoteExecutor, ha_config: str, interactive: bool) -> None:
    old_script = f"{ha_config}/scripts/stoveOnOff.py"
    old_env = f"{ha_config}/scripts/.env"
    config_yaml = f"{ha_config}/configuration.yaml"
    integration_init = f"{ha_config}/custom_components/duepi/__init__.py"
    backup_dir = f"{ha_config}/duepi_migration_backup"

    print(f"\n{BOLD}{'=' * 60}{RESET}")
    print(f"{BOLD}  Duepi Pellet Stove — Migration Script{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}")

    is_remote = isinstance(executor, RemoteExecutor)
    if is_remote:
        print(f"  Mode: {CYAN}SSH remote{RESET} → {executor._host}")
    else:
        print(f"  Mode: {CYAN}Local{RESET}")
    print(f"  HA config: {CYAN}{ha_config}{RESET}")

    # --- Step 1: Detect old installation ---
    print_step(1, "Detecting old installation")

    has_script = executor.file_exists(old_script)
    has_env = executor.file_exists(old_env)

    if not has_script and not has_env:
        print_warn(f"No old installation found (no stoveOnOff.py or .env in {ha_config}/scripts/)")
    else:
        if has_script:
            print_ok(f"Found old script: {old_script}")
        if has_env:
            print_ok(f"Found old .env: {old_env}")

    # --- Step 2: Read old credentials ---
    print_step(2, "Reading old credentials")

    env: dict[str, str] = {}
    if has_env:
        env_content = executor.read_file(old_env)
        if env_content:
            env = load_env_from_content(env_content)

    device_id = env.get("DUEPI_DEVICE_ID", "")
    session_cookie = env.get("DUEPI_SESSION_COOKIE", "")
    default_power = env.get("DUEPI_SETTED_POWER", "5")
    default_temp = env.get("DUEPI_SETTED_TEMPERATURE", "25")

    if device_id:
        print_ok(f"Device ID: {device_id[:12]}...")
    else:
        print_warn("No DUEPI_DEVICE_ID found in .env")

    if session_cookie:
        print_ok("Session cookie found (will NOT be migrated — new integration uses email/password)")
    else:
        print_warn("No session cookie found")

    print_ok(f"Default power: {default_power}")
    print_ok(f"Default temperature: {default_temp}°C")

    # --- Step 3: Scan configuration.yaml ---
    print_step(3, "Scanning configuration.yaml for old entries")

    yaml_content = executor.read_file(config_yaml)
    if yaml_content:
        yaml_entries = detect_old_yaml_entries(yaml_content)
    else:
        yaml_entries = {"command_line": [], "climate": []}
        print_warn(f"Could not read {config_yaml}")

    if yaml_entries["command_line"]:
        print_warn("Found command_line references to stoveOnOff.py:")
        for line in yaml_entries["command_line"]:
            print(f"    {RED}{line}{RESET}")
    else:
        print_ok("No command_line references to stoveOnOff.py found")

    if yaml_entries["climate"]:
        print_warn("Found possible thermostat/stove entries:")
        for line in yaml_entries["climate"]:
            print(f"    {YELLOW}{line}{RESET}")
    else:
        print_ok("No generic_thermostat stove entries found")

    # --- Step 4: Check new integration ---
    print_step(4, "Checking new custom integration")

    if executor.file_exists(integration_init):
        print_ok("New integration already installed at custom_components/duepi/")
    else:
        print_err("New integration NOT found at custom_components/duepi/")
        print_warn("Install it first via HACS or manually before continuing.")
        print_warn("See: https://github.com/FlorianCasse/DuepiRemoteHA#installation")

    # --- Step 5: Backup & cleanup ---
    print_step(5, "Backup and cleanup")

    if not has_script and not has_env:
        print_ok("Nothing to back up (old files already removed)")
    else:
        print(f"\n  The following files will be backed up to {backup_dir}/")
        if has_script:
            print(f"    - {old_script}")
        if has_env:
            print(f"    - {old_env}")

        proceed = True
        if interactive:
            answer = input(f"\n  {BOLD}Proceed with backup and removal? [y/N]{RESET} ").strip().lower()
            proceed = answer in ("y", "yes")

        if proceed:
            executor.mkdir(backup_dir)

            for src in [old_script, old_env]:
                if executor.file_exists(src):
                    filename = src.rsplit("/", 1)[-1]
                    dst = f"{backup_dir}/{filename}"
                    executor.copy(src, dst)
                    print_ok(f"Backed up: {src} → {dst}")

            for src in [old_script, old_env]:
                if executor.file_exists(src):
                    executor.remove(src)
                    print_ok(f"Removed: {src}")
        else:
            print_warn("Skipped — you can clean up manually later.")

    # --- Step 6: Summary & next steps ---
    print_step(6, "Next steps")

    has_yaml_to_clean = yaml_entries["command_line"] or yaml_entries["climate"]

    print(f"""
  {BOLD}To complete the migration:{RESET}

  1. {BOLD}Edit configuration.yaml{RESET} and remove the old entries:""")

    if has_yaml_to_clean:
        print(f"""     - Remove the {CYAN}command_line{RESET} switch and sensor blocks referencing stoveOnOff.py
     - Remove the {CYAN}generic_thermostat{RESET} climate block for the stove (if present)""")
    else:
        print(f"     {GREEN}✓ No old entries detected (already clean or in separate files){RESET}")

    print(f"""
  2. {BOLD}Restart Home Assistant{RESET}
     Settings → System → Restart

  3. {BOLD}Add the new integration{RESET}
     Settings → Devices & Services → + Add Integration → "Duepi Pellet Stove"

  4. {BOLD}Enter your credentials:{RESET}
     - Email: your dpremoteiot.com email
     - Password: your dpremoteiot.com password""")

    if device_id:
        print(f"     - Device ID: {CYAN}{device_id}{RESET}")
    else:
        print("     - Device ID: find it on the dpremoteiot.com dashboard")

    print(f"""
  5. {BOLD}Update automations{RESET} (if any):
     - {RED}switch.pellet_stove{RESET}        → {GREEN}climate.duepi_pellet_stove{RESET}
     - {RED}sensor.pellet_stove_info{RESET}   → individual sensors:
       • {GREEN}sensor.duepi_pellet_stove_room_temperature{RESET}
       • {GREEN}sensor.duepi_pellet_stove_power_level{RESET}
       • {GREEN}sensor.duepi_pellet_stove_status{RESET}
       • {GREEN}binary_sensor.duepi_pellet_stove_online{RESET}

  6. {BOLD}Configure options{RESET} (optional):
     Settings → Devices & Services → Duepi Pellet Stove → Configure
     - Update interval: default 120s (was 300s)
     - Default power: {default_power}
     - Default temperature: {default_temp}°C
""")

    print(f"{BOLD}{'=' * 60}{RESET}")
    print(f"{GREEN}{BOLD}  Migration preparation complete!{RESET}")
    print(f"{BOLD}{'=' * 60}{RESET}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate from legacy stoveOnOff.py to the Duepi custom integration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 migrate.py                              # Local (on HA instance)
  python3 migrate.py --ssh root@192.168.1.100     # Remote via SSH
  python3 migrate.py --ssh root@ha --port 22222   # Custom SSH port
  python3 migrate.py --ssh root@ha --key ~/.ssh/id # With SSH key
  python3 migrate.py --ssh root@ha --no-interactive # No prompts
  python3 migrate.py --rollback                   # Rollback locally
  python3 migrate.py --rollback --ssh root@ha     # Rollback via SSH
""",
    )
    parser.add_argument(
        "--ssh",
        metavar="USER@HOST",
        help="SSH destination for remote execution (e.g. root@192.168.1.100)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=22,
        help="SSH port (default: 22)",
    )
    parser.add_argument(
        "--key",
        metavar="PATH",
        help="Path to SSH private key",
    )
    parser.add_argument(
        "--config",
        default="/config",
        help="HA config directory on the target (default: /config)",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Restore old files from backup and remove the custom integration",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Skip confirmation prompts (auto-yes for backup/removal)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    interactive = not args.no_interactive

    if args.ssh:
        print(f"\n  Connecting to {BOLD}{args.ssh}{RESET} (port {args.port})...")
        executor = RemoteExecutor(args.ssh, args.port, args.key)
        if not executor.test_connection():
            print_err(f"Cannot connect to {args.ssh} via SSH.")
            print_warn("Check that:")
            print_warn(f"  - The host is reachable: ssh {args.ssh}")
            print_warn(f"  - SSH port is correct (--port {args.port})")
            if args.key:
                print_warn(f"  - SSH key exists: {args.key}")
            else:
                print_warn("  - Your SSH key is loaded (ssh-add) or use --key")
            sys.exit(1)
        print_ok(f"Connected to {args.ssh}")
    else:
        executor = LocalExecutor()

    if args.rollback:
        run_rollback(executor, args.config, interactive)
    else:
        run_migration(executor, args.config, interactive)


if __name__ == "__main__":
    main()
