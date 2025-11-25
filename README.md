# AIS + ADS-B → APRS Bridge with Dual GUI Monitors

This project combines:

- An **AIS → APRS** bridge (using AIS-catcher as a source)
- An **ADS-B → APRS** bridge (using `dump1090` as a source)

…into a single Python script that:

- Feeds **APRS object packets** into a local **APRSIS / APRSIS32 IS-Server**
- Shows **two live “feather” monitor windows** via Tkinter:
  - **AIS Monitor:** ships / base stations (MMSI, name, lat/lon, symbol, last seen, etc.)
  - **ADS-B Monitor:** aircraft (object name, ICAO, callsign, emitter category, aircraft type, lat/lon, altitude, groundspeed, track, last seen, etc.)

It’s designed for local integration with APRSIS32 on Windows, but will run anywhere Python 3 + Tkinter is available.

---

## Features

### AIS → APRS

- Listens for AIS NMEA (`!AIVDM` / `!AIVDO`) from **AIS-catcher** over TCP.
- Decodes:
  - Position reports: Types **1, 2, 3, 18, 19**
  - Base stations: Type **4**
  - Long range: Type **27**
  - Static/voyage: Types **5, 24** (for vessel names)
- Range filter around a configurable **center lat/lon**.
- Teleport filter to drop bogus jumps (e.g. 150+ nm in a few minutes).
- Sends **APRS object packets** to a local APRS-IS server.
- AIS GUI Monitor:
  - MMSI, vessel name (from static/voyage messages)
  - APRS symbol + description
  - Latitude / longitude
  - AIS message type
  - Last seen (UTC)
  - Updates every 1 second.

### ADS-B → APRS

- Connects to **dump1090** SBS TCP feed (`port 30003`).
- Periodically polls **dump1090 JSON** (`data.json`) for:
  - Emitter category (`category`)
  - Aircraft type (`type` / `t`)
  - Flight / callsign
- Smart logic:
  - Haversine distance from a home location (KBUF in this example).
  - **Add** objects inside `ADD_DISTANCE_MI`, **clear** when beyond `CLEAR_DISTANCE_MI`.
  - Minimum movement (in miles) and time between updates.
  - Landing suppression and time-based delete at low altitudes.
  - Object rename (hex → flight) when a callsign appears.
  - Object TTL cleanup with APRS object delete packets.
- ADS-B GUI Monitor:
  - Object name (9-char APRS object name)
  - ICAO hex
  - Callsign / flight
  - Emitter category (cat)
  - Aircraft type (type)
  - APRS symbol
  - Latitude / longitude
  - Altitude (ft), ground speed (kt), track (deg)
  - Last seen (UTC)
  - Updates every 1 second.

---

## Architecture

- **Single Python process**, **three threads**:
  - AIS bridge worker (blocking TCP server + APRS client)
  - ADS-B bridge worker (SBS client + JSON poll + APRS client)
  - Tkinter main loop (GUI) in the main thread
- Each bridge has its own APRS-IS connection (but shared config constants).
- Shared in-memory tables:
  - AIS: `vessels`, `name_cache`
  - ADS-B: `aircraft`, `meta_cache`
- Thread safety via `threading.Lock` for read/write access to tables.

---

## Requirements

### Software

- **Python 3.8+**
- Built-ins only; externally required libraries:
  - `tkinter` (for GUI)
  - Standard lib: `socket`, `threading`, `datetime`, `time`, `math`, `json`, `urllib.request`, `re`

On many Linux distros you may need to install Tkinter separately, for example:

```bash
# Debian/Ubuntu
sudo apt-get install python3-tk
````

### External Services

1. **APRS-IS / APRSIS32**

   * Local IS-Server listening on `127.0.0.1:14580` (configurable).
   * This script logs in using your callsign/SSID and a passcode (for local use, any int works).

2. **AIS-catcher**

   * Must be configured to output `!AIVDM/!AIVDO` NMEA sentences to a TCP listener.
   * Script defaults:

     * Listen on `0.0.0.0:10110` (changeable)
   * Example AIS-catcher command:

     ```bash
     ais-catcher -d 4 -n -o 1 -P 192.168.35.183 10110
     ```

     Adjust device index (`-d`), host, and port as needed.

3. **dump1090**

   * Must be running and providing:

     * SBS TCP feed on `port 30003` (configurable).
     * JSON API on `http://<host>:8080/data.json` (dump1090-fa style).
   * Script defaults:

     * `DUMP1090_HOST = "192.168.35.33"`
     * `DUMP1090_PORT = 30003`
     * JSON URL: `http://192.168.35.33:8080/data.json`

---

## Configuration

Edit the combined script and adjust the config constants at the top of each section.

### Shared APRS Settings

```python
APRSIS_IP       = "127.0.0.1"
APRSIS_TCP_PORT = 14580

CALLSIGN        = "YOURCALL"   # Your callsign-SSID
PASSCODE        = -1           # For local APRSIS32, any int is fine
```

