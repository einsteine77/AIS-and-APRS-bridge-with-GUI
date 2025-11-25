#!/usr/bin/env python3
"""
Combined AIS+ADSB ? APRS bridge with two GUI monitor windows.

- AIS side:
    * Listens for NMEA AIS (AIVDM/AIVDO) from AIS-catcher on TCP 0.0.0.0:10110
    * Decodes position, base station, long-range, and static/voyage (names)
    * Sends APRS objects to local APRSIS32 IS-Server
    * Shows vessels in an AIS monitor window (Tkinter)

- ADS-B side:
    * Connects to dump1090 SBS (port 30003) and dump1090 JSON (data.json)
    * Applies range / movement / landing / rename logic
    * Sends APRS objects to local APRSIS32 IS-Server
    * Shows aircraft in an ADSB monitor window (Tkinter), including:
        - name, ICAO, callsign, category, ac type, symbol, lat, lon, alt, GS, TRK, last seen
"""

import socket
import time
import re
import math
import json
import urllib.request
import threading
from datetime import datetime, timezone
import tkinter as tk
from tkinter import ttk

# ============================================================
# Versions / shared APRS settings
# ============================================================
COMBINED_VERSION = "AIS+ADSB-GUI-1.1"

APRSIS_IP       = "127.0.0.1"
APRSIS_TCP_PORT = 14580

CALLSIGN        = "N2UGS-10"
PASSCODE        = -1

MAX_PKTS_PER_SEC = 5
DEBUG = True

# ============================================================
# Shared helpers
# ============================================================
def utc_hhmmss():
    return datetime.now(timezone.utc).strftime("%H%M%S")

def dm_lat(lat):
    hemi = "N" if lat >= 0 else "S"
    a = abs(lat); d = int(a); m = (a - d) * 60
    return f"{d:02d}{m:05.2f}{hemi}"

def dm_lon(lon):
    hemi = "E" if lon >= 0 else "W"
    a = abs(lon); d = int(a); m = (a - d) * 60
    return f"{d:03d}{m:05.2f}{hemi}"

