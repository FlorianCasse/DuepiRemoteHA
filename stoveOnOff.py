#!/usr/bin/env python3

import sys
import os
import logging
import time
import re
import urllib.parse
import requests


# --- .env loader (no external dependency) ---
def _load_dotenv() -> None:
    """Load .env file from the same directory as this script."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_dotenv()

# --- Configuration from environment ---
DEVICE_ID: str = os.environ.get('DUEPI_DEVICE_ID', '')
SHORT_ID: str = os.environ.get('DUEPI_SHORT_ID', '')
RAW_COOKIE: str = os.environ.get('DUEPI_SESSION_COOKIE', '')
SETTED_POWER: str = os.environ.get('DUEPI_SETTED_POWER', '5')
SETTED_TEMPERATURE: str = os.environ.get('DUEPI_SETTED_TEMPERATURE', '25')

# --- Logging to stderr (won't interfere with HA stdout capture) ---
log_level = os.environ.get('DUEPI_LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stderr,
)
logger = logging.getLogger('duepi')

# --- Cookie handling ---
if RAW_COOKIE.startswith('session='):
    RAW_COOKIE = RAW_COOKIE[8:]
SESSION_COOKIE: str = urllib.parse.unquote(RAW_COOKIE)

URL_DASHBOARD = 'https://dpremoteiot.com/dashboard'
URL_SET = 'https://dpremoteiot.com/devices/setSettings'

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36',
    'Referer': 'https://dpremoteiot.com/dashboard',
    'Origin': 'https://dpremoteiot.com',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Upgrade-Insecure-Requests': '1',
}


def _create_session() -> requests.Session:
    """Create a configured requests session with headers and cookies."""
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.set('session', SESSION_COOKIE)
    return session


def _request_with_retry(func, *args, retries: int = 1, delay: float = 2.0, **kwargs):
    """Execute a request function with a single retry on transient network errors."""
    for attempt in range(1 + retries):
        try:
            return func(*args, **kwargs)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < retries:
                logger.warning("Transient error (%s), retrying in %.0fs...", e, delay)
                time.sleep(delay)
            else:
                raise


def get_status_json() -> None:
    """Fetch stove status from the dashboard and print '1' (ON) or '0' (OFF) to stdout."""
    logger.info("Checking stove status")
    try:
        with _create_session() as session:
            response = _request_with_retry(session.get, URL_DASHBOARD, timeout=15)
            response.raise_for_status()

        html_content = response.text
        logger.debug("Dashboard response length: %d chars", len(html_content))

        pattern = rf"{re.escape(DEVICE_ID)}.*?powerState.*?((?:ON)|(?:OFF))"
        match = re.search(pattern, html_content, re.DOTALL | re.IGNORECASE)

        if match:
            state = match.group(1).upper()
            logger.info("Stove state detected (primary pattern): %s", state)
            print("1" if state == "ON" else "0")
        else:
            backup_pattern = rf"{re.escape(SHORT_ID)}.*?Power Status\s*:\s*(ON|OFF)"
            backup_match = re.search(backup_pattern, html_content, re.DOTALL | re.IGNORECASE)
            if backup_match:
                state = backup_match.group(1).upper()
                logger.info("Stove state detected (backup pattern): %s", state)
                print("1" if state == "ON" else "0")
            else:
                logger.warning("Could not find power state in dashboard HTML")
                print("0")

    except requests.exceptions.Timeout:
        logger.error("Dashboard request timed out")
        print("0")
    except requests.exceptions.ConnectionError:
        logger.error("Cannot reach dpremoteiot.com")
        print("0")
    except requests.exceptions.HTTPError as e:
        logger.error("HTTP error: %s", e)
        print("0")
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        print("0")


def control_stove(command: str) -> None:
    """Send ON or OFF command to the stove."""
    is_on = command.lower() == 'on'
    target_active = '1' if is_on else '0'
    target_switch = 'on' if is_on else 'off'

    logger.info("Sending command: %s (active=%s, switch=%s)", command, target_active, target_switch)

    data = {
        'deviceId': DEVICE_ID,
        'active': target_active,
        'emailNotifications': '0',
        'settedPower': SETTED_POWER,
        'settedTemperature': SETTED_TEMPERATURE,
        'switch': target_switch,
    }

    try:
        with _create_session() as session:
            response = _request_with_retry(session.post, URL_SET, data=data, timeout=10)
            response.raise_for_status()
        logger.info("Command '%s' sent successfully (HTTP %d)", command, response.status_code)

    except requests.exceptions.Timeout:
        logger.error("Control request timed out")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        logger.error("Cannot reach dpremoteiot.com")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        logger.error("HTTP error: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Unexpected error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1].lower() not in ('status', 'on', 'off'):
        print(f"Usage: {sys.argv[0]} <status|on|off>", file=sys.stderr)
        sys.exit(1)

    if not DEVICE_ID or not SESSION_COOKIE:
        logger.error("Missing credentials: set DUEPI_DEVICE_ID and DUEPI_SESSION_COOKIE "
                      "(via environment or .env file)")
        print("0")
        sys.exit(1)

    action = sys.argv[1].lower()
    logger.debug("Action: %s | Device: %s", action, DEVICE_ID)

    if action == 'status':
        get_status_json()
    elif action in ('on', 'off'):
        control_stove(action)