### ADS-B Settings

```python
DUMP1090_HOST     = "192.168.35.33"
DUMP1090_PORT     = 30003
DUMP1090_JSON_URL = f"http://{DUMP1090_HOST}:8080/data.json"

# Center & ranges
KBUF_LAT          = 42.9405
KBUF_LON          = -78.7322
ADD_DISTANCE_MI   = 35
CLEAR_DISTANCE_MI = 40

# Update & landing behavior, etc.
MIN_UPDATE_SEC   = 5
MIN_MOVE_MI      = 0.50
OBJECT_TTL_SEC   = 300
LANDED_ALT_FT    = 1000
LANDED_WAIT_SEC  = 180
LAND_CLEAR_ALT   = 1500
```

### AIS Settings

```python
AIS_LISTEN_IP   = "0.0.0.0"
AIS_LISTEN_PORT = 10110

CENTER_LAT        = 42.9
CENTER_LON        = -78.9
MAX_RANGE_NM      = 250.0

TELEPORT_MOVE_NM  = 150.0
TELEPORT_TIME_SEC = 900
```

---

## Running

1. Make sure APRSIS32 (or an APRS-IS server) is running and listening on the configured IP/port.
2. Start **dump1090** and verify:

   * SBS feed on `port 30003`
   * JSON at `/data.json`
3. Start **AIS-catcher** with TCP output to match `AIS_LISTEN_IP` and `AIS_LISTEN_PORT`.
4. Run the script:

```bash
python3 combined_ais_adsb_aprs.py
```

On Windows (with Python installed):

```bat
python combined_ais_adsb_aprs.py
```

You should see:

* Console messages indicating connection to APRS, AIS listener, dump1090 SBS, and JSON status.
* A main Tkinter window titled **“AIS→APRS Monitor”**.
* A second Tkinter window titled **“ADSB→APRS Monitor”**.

---

## GUI Overview

### AIS Monitor Window

Columns:

* `MMSI` – 9-digit MMSI (also used as APRS object name)
* `Name` – Vessel name (from type 5/24 static/voyage messages)
* `Sym` – APRS symbol table+code (e.g. `/s`)
* `Symbol Desc` – Human-readable symbol interpretation
* `Latitude` / `Longitude`
* `AIS Type` – AIS message type (1,2,3,4,18,19,27)
* `Last Seen (UTC)` – Last update time

### ADS-B Monitor Window

Columns:

* `Object` – APRS object name (callsign or ICAO padded to 9 chars)
* `ICAO` – Hex address (e.g. `A8B123`)
* `Callsign` – Flight identifier
* `Cat` – Emitter category from JSON (e.g. `A1`, `A3`, `B2`, `A7`, etc.)
* `Type` – Aircraft type from JSON (e.g. `B738`, `A320`, `C172`)
* `Sym` – APRS symbol table+code (plane, heli, balloon, glider)
* `Latitude` / `Longitude`
* `Alt (ft)` – Baro altitude
* `GS (kt)` – Ground speed
* `TRK` – Track (degrees)
* `Last Seen (UTC)` – Last update time

Rows update once per second for a smooth, near-real-time view.

---

## Notes / Tuning

* The default ranges and thresholds (movement, TTL, landing altitude, etc.) are tuned for a **Buffalo, NY**-centric setup and moderate APRS packet rate.
* You can safely adjust:

  * Range radii (`ADD_DISTANCE_MI`, `CLEAR_DISTANCE_MI`, `MAX_RANGE_NM`)
  * Teleport thresholds
  * Update intervals (`MIN_UPDATE_SEC`, `JSON_REFRESH_SEC`)
  * GUI column widths and sets, if you want less or more detail.

---

## Troubleshooting

* **No AIS data in the GUI:**

  * Confirm AIS-catcher is sending NMEA to the correct host/port.
  * Check firewall rules.
  * Watch the console for `[AIS]` logs and `[AIS-DEBUG]` messages.

* **No ADS-B data in the GUI:**

  * Verify `dump1090` is running and accepting SBS connections (`30003`).
  * Open the JSON URL in a browser to confirm it serves valid data.
  * Watch the console for `[SBS]` and `[JSON]` logs.

* **No APRS objects in APRSIS32:**

  * Ensure APRSIS32 is configured as an IS-Server listening on `APRSIS_IP:APRSIS_TCP_PORT`.
  * Check that `CALLSIGN` and `PASSCODE` are accepted.
  * Look for `[APRS-AIS]` / `[APRS-ADSB]` send and reconnect messages.


## Credits

* AIS decoding logic inspired by the AIS specification and common open-source examples.
* ADS-B handling based on `dump1090` SBS/JSON outputs.
* APRS object format based on standard APRS spec and APRSIS32 conventions.

```
```