def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles."""
    R = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def haversine_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    R_nm = 3440.1
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2)**2
    return R_nm * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def connect_aprs(client_tag: str):
    """Shared APRS-IS connector; client_tag is e.g. 'AIS2APRS' or 'ADSB2APRS'."""
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((APRSIS_IP, APRSIS_TCP_PORT))
            login = f"user {CALLSIGN} pass {PASSCODE} vers {client_tag} {COMBINED_VERSION} filter m/500\n"
            s.send(login.encode("ascii"))
            print(f"[APRS-{client_tag}] Connected & logged in as {CALLSIGN}")
            return s
        except Exception as e:
            print(f"[APRS-{client_tag}] Connect failed ({e}); retry in 3s...")
            time.sleep(3)

# ============================================================
# ---------------------- ADS-B SECTION -----------------------
# ============================================================
ADSB_VERSION = "2.14"

# -------- ADS-B CONFIG ---------
DUMP1090_HOST     = "192.168.35.33"
DUMP1090_PORT     = 30003
DUMP1090_JSON_URL = f"http://{DUMP1090_HOST}:8080/data.json"

MIN_UPDATE_SEC   = 5
MIN_MOVE_MI      = 0.50

OBJECT_TTL_SEC   = 300
LANDED_ALT_FT    = 1000
LANDED_WAIT_SEC  = 180
LAND_CLEAR_ALT   = 1500

EPS_LATLON_DEG   = 0.00015
EPS_ALT_FT       = 25
EPS_TRK_DEG      = 3
EPS_GS_KT        = 2

KBUF_LAT          = 42.9405
KBUF_LON          = -78.7322
ADD_DISTANCE_MI   = 35
CLEAR_DISTANCE_MI = 40

JSON_REFRESH_SEC  = 5

APPEND_SYM_TAG        = True
RENAME_LOG_BRIEF_ONLY = True

# ADS-B ? GUI table
aircraft = {}           # key: tracked_name (str) -> dict(...)
aircraft_lock = threading.Lock()

# ----------------- ADS-B symbol helpers -----------------
def symbol_for_category(emitter_cat, ac_type=None):
    PLANE   = ('/','^','PLANE')
    HELI    = ('/','X','HELI')
    BALLOON = ('/','O','BALLOON')
    GLIDER  = ('/','g','GLIDER')

    if emitter_cat:
        cat = str(emitter_cat).upper().strip()
        if cat == 'A7':         # rotorcraft
            return HELI
        if cat == 'B2':         # lighter-than-air
            return BALLOON
        if cat in ('B1','B4'):  # glider/ultralight
            return GLIDER
        return PLANE

    t = (ac_type or "").upper()
    if t:
        if t.startswith('H') or 'HELI' in t or t.startswith(('EC','UH','AH','CH','MH','R22','R44','BELL','BK')):
            return HELI
        if 'GLID' in t or t.startswith(('DG','ASW','ASK','LS','G1','G2','G3')):
            return GLIDER
        if 'BAL' in t or 'BLN' in t or 'BALLOON' in t or 'HAB' in t:
            return BALLOON
    return PLANE

_name_cleaner = re.compile(r"[^A-Z0-9]")

def normalize_callsign(cs):
    if not cs: return None
    n = _name_cleaner.sub('', cs.upper())
    return n or None

def name_from_callsign_or_hex(callsign, icao_hex):
    n = normalize_callsign(callsign)
    if n: return n[:9].ljust(9)
    return (icao_hex or "AIRCRAFT")[:9].ljust(9)

def make_adsb_aprs_object(name, lat, lon, table='/', code='^',
                          trk=None, gs=None, alt=None,
                          icao=None, callsign=None, sym_tag=None,
                          delete=False):
    ts = utc_hhmmss() + "z"
    lat_s, lon_s = dm_lat(lat), dm_lon(lon)

    parts=[]
    if trk is not None: parts.append(f"TRK {int(trk)%360:03d}")
    if gs  is not None: parts.append(f"GS {int(gs)}kt")
    if alt is not None: parts.append(f"ALT {int(alt)}ft")
    if callsign:
        cs = normalize_callsign(callsign)
        if cs: parts.append(f"FLT {cs}")
    if icao: parts.append(f"ICAO {icao}")
    if APPEND_SYM_TAG and sym_tag:
        parts.append(f"SYM {sym_tag}")
    if delete:
        parts.append("DEL")

    comment = " ".join(parts) if parts else "ADS-B"
    return f";{name}*{ts}{lat_s}{table}{lon_s}{code}{comment}"

# --- ADS-B connections, parsers, JSON helpers ---
def connect_sbs():
    while True:
        try:
            s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((DUMP1090_HOST, DUMP1090_PORT))
            print(f"[SBS] Connected to {DUMP1090_HOST}:{DUMP1090_PORT}")
            return s
        except Exception as e:
            print(f"[SBS] Connect fail ({e}); retry 3s")
            time.sleep(3)

def parse_sbs(line):
    f=line.strip().split(',')
    if len(f)<22 or f[0]!="MSG": return None
    try: subtype=int(f[1])
    except: return None
    if subtype not in (3,4): return None

    icao = f[4].strip().upper() if f[4] else None
    callsign = f[10].strip() if len(f)>10 and f[10].strip() else None

    try: lat=float(f[14]) if f[14] else None
    except: lat=None
    try: lon=float(f[15]) if f[15] else None
    except: lon=None
    try: alt=float(f[11]) if f[11] else None
    except: alt=None
    try: gs=float(f[12]) if f[12] else None
    except: gs=None
    try: trk=float(f[13]) if f[13] else None
    except: trk=None

    if lat is None or lon is None: return None
    return {"icao":icao,"callsign":callsign,"lat":lat,"lon":lon,"trk":trk,"gs":gs,"alt":alt}

def fetch_aircraft_json():
    try:
        with urllib.request.urlopen(DUMP1090_JSON_URL, timeout=1.5) as r:
            if r.status != 200: return None
            return json.loads(r.read().decode('utf-8', errors='ignore'))
    except Exception:
        return None

def _maybe_print_json_status(js):
    now = time.time()
    last = js.get('_last_print', 0)
    changed = js.get('_last_ok_state') != js.get('ok')
    if changed or (now - last) > 60:
        if js.get('ok'):
            cnt = js.get('count', 0)
            print(f"[JSON] OK  source={DUMP1090_JSON_URL} count={cnt} last_ok={int(now-js.get('last_ok',now))}s")
        else:
            print(f"[JSON] FAIL ({js.get('last_err','no data')})  url={DUMP1090_JSON_URL}")
        js['_last_print'] = now
        js['_last_ok_state'] = js.get('ok')

def refresh_meta_cache(meta_cache, json_status):
    j = fetch_aircraft_json()
    now = time.time()
    if isinstance(j, dict) and 'aircraft' in j:
        ac_list = j.get('aircraft', [])
    elif isinstance(j, list):
        ac_list = j
    else:
        ac_list = None

    if isinstance(ac_list, list):
        count = 0
        for a in ac_list:
            icao = (a.get('hex') or "").upper()
            if not icao:
                continue
            entry = meta_cache.setdefault(icao, {})
            cat = a.get('category')
            typ = a.get('type') or a.get('t')
            flt = a.get('flight') or a.get('call') or a.get('flightnumber')
            if cat: entry['cat'] = str(cat).strip()
            if typ: entry['type'] = str(typ).strip()
            if flt: entry['flight'] = str(flt).strip()
            count += 1
        json_status['ok'] = True
        json_status['last_ok'] = now
        json_status['last_err'] = None
        json_status['count'] = count
        _maybe_print_json_status(json_status)
    else:
        json_status['ok'] = False
        json_status['last_err'] = "bad format" if j is not None else "no data"
        _maybe_print_json_status(json_status)

# -------------- ADS-B background worker --------------
def adsb_worker():
    print(
        f"ADSB?APRS bridge v{ADSB_VERSION} | "
        f"Add={ADD_DISTANCE_MI}mi / Clear>{CLEAR_DISTANCE_MI}mi | "
        f"pacing {MIN_UPDATE_SEC}s / {MIN_MOVE_MI}mi | "
        f"Landed dwell {LANDED_WAIT_SEC}s at {LANDED_ALT_FT}ft | "
        f"JSON {DUMP1090_JSON_URL}"
    )

    aprs = connect_aprs("ADSB2APRS")
    sbs  = connect_sbs()
    buff = b""

    last_seen, last_sent = {}, {}
    low_alt_since, landed_block = {}, set()
    hex_to_name, name_to_hex = {}, {}

    meta_cache, json_status = {}, {'ok': False, '_last_ok_state': None, '_last_print': 0}
    last_json_poll = 0

    last_sec = 0
    sent_this_sec = 0

    while True:
        try:
            now_time = time.time()
            if now_time - last_json_poll >= JSON_REFRESH_SEC:
                refresh_meta_cache(meta_cache, json_status)
                last_json_poll = now_time

            data = sbs.recv(4096)
            if not data:
                print("[SBS] Lost connection; reconnecting...")
                try: sbs.close()
                except: pass
                sbs = connect_sbs()
                continue

            buff += data
            while b"\n" in buff:
                raw, buff = buff.split(b"\n", 1)
                line = raw.decode(errors="ignore").strip()
                msg  = parse_sbs(line)
                if not msg:
                    continue

                icao_hex = (msg["icao"] or "").upper()
                meta = meta_cache.get(icao_hex, {})
                json_callsign = meta.get('flight')
                callsign = msg["callsign"] or json_callsign
                desired_name = name_from_callsign_or_hex(callsign, icao_hex)

                table, code, sym_tag = symbol_for_category(meta.get('cat'), meta.get('type'))
                dist_kbuf = haversine_miles(KBUF_LAT, KBUF_LON, msg["lat"], msg["lon"])

                current_name_for_hex = hex_to_name.get(icao_hex)
                tracked_name = current_name_for_hex if current_name_for_hex else desired_name

                # Range hysteresis
                if tracked_name in last_seen and dist_kbuf > CLEAR_DISTANCE_MI:
                    li = last_sent.get(tracked_name)
                    lat = li["lat"] if li else msg["lat"]
                    lon = li["lon"] if li else msg["lon"]
                    try:
                        delpkt = make_adsb_aprs_object(tracked_name, lat, lon, table, code, sym_tag=sym_tag, delete=True)
                        aprs.send(f"{CALLSIGN}>APRS,TCPIP*:{delpkt}\n".encode())
                    except:
                        pass
                    last_seen.pop(tracked_name, None)
                    last_sent.pop(tracked_name, None)
                    low_alt_since.pop(tracked_name, None)
                    landed_block.discard(tracked_name)
                    if current_name_for_hex:
                        hex_to_name.pop(icao_hex, None)
                    name_to_hex.pop(tracked_name, None)
                    with aircraft_lock:
                        aircraft.pop(tracked_name, None)
                    if DEBUG: print(f"[EXPIRE] Out of range >{CLEAR_DISTANCE_MI}mi: Deleted {tracked_name.strip()}")
                    continue

                if tracked_name not in last_seen and dist_kbuf > ADD_DISTANCE_MI:
                    continue

                alt = msg["alt"]
                now = int(time.time())

                # Landed suppression re-enable
                if tracked_name in landed_block and (alt is None or alt > LAND_CLEAR_ALT):
                    landed_block.discard(tracked_name)
                    low_alt_since.pop(tracked_name, None)
                    if DEBUG: print(f"[LAND] {tracked_name.strip()} climbed >{LAND_CLEAR_ALT}ft; re-enable")

                if tracked_name in landed_block and alt is not None and alt <= LANDED_ALT_FT:
                    continue

                # Landed dwell
                if alt is not None and alt <= LANDED_ALT_FT:
                    if tracked_name not in low_alt_since:
                        low_alt_since[tracked_name] = now
                    if now - low_alt_since[tracked_name] >= LANDED_WAIT_SEC:
                        li = last_sent.get(tracked_name)
                        lat = li["lat"] if li else msg["lat"]
                        lon = li["lon"] if li else msg["lon"]
                        try:
                            delpkt = make_adsb_aprs_object(tracked_name, lat, lon, table, code, sym_tag=sym_tag, delete=True)
                            aprs.send(f"{CALLSIGN}>APRS,TCPIP*:{delpkt}\n".encode())
                        except:
                            pass
                        last_seen.pop(tracked_name, None)
                        last_sent.pop(tracked_name, None)
                        landed_block.add(tracked_name)
                        if current_name_for_hex:
                            hex_to_name.pop(icao_hex, None)
                        name_to_hex.pop(tracked_name, None)
                        with aircraft_lock:
                            aircraft.pop(tracked_name, None)
                        if DEBUG: print(f"[LAND] Dwell delete {tracked_name.strip()} ({LANDED_ALT_FT}ft for {LANDED_WAIT_SEC}s)")
                        continue
                else:
                    low_alt_since.pop(tracked_name, None)

                # global throttle
                if now != last_sec:
                    last_sec = now
                    sent_this_sec = 0
                if sent_this_sec >= MAX_PKTS_PER_SEC:
                    continue

                # Rename hex ? callsign when flight appears
                if current_name_for_hex and desired_name != current_name_for_hex:
                    li = last_sent.get(current_name_for_hex)
                    lat = li["lat"] if li else msg["lat"]
                    lon = li["lon"] if li else msg["lon"]
                    try:
                        delpkt = make_adsb_aprs_object(current_name_for_hex, lat, lon, table, code, sym_tag=sym_tag, delete=True)
                        aprs.send(f"{CALLSIGN}>APRS,TCPIP*:{delpkt}\n".encode())
                    except:
                        pass
                    last_seen.pop(current_name_for_hex, None)
                    prev_info = last_sent.pop(current_name_for_hex, None)
                    if prev_info:
                        last_sent[desired_name] = prev_info
                    name_to_hex.pop(current_name_for_hex, None)
                    hex_to_name[icao_hex] = desired_name
                    name_to_hex[desired_name] = icao_hex
                    tracked_name = desired_name
                    if DEBUG:
                        print(f"[RENAME] {current_name_for_hex.strip()} ? {desired_name.strip()}")

                if icao_hex and icao_hex not in hex_to_name:
                    hex_to_name[icao_hex] = tracked_name
                    name_to_hex[tracked_name] = icao_hex

                last_seen[tracked_name] = now

                prev_info  = last_sent.get(tracked_name)
                prev_state = prev_info["state"] if prev_info else None
                prev_time  = prev_info["time"]  if prev_info else 0
                prev_lat   = prev_info["lat"]   if prev_info else None
                prev_lon   = prev_info["lon"]   if prev_info else None

                moved_far_enough = False
                if prev_lat is not None and prev_lon is not None:
                    moved_far_enough = (haversine_miles(prev_lat, prev_lon, msg["lat"], msg["lon"]) >= MIN_MOVE_MI)

                def state_changed(prev, cur):
                    if prev is None: return True
                    if abs(cur["lat"]-prev["lat"]) >= EPS_LATLON_DEG: return True
                    if abs(cur["lon"]-prev["lon"]) >= EPS_LATLON_DEG: return True
                    if (cur["alt"] is None) != (prev["alt"] is None): return True
                    if cur["alt"] is not None and prev["alt"] is not None:
                        if abs(cur["alt"]-prev["alt"]) >= EPS_ALT_FT: return True
                    if (cur["trk"] is None) != (prev["trk"] is None): return True
                    if cur["trk"] is not None and prev["trk"] is not None:
                        a=int(cur["trk"])%360; b=int(prev["trk"])%360
                        d=abs(a-b); d=min(d,360-d)
                        if d >= EPS_TRK_DEG: return True
                    if (cur["gs"] is None) != (prev["gs"] is None): return True
                    if cur["gs"] is not None and prev["gs"] is not None:
                        if abs(cur["gs"]-prev["gs"]) >= EPS_GS_KT: return True
                    return False

                need_send = False
                if prev_state is None:
                    need_send = True
                elif moved_far_enough:
                    need_send = True
                elif state_changed(prev_state, msg) and (now - prev_time) >= MIN_UPDATE_SEC:
                    need_send = True

                if not need_send:
                    continue

                pkt = make_adsb_aprs_object(
                    tracked_name, msg["lat"], msg["lon"],
                    table, code,
                    msg["trk"], msg["gs"], msg["alt"],
                    icao_hex, callsign, sym_tag
                )
                out = f"{CALLSIGN}>APRS,TCPIP*:{pkt}\n"

                try:
                    aprs.send(out.encode("ascii", errors="ignore"))
                    last_sent[tracked_name] = {
                        "time": now, "state": msg, "lat": msg["lat"], "lon": msg["lon"], "icao": icao_hex
                    }

                    # NEW: pack meta (cat, type) into aircraft dict for GUI
                    cat = meta.get('cat') or ""
                    ac_type = meta.get('type') or ""

                    with aircraft_lock:
                        aircraft[tracked_name] = {
                            "name": tracked_name.strip(),
                            "icao": icao_hex,
                            "callsign": callsign or "",
                            "cat": cat,
                            "ac_type": ac_type,
                            "lat": msg["lat"],
                            "lon": msg["lon"],
                            "alt": msg["alt"],
                            "gs": msg["gs"],
                            "trk": msg["trk"],
                            "symbol": table + code,
                            "sym_tag": sym_tag,
                            "last": datetime.utcnow().strftime("%H:%M:%S"),
                        }

                    sent_this_sec += 1

                    if DEBUG:
                        print(f"[SEND-ADSB] {tracked_name.strip()} "
                              f"{msg['lat']:.5f},{msg['lon']:.5f} "
                              f"alt={int(msg['alt']) if msg['alt'] is not None else '-'} "
                              f"gs={int(msg['gs']) if msg['gs'] is not None else '-'} "
                              f"trk={int(msg['trk']) if msg['trk'] is not None else '-'} "
                              f"cat={cat} type={ac_type} "
                              f"sym={table}{code} tag={sym_tag}")
                except Exception as e:
                    print(f"[APRS-ADSB] Send fail ({e}); reconnecting...")
                    try: aprs.close()
                    except: pass
                    aprs = connect_aprs("ADSB2APRS")

            # Cleanup phase
            now = int(time.time())
            to_delete = [n for n, t0 in list(last_seen.items()) if now - t0 >= OBJECT_TTL_SEC]

            for n in to_delete:
                li = last_sent.get(n)
                lat = li["lat"] if li else 0
                lon = li["lon"] if li else 0
                last_seen.pop(n, None)
                last_sent.pop(n, None)
                low_alt_since.pop(n, None)
                landed_block.discard(n)
                hx = name_to_hex.pop(n, None)
                if hx:
                    hex_to_name.pop(hx, None)
                try:
                    meta = {} if not hx else meta_cache.get(hx, {})
                    t,c,tag = symbol_for_category(meta.get('cat'), meta.get('type'))
                    delpkt = make_adsb_aprs_object(n, lat, lon, t, c, sym_tag=tag, delete=True)
                    aprs.send(f"{CALLSIGN}>APRS,TCPIP*:{delpkt}\n".encode())
                except:
                    pass
                with aircraft_lock:
                    aircraft.pop(n, None)
                if DEBUG:
                    print(f"[EXPIRE] Deleted {n.strip()} (silent = {OBJECT_TTL_SEC}s)")

        except Exception as e:
            print(f"[SBS] Error {e}; retry 2s")
            time.sleep(2)
            try: sbs.close()
            except: pass
            sbs = connect_sbs()

# ============================================================
# ----------------------- AIS SECTION ------------------------
# ============================================================
AIS_VERSION = "1.5-GUI-NAMES"

AIS_LISTEN_IP   = "0.0.0.0"
AIS_LISTEN_PORT = 10110

CENTER_LAT        = 42.9
CENTER_LON        = -78.9
MAX_RANGE_NM      = 250.0

TELEPORT_MOVE_NM  = 150.0
TELEPORT_TIME_SEC = 900

# Global vessel & name tables for GUI
vessels = {}        # mmsi (int) -> dict(...)
name_cache = {}     # mmsi (int) -> name
vessels_lock = threading.Lock()

_ASCII2SIX = {chr(i): (i - 48 if i < 88 else i - 56) for i in range(48, 120)}

def sixbit_unpack(payload: str) -> str:
    return "".join(f"{_ASCII2SIX.get(ch, 0):06b}" for ch in payload)

def _u(bits, start, length):
    return int(bits[start:start+length], 2)

def _s(bits, start, length):
    b = bits[start:start+length]
    if not b:
        return 0
    v = int(b, 2)
    if b[0] == "1":
        v -= (1 << length)
    return v

def sixbit_to_ascii(bits, start, length):
    out = []
    for i in range(start, start + length, 6):
        val = _u(bits, i, 6)
        ch = chr(val + 0x20)
        if ch == "@":
            ch = " "
        out.append(ch)
    return "".join(out).strip()

def decode_position(bits):
    if len(bits) < 168:
        return None, "short"
    msgtype = _u(bits, 0, 6)
    if msgtype not in (1, 2, 3, 18, 19):
        return None, f"type{msgtype}"

    if msgtype in (1, 2, 3):
        lon = _s(bits, 61, 28)
        lat = _s(bits, 89, 27)
        sog10 = _u(bits, 50, 10)
        cog10 = _u(bits, 116, 12)
        hdg = _u(bits, 128, 9)
    else:
        lon = _s(bits, 57, 28)
        lat = _s(bits, 85, 27)
        sog10 = _u(bits, 46, 10)
        cog10 = _u(bits, 112, 12)
        hdg = _u(bits, 124, 9)

    if abs(lon) >= 108600000 or abs(lat) >= 54600000:
        return None, "invalid_latlon"

    info = {
        "type": msgtype,
        "mmsi": _u(bits, 8, 30),
        "lat":  lat / 600000.0,
        "lon":  lon / 600000.0,
        "sog":  None if sog10 == 1023 else sog10 / 10.0,
        "cog":  None if cog10 >= 3600 else cog10 / 10.0,
        "hdg":  None if hdg == 511 else hdg
    }
    return info, None

def decode_basestation(bits):
    if len(bits) < 168:
        return None, "short"
    msgtype = _u(bits, 0, 6)
    if msgtype != 4:
        return None, "not_type4"

    lon = _s(bits, 79, 28)
    lat = _s(bits, 107, 27)
    if abs(lon) >= 108600000 or abs(lat) >= 54600000:
        return None, "invalid_latlon"

    info = {
        "type": 4,
        "mmsi": _u(bits, 8, 30),
        "lat":  lat / 600000.0,
        "lon":  lon / 600000.0,
        "sog":  0.0,
        "cog":  0.0,
        "hdg":  None
    }
    return info, None

def decode_longrange(bits):
    if len(bits) < 96:
        return None, "short"
    msgtype = _u(bits, 0, 6)
    if msgtype != 27:
        return None, "not_type27"

    lon = _s(bits, 44, 18)
    lat = _s(bits, 62, 17)

    if lon == 0x1FFFF or lat == 0x1FFFF:
        return None, "invalid_latlon"

    info = {
        "type": 27,
        "mmsi": _u(bits, 8, 30),
        "lat":  lat / 600.0,
        "lon":  lon / 600.0,
        "sog":  None,
        "cog":  None,
        "hdg":  None
    }
    return info, None

def decode_static(bits):
    msgtype = _u(bits, 0, 6)
    if msgtype == 5:
        if len(bits) < 424:
            return None, "short5"
        mmsi = _u(bits, 8, 30)
        name = sixbit_to_ascii(bits, 112, 120)
        return {"type": 5, "mmsi": mmsi, "name": name}, None

    if msgtype == 24:
        if len(bits) < 160:
            return None, "short24"
        mmsi = _u(bits, 8, 30)
        part_no = _u(bits, 38, 2)
        if part_no in (0, 1):
            name = sixbit_to_ascii(bits, 40, 120)
            return {"type": 24, "mmsi": mmsi, "name": name}, None
        return None, "partB"

    return None, f"not_static{msgtype}"

def aprs_symbol_for(info):
    t = info["type"]
    if t == 4:
        return "/", "r"
    if t == 27:
        return "/", "s"
    if t in (1, 2, 3, 18, 19):
        return "/", "s"
    return "/", "s"

def symbol_desc(info):
    t = info["type"]
    if t == 4:
        return "Base station"
    if t in (1, 2, 3):
        return "Ship (Class A)"
    if t in (18, 19):
        return "Ship (Class B)"
    if t == 27:
        return "Long-range ship"
    return f"Type {t}"

def make_ais_aprs_object(info, vessel_name=None):
    mmsi_str = f"{info['mmsi']:09d}"
    obj_name = mmsi_str
    ts = utc_hhmmss() + "z"
    table, code = aprs_symbol_for(info)

    sog = int(info["sog"] or 0)
    cog = int(info["cog"] or 0)
    hdg = info["hdg"]

    comment_parts = []
    if vessel_name:
        comment_parts.append(f"NAME {vessel_name}")
    comment_parts.append(f"SOG {sog}kt")
    comment_parts.append(f"COG {cog:03d}")
    if hdg is not None:
        comment_parts.append(f"HDG {int(hdg)}")
    comment_parts.append(f"MMSI {mmsi_str}")
    comment = " ".join(comment_parts)

    pkt = (
        f";{obj_name:<9s}*"
        f"{ts}"
        f"{dm_lat(info['lat'])}"
        f"{table}"
        f"{dm_lon(info['lon'])}"
        f"{code}"
        f"{comment}"
    )
    return pkt, obj_name, table, code

frag_store = {}

def parse_nmea(line: str):
    if not (line.startswith("!AIVDM") or line.startswith("!AIVDO")):
        return None, "not_AIVDM"
    parts = line.split(",")
    if len(parts) < 7:
        return None, "short_line"

    try:
        fragcount = int(parts[1] or "1")
        fragnum   = int(parts[2] or "1")
    except ValueError:
        return None, "bad_frag_fields"

    seq_id = parts[3] or ""
    channel = parts[4] or ""
    payload = parts[5]
    fb_raw  = parts[6].split("*")[0]
    try:
        fillbits = int(fb_raw or "0")
    except ValueError:
        fillbits = 0

    if fragcount == 1 and fragnum == 1:
        return (payload, fillbits), None

    key = (seq_id, channel)
    entry = frag_store.get(key)
    if not entry:
        entry = {
            "total": fragcount,
            "payloads": {},
            "fill": fillbits,
            "time": time.time()
        }
        frag_store[key] = entry

    entry["payloads"][fragnum] = payload
    entry["fill"] = fillbits

    if len(entry["payloads"]) == entry["total"]:
        full_payload = "".join(entry["payloads"].get(i, "") for i in range(1, entry["total"] + 1))
        full_fill = entry["fill"]
        del frag_store[key]
        return (full_payload, full_fill), None

    return None, "waiting_frag"

def accept_ais():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((AIS_LISTEN_IP, AIS_LISTEN_PORT))
    srv.listen(1)
    print(f"[AIS] Waiting for TCP on {AIS_LISTEN_IP}:{AIS_LISTEN_PORT} ...")
    while True:
        conn, addr = srv.accept()
        print(f"[AIS] Connected from {addr[0]}:{addr[1]}")
        buf = b""
        try:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    print("[AIS] Disconnected; waiting for new connection...")
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.decode(errors="ignore").strip()
        finally:
            conn.close()

def ais_worker():
    print(f"AIS?APRS bridge with GUI v{AIS_VERSION}")
    print(f"APRSIS32 IS-Server @ {APRSIS_IP}:{APRSIS_TCP_PORT}")
    print(f"Listening for AIS-catcher on {AIS_LISTEN_IP}:{AIS_LISTEN_PORT}")
    print("Remember to point AIS-catcher here, e.g.:")
    print(f"  ais-catcher -d 4 -n -o 1 -P 192.168.35.183 {AIS_LISTEN_PORT}")

    aprs = connect_aprs("AIS2APRS")
    last_sec = 0
    sent_this_sec = 0
    last_good = {}

    for line in accept_ais():
        try:
            parsed, perr = parse_nmea(line)
            if perr or not parsed:
                if DEBUG and perr not in ("not_AIVDM", "waiting_frag"):
                    if perr not in ("short_line", "bad_frag_fields"):
                        print(f"[AIS-DEBUG] Skip ({perr})")
                continue

            payload, fill = parsed
            bits = sixbit_unpack(payload)
            if fill:
                bits = bits[:-fill]

            static_info, swhy = decode_static(bits)
            if static_info:
                mmsi = static_info["mmsi"]
                name = static_info["name"].strip()
                if name:
                    with vessels_lock:
                        name_cache[mmsi] = name
                        v = vessels.get(mmsi)
                        if v:
                            v["name"] = name
                    if DEBUG:
                        print(f"[NAME] {mmsi} -> \"{name}\" (type {static_info['type']})")
                continue

            info, why = decode_position(bits)
            if not info:
                info, why = decode_basestation(bits)
            if not info:
                info, why = decode_longrange(bits)
            if not info:
                if DEBUG and why not in ("short", "invalid_latlon"):
                    print(f"[AIS-DEBUG] Skip unsupported {why}")
                continue

            lat, lon = info["lat"], info["lon"]

            if abs(lat) < 0.001 and abs(lon) < 0.001:
                if DEBUG:
                    print(f"[AIS-SKIP] {info['mmsi']} near 0,0")
                continue

            d_center = haversine_nm(CENTER_LAT, CENTER_LON, lat, lon)
            if d_center > MAX_RANGE_NM:
                if DEBUG:
                    print(f"[AIS-SKIP] {info['mmsi']} out of range {d_center:.1f}nm")
                continue

            now = int(time.time())

            prev = last_good.get(info["mmsi"])
            if prev:
                d_move = haversine_nm(prev["lat"], prev["lon"], lat, lon)
                dt = now - prev["time"]
                if dt < TELEPORT_TIME_SEC and d_move > TELEPORT_MOVE_NM:
                    if DEBUG:
                        print(f"[AIS-SKIP] {info['mmsi']} teleport {d_move:.1f}nm in {dt}s")
                    continue

            if now != last_sec:
                last_sec = now
                sent_this_sec = 0
            if sent_this_sec >= MAX_PKTS_PER_SEC:
                continue
            sent_this_sec += 1

            with vessels_lock:
                vname = name_cache.get(info["mmsi"])

            pkt, obj_name, table, code = make_ais_aprs_object(info, vessel_name=vname)
            header = f"{CALLSIGN}>APRS,TCPIP*:{pkt}\n"

            try:
                aprs.send(header.encode("ascii", errors="ignore"))
            except Exception as e:
                print(f"[APRS-AIS] Send failed ({e}); reconnecting...")
                time.sleep(2)
                try:
                    aprs.close()
                except Exception:
                    pass
                aprs = connect_aprs("AIS2APRS")
                continue

            last_good[info["mmsi"]] = {"lat": lat, "lon": lon, "time": now}

            with vessels_lock:
                display_name = vname if vname else f"{info['mmsi']:09d}"
                vessels[info["mmsi"]] = {
                    "mmsi": f"{info['mmsi']:09d}",
                    "name": display_name,
                    "lat": lat,
                    "lon": lon,
                    "symbol": table + code,
                    "type": info["type"],
                    "sym_desc": symbol_desc(info),
                    "last": datetime.utcnow().strftime("%H:%M:%S")
                }

            if DEBUG:
                if vname:
                    print(f"[SEND-AIS] {info['mmsi']} lat={lat:.5f} lon={lon:.5f} name=\"{vname}\"")
                else:
                    print(f"[SEND-AIS] {info['mmsi']} lat={lat:.5f} lon={lon:.5f}")

        except Exception as e:
            print(f"[AIS] Error ({e}); reconnecting APRS in 2s...")
            time.sleep(2)
            try:
                aprs.close()
            except Exception:
                pass
            aprs = connect_aprs("AIS2APRS")

# ============================================================
# ------------------------- GUI ------------------------------
# ============================================================
def setup_ais_gui(root):
    root.title(f"AIS?APRS Monitor v{AIS_VERSION}")

    cols = ("mmsi", "name", "symbol", "sym_desc", "lat", "lon", "type", "last")
    tree = ttk.Treeview(root, columns=cols, show="headings", height=24)

    tree.heading("mmsi", text="MMSI")
    tree.heading("name", text="Name")
    tree.heading("symbol", text="Sym")
    tree.heading("sym_desc", text="Symbol Desc")
    tree.heading("lat", text="Latitude")
    tree.heading("lon", text="Longitude")
    tree.heading("type", text="AIS Type")
    tree.heading("last", text="Last Seen (UTC)")

    for c in cols:
        width = 120
        if c == "name":
            width = 150
        tree.column(c, width=width, anchor="center")

    tree.pack(fill="both", expand=True)

    status = tk.Label(root, text="Waiting for AIS data…", anchor="w")
    status.pack(fill="x")

    def refresh():
        with vessels_lock:
            data = list(vessels.values())

        tree.delete(*tree.get_children())
        for v in sorted(data, key=lambda r: r["mmsi"]):
            tree.insert(
                "",
                "end",
                values=(
                    v["mmsi"],
                    v["name"],
                    v["symbol"],
                    v["sym_desc"],
                    f"{v['lat']:.5f}",
                    f"{v['lon']:.5f}",
                    v["type"],
                    v["last"],
                ),
            )
        status.config(text=f"Tracked AIS objects: {len(data)}")
        root.after(1000, refresh)

    root.after(1000, refresh)

def setup_adsb_gui(root):
    win = tk.Toplevel(root)
    win.title(f"ADSB?APRS Monitor v{ADSB_VERSION}")

    # NEW: include cat and ac_type
    cols = ("name", "icao", "callsign", "cat", "ac_type",
            "symbol", "lat", "lon", "alt", "gs", "trk", "last")
    tree = ttk.Treeview(win, columns=cols, show="headings", height=24)

    tree.heading("name", text="Object")
    tree.heading("icao", text="ICAO")
    tree.heading("callsign", text="Callsign")
    tree.heading("cat", text="Cat")
    tree.heading("ac_type", text="Type")
    tree.heading("symbol", text="Sym")
    tree.heading("lat", text="Latitude")
    tree.heading("lon", text="Longitude")
    tree.heading("alt", text="Alt (ft)")
    tree.heading("gs", text="GS (kt)")
    tree.heading("trk", text="TRK")
    tree.heading("last", text="Last Seen (UTC)")

    for c in cols:
        width = 90
        if c == "name":
            width = 140
        elif c == "callsign":
            width = 90
        elif c == "cat":
            width = 60
        elif c == "ac_type":
            width = 130
        elif c in ("lat", "lon"):
            width = 110
        elif c in ("alt", "gs", "trk"):
            width = 70
        elif c == "last":
            width = 110
        tree.column(c, width=width, anchor="center")

    tree.pack(fill="both", expand=True)

    status = tk.Label(win, text="Waiting for ADS-B data…", anchor="w")
    status.pack(fill="x")

    def refresh_adsb():
        with aircraft_lock:
            data = list(aircraft.values())

        tree.delete(*tree.get_children())
        for a in sorted(data, key=lambda r: r["name"]):
            alt = "-" if a["alt"] is None else int(a["alt"])
            gs  = "-" if a["gs"]  is None else int(a["gs"])
            trk = "-" if a["trk"] is None else int(a["trk"]) % 360
            tree.insert(
                "",
                "end",
                values=(
                    a["name"],
                    a["icao"],
                    a["callsign"],
                    a["cat"],
                    a["ac_type"],
                    a["symbol"],
                    f"{a['lat']:.5f}",
                    f"{a['lon']:.5f}",
                    alt,
                    gs,
                    trk,
                    a["last"],
                ),
            )
        status.config(text=f"Tracked ADS-B objects: {len(data)}")
        win.after(1000, refresh_adsb)

    win.after(1000, refresh_adsb)

def start_combined_gui():
    root = tk.Tk()
    setup_ais_gui(root)
    setup_adsb_gui(root)
    root.mainloop()

# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    print(f"Combined AIS+ADSB APRS bridge v{COMBINED_VERSION}")
    print(f"APRSIS32 IS-Server @ {APRSIS_IP}:{APRSIS_TCP_PORT}")

    t_ais  = threading.Thread(target=ais_worker,  daemon=True)
    t_adsb = threading.Thread(target=adsb_worker, daemon=True)

    t_ais.start()
    t_adsb.start()

    start_combined_gui()
