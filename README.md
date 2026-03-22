# DuepiRemoteHA

Control a Duepi pellet stove from Home Assistant via the [dpremoteiot.com](https://dpremoteiot.com) cloud API.

## Features

- **Native Climate entity** — full HVAC control with temperature and power level, using the stove's built-in temperature sensor
- **Auto-login** — authenticates with email/password, auto-renews the session when it expires (no more manual cookie extraction!)
- **Config flow UI** — set up entirely from the Home Assistant UI (Settings → Add Integration)
- **Rich entities** — climate, temperature sensor, power level, status text, online/offline connectivity, power slider
- **HACS compatible** — install easily via HACS custom repository
- **Reauth flow** — automatically prompts for new credentials if authentication fails
- **Diagnostics** — built-in diagnostic dump for troubleshooting

## Prerequisites

- A Duepi Remote Wifi module connected to your stove and registered on dpremoteiot.com.
  Examples: [poelediscount.com](https://www.poelediscount.com/modules-wifi-et-thermostats-connectes/3320-module-wifi-duepi-interstoves.html), [lafrancaisedupoele.fr](https://www.lafrancaisedupoele.fr/accueil/481-module-wi-fi-duepi.html)
- An account on [dpremoteiot.com](https://dpremoteiot.com) with your stove added.
- Home Assistant 2024.1.0 or later.

![Account and Device ID](screenshots/AccountAndDeviceID.png)

## Installation

### Via HACS (recommended)

[HACS](https://hacs.xyz/) (Home Assistant Community Store) is the easiest way to install custom integrations.

> **Don't have HACS yet?** Follow the [official HACS installation guide](https://hacs.xyz/docs/use/) first.

1. In Home Assistant, go to **HACS → Integrations**.
2. Click the **⋮** (3-dot menu) in the top-right corner → **Custom repositories**.
3. In the dialog that opens:
   - **Repository:** `https://github.com/FlorianCasse/DuepiRemoteHA`
   - **Category:** select **Integration**
   - Click **Add**.
4. Close the dialog. Search for **Duepi Pellet Stove** in the HACS integration list.
5. Click **Download** and confirm.
6. **Restart Home Assistant** (Settings → System → Restart).

### Manual installation

Use this method if you don't want to use HACS.

1. Download or clone this repository.
2. Copy the entire `custom_components/duepi/` folder into your Home Assistant configuration directory:
   ```
   <your HA config>/
   └── custom_components/
       └── duepi/
           ├── __init__.py
           ├── manifest.json
           ├── api.py
           ├── climate.py
           ├── ...
   ```
   > **Tip:** The HA config directory is typically `/config/` if you use Home Assistant OS/Supervised, or `~/.homeassistant/` for Core installations.
3. **Restart Home Assistant** (Settings → System → Restart).

## Setup

Once the integration is installed and HA has restarted:

### Step 1 — Find your Device ID

Before adding the integration, you need your **Device ID** from dpremoteiot.com:

1. Log in to [dpremoteiot.com](https://dpremoteiot.com).
2. On the dashboard, find your stove and look for the **delete device** button.
3. Inspect the button's link — it contains `deviceid=YOUR_DEVICE_ID`.

![Account and Device ID](screenshots/AccountAndDeviceID.png)

### Step 2 — Add the integration

1. Go to **Settings → Devices & Services**.
2. Click **+ Add Integration** (bottom-right).
3. Search for **Duepi Pellet Stove** and select it.
4. Fill in the form:

| Field | Description |
|---|---|
| **Email** | Your dpremoteiot.com account email |
| **Password** | Your dpremoteiot.com account password |
| **Device ID** | The device ID you found in Step 1 |

5. Click **Submit**. The integration will validate your credentials by logging in to dpremoteiot.com.
6. If successful, a new **Duepi Pellet Stove** device appears with all its entities.

> **Authentication failed?** Double-check your email and password. Make sure you can log in to [dpremoteiot.com](https://dpremoteiot.com) with the same credentials in a browser.

### Step 3 — Configure options (optional)

1. Go to **Settings → Devices & Services → Duepi Pellet Stove**.
2. Click **Configure**.

| Option | Default | Description |
|---|---|---|
| Update interval | 120s | How often to poll the stove state (min: 30s) |
| Default power | 5 | Power level used when turning on (1-5) |
| Default temperature | 25°C | Temperature used when turning on (0-35°C) |

## Entities

All entities are grouped under a single **Duepi Pellet Stove** device:

| Entity | Type | Description |
|---|---|---|
| Duepi Pellet Stove | Climate | Main control — HVAC mode (heat/off), target temperature, fan mode (power 1-5) |
| Room temperature | Sensor | Current room temperature from the stove's built-in sensor (°C) |
| Power level | Sensor | Current working power level (1-5) |
| Status | Sensor | Status text (Heating, Idle, Standby, Off...) |
| Set temperature | Sensor | Current target temperature (°C) |
| Online | Binary sensor | Whether the stove is reachable via dpremoteiot.com |
| Power level | Number | Slider to adjust power level (1-5) |

## Migration from Script-Based Setup

If you were using the previous `stoveOnOff.py` + `command_line` setup:

1. Install the custom integration (see above).
2. Add it via the UI with your dpremoteiot.com email and password.
3. Remove from your `configuration.yaml`:
   - The `command_line` switch and sensor entries
   - The `generic_thermostat` climate entry (if used)
4. Delete `/config/scripts/stoveOnOff.py` and `/config/scripts/.env` from your HA instance.
5. Update any automations:
   - Replace `switch.pellet_stove` → use the new `climate.duepi_pellet_stove` entity
   - Replace `sensor.pellet_stove_info` attributes → use the individual sensor entities
6. Restart Home Assistant.

The old script files are preserved in the `legacy/` folder for reference.

## Known Limitations

- **Power level may be ignored** — some users report that dpremoteiot.com always starts the stove at power 3 regardless of `settedPower`. This is a server-side limitation, not a bug in this integration.
- **Cloud dependency** — requires internet connectivity (communicates via dpremoteiot.com, not locally).
- **HTML scraping** — state detection relies on parsing the dashboard HTML. If dpremoteiot.com changes their layout, the integration may need updating.
