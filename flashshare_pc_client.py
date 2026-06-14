"""
ZZZ FlashShare — PC client (macOS / Windows, Python 3.9+)

Role-reversed mode: the Android device is the host (hotspot) and the PC is the
client. macOS/Windows cannot reliably act as a Wi-Fi AP, so the PC only "joins".

Flow:
  1. Scan for the host (Android) over BLE (bleak) using the service UUID
  2. Connect and read the Wi-Fi credentials (SSID/PASS/PORT/HOST)
  3. Join that Wi-Fi (Android's LocalOnlyHotspot) via networksetup / netsh
  4. Send files (POST /flashshare/upload) / receive (poll the outbox and GET)

Before running, set the Android side to "Become host (receive)" and leave it
on the "Waiting for connection" screen.

Dependency: pip install -r requirements.txt   (bleak)
Examples:
  python3 flashshare_pc_client.py                      # wait to receive (poll the outbox)
  python3 flashshare_pc_client.py --send a.jpg b.pdf   # send, then keep receiving

Note: while running, the PC's Wi-Fi is switched to the Android hotspot. On exit
(including Ctrl+C) the script reconnects to your previous Wi-Fi network.
"""
from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import locale
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

from bleak import BleakClient, BleakScanner  # type: ignore

# ---- Protocol constants (must match the Flutter app) ----------------------- #
SERVICE_UUID = "7a8b0001-2c3d-4e5f-8a9b-0c1d2e3f4a5b"
WIFI_CRED_UUID = "7a8b0002-2c3d-4e5f-8a9b-0c1d2e3f4a5b"

UPLOAD_PATH = "/flashshare/upload"
PING_PATH = "/flashshare/ping"
OUTBOX_PATH = "/flashshare/outbox"
DOWNLOAD_PATH = "/flashshare/download"

INBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FlashShareInbox")

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def _mask(value) -> str:
    """Mask an identifier for display so full BLE/MAC IDs aren't shown in logs or demo videos."""
    s = str(value)
    return f"{s[:8]}…" if len(s) > 8 else s


# =========================================================================== #
# 1) BLE: discover the host and read the credentials
# =========================================================================== #
async def find_host(timeout: float = 8.0):
    """Scan for the host (Android) by service UUID and return matches (or [])."""
    print(f"[ble] Scanning for host... ({timeout:.0f}s)")
    found = await BleakScanner.discover(timeout=timeout, service_uuids=[SERVICE_UUID], return_adv=True)
    hosts = []
    for address, (device, adv) in found.items():
        suuids = [str(u).lower() for u in (adv.service_uuids or [])]
        name = device.name or adv.local_name or "ZZZ FlashShare Host"
        if SERVICE_UUID.lower() in suuids or "flashshare" in name.lower():
            hosts.append((device, name, adv.rssi))
    hosts.sort(key=lambda x: x[2], reverse=True)
    return hosts


async def read_credential(device) -> dict:
    """Connect to the host and read the Wi-Fi credentials (JSON)."""
    # Windows (WinRT) caches GATT services. The host rebuilds its BLE stack on
    # each connection, so a stale cache causes "Could not get GATT services:
    # Unreachable". use_cached_services=False forces a fresh discovery each time.
    kwargs = {"winrt": {"use_cached_services": False}} if IS_WIN else {}
    last = None
    for attempt in range(4):
        try:
            async with BleakClient(device, timeout=20.0, **kwargs) as client:
                raw = await client.read_gatt_char(WIFI_CRED_UUID)
                return json.loads(bytes(raw).decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[ble] Read failed (retry {attempt + 1}/4): {e}")
            await asyncio.sleep(1.5)
    raise RuntimeError(f"Failed to read credentials: {last}")


# =========================================================================== #
# 2) Join Wi-Fi (macOS: networksetup / Windows: netsh)
# =========================================================================== #
def _run(cmd: list[str], timeout: int = 20) -> str:
    """Run a command and return stdout+stderr (never raises).
    Japanese Windows netsh outputs CP932, so decode bytes with several encodings."""
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout)
        raw = (res.stdout or b"") + (res.stderr or b"")
    except Exception:  # noqa: BLE001
        return ""
    for enc in (locale.getpreferredencoding(False), "cp932", "utf-8", "cp437"):
        try:
            return raw.decode(enc)
        except Exception:  # noqa: BLE001
            continue
    return raw.decode("utf-8", errors="replace")


def wifi_device() -> str:
    """Wi-Fi interface device ID. macOS = en0-style, Windows = interface name."""
    if IS_MAC:
        out = _run(["networksetup", "-listallhardwareports"])
        lines = out.splitlines()
        for i, line in enumerate(lines):
            if "Wi-Fi" in line or "AirPort" in line:
                for j in range(i, min(i + 3, len(lines))):
                    if lines[j].startswith("Device:"):
                        return lines[j].split(":", 1)[1].strip()
        return "en0"
    if IS_WIN:
        out = _run(["netsh", "wlan", "show", "interfaces"])
        for line in out.splitlines():
            s = line.strip()
            if s.lower().startswith("name") and ":" in s:
                return s.split(":", 1)[1].strip()
        return "Wi-Fi"
    return "wlan0"


def current_ssid(dev: str) -> str | None:
    if IS_MAC:
        out = _run(["networksetup", "-getairportnetwork", dev])
        if ":" in out and "not associated" not in out.lower():
            return out.split(":", 1)[1].strip()
        return None
    if IS_WIN:
        out = _run(["netsh", "wlan", "show", "interfaces"])
        for line in out.splitlines():
            s = line.strip()
            low = s.lower()
            if low.startswith("ssid") and not low.startswith("bssid") and ":" in s:
                return s.split(":", 1)[1].strip()
        return None
    return None


def ssid_visible(ssid: str) -> bool:
    """Whether the given SSID is visible in the surrounding scan results."""
    if IS_MAC:
        return ssid in _run(["system_profiler", "SPAirPortDataType"], timeout=25)
    if IS_WIN:
        return ssid in _run(["netsh", "wlan", "show", "networks"])
    return True


def resolve_gateway(dev: str) -> str | None:
    """Get the gateway (= host) IP of the network currently joined."""
    if IS_MAC:
        out = _run(["ipconfig", "getoption", dev, "router"]).strip()
        return out if out.count(".") == 3 else None
    if IS_WIN:
        out = _run(["ipconfig"])
        gw = None
        for line in out.splitlines():
            if "Default Gateway" in line or "デフォルト ゲートウェイ" in line or "ゲートウェイ" in line:
                val = line.split(":")[-1].strip()
                if val.count(".") == 3:
                    gw = val  # last IPv4 found (usually the Wi-Fi adapter)
        return gw
    return None


def _win_join(ssid: str, password: str) -> bool:
    """Windows: generate a WPA2 profile XML -> add -> connect."""
    import tempfile
    xml = (
        '<?xml version="1.0"?>\n'
        '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">\n'
        f'  <name>{ssid}</name>\n'
        f'  <SSIDConfig><SSID><name>{ssid}</name></SSID></SSIDConfig>\n'
        '  <connectionType>ESS</connectionType>\n'
        '  <connectionMode>manual</connectionMode>\n'
        '  <MSM><security>\n'
        '    <authEncryption><authentication>WPA2PSK</authentication>'
        '<encryption>AES</encryption><useOneX>false</useOneX></authEncryption>\n'
        '    <sharedKey><keyType>passPhrase</keyType><protected>false</protected>'
        f'<keyMaterial>{password}</keyMaterial></sharedKey>\n'
        '  </security></MSM>\n'
        '</WLANProfile>\n'
    )
    fd, path = tempfile.mkstemp(suffix=".xml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(xml)
    try:
        _run(["netsh", "wlan", "add", "profile", f"filename={path}", "user=current"])
        # netsh success/failure messages vary by locale (CP932 etc.) and are hard
        # to parse reliably, so we just request the connection and wait; the
        # caller confirms real connectivity with ping().
        for attempt in range(3):
            print(f"[wifi] Connecting to '{ssid}'... ({attempt + 1}/3)")
            out = _run(["netsh", "wlan", "connect", f"name={ssid}", f"ssid={ssid}"])
            if out.strip():
                print(f"[wifi] netsh: {out.strip()}")
            time.sleep(4.0)  # wait for association + DHCP
            if current_ssid("") == ssid:  # confirmed if current SSID matches
                print("[wifi] Connected")
                return True
        # Even if unconfirmed it may actually be connected (locale differences).
        print("[wifi] Connection requested (connectivity is checked next)")
        return True
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def join_wifi(dev: str, ssid: str, password: str) -> bool:
    """Join Wi-Fi. Connectivity is finally confirmed by the caller's ping()."""
    # Windows: netsh needs no pre-scan wait. Add profile -> connect -> ping.
    if IS_WIN:
        return _win_join(ssid, password)

    # macOS: wait until the SSID appears in the scan before joining
    # (joining tends to fail if it's not yet visible).
    print(f"[wifi] Looking for '{ssid}'...")
    visible = False
    for i in range(6):
        if ssid_visible(ssid):
            visible = True
            break
        print(f"[wifi]   scanning... ({i + 1}/6)")
    if not visible:
        print(f"[wifi] '{ssid}' is not appearing in the scan.")
        print("[wifi] Tip: turn OFF the Android phone's Wi-Fi (disconnect from your")
        print("       home router) before 'Become host'. The hotspot then runs on")
        print("       2.4 GHz and becomes discoverable from the Mac.")

    subprocess.run(["networksetup", "-setairportpower", dev, "on"], capture_output=True)
    time.sleep(1.0)
    for attempt in range(4):
        print(f"[wifi] Connecting to '{ssid}'... ({attempt + 1}/4)")
        out = _run(["networksetup", "-setairportnetwork", dev, ssid, password])
        low = out.lower()
        if any(k in low for k in ("could not", "failed", "error", "not be found")):
            print(f"[wifi] {out.strip()}")
            time.sleep(3.0)
            continue
        if out.strip():
            print(f"[wifi] {out.strip()}")
        time.sleep(3.0)
        print("[wifi] Connection requested")
        return True
    return False


# =========================================================================== #
# 3) File transfer (HTTP)
# =========================================================================== #
def ping(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{PING_PATH}", timeout=4) as r:
            return r.status == 200 and b"FLASHSHARE" in r.read()
    except Exception:  # noqa: BLE001
        return False


def disconnect_wifi(dev: str, prev: str | None, joined_ssid: str | None) -> None:
    """On exit, leave the Android hotspot and reconnect to the previous Wi-Fi if possible."""
    if IS_WIN:
        if joined_ssid:
            _run(["netsh", "wlan", "delete", "profile", f"name={joined_ssid}"])  # remove temp profile
        if prev:
            print(f"[wifi] Reconnecting to your previous Wi-Fi '{prev}'...")
            _run(["netsh", "wlan", "connect", f"name={prev}"])
        else:
            _run(["netsh", "wlan", "disconnect"])
    elif IS_MAC:
        if prev and prev != joined_ssid:
            print(f"[wifi] Reconnecting to your previous Wi-Fi '{prev}'...")
            _run(["networksetup", "-setairportnetwork", dev, prev])
        else:
            # Power-cycle Wi-Fi so it auto-rejoins a known network.
            _run(["networksetup", "-setairportpower", dev, "off"])
            time.sleep(1.0)
            _run(["networksetup", "-setairportpower", dev, "on"])


def upload(host: str, port: int, filepath: str) -> None:
    name = os.path.basename(filepath)
    size = os.path.getsize(filepath)
    conn = http.client.HTTPConnection(host, port, timeout=60)
    conn.putrequest("POST", UPLOAD_PATH)
    conn.putheader("X-Filename", urllib.parse.quote(name))
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("Content-Length", str(size))
    conn.endheaders()
    sent = 0
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            conn.send(chunk)
            sent += len(chunk)
            pct = 100 if size == 0 else int(sent * 100 / size)
            print(f"\r[send] {name}  {pct}%", end="", flush=True)
    resp = conn.getresponse()
    resp.read()
    conn.close()
    print(f"\r[send] {name}  done ({sent} bytes)        ")


def list_outbox(host: str, port: int):
    """Success: list; unreachable/error: None (distinct from empty [] for disconnect detection)."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{OUTBOX_PATH}", timeout=5) as r:
            if r.status != 200:
                return None
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def download(host: str, port: int, entry: dict) -> str:
    os.makedirs(INBOX, exist_ok=True)
    dest = os.path.join(INBOX, os.path.basename(entry["name"]))
    url = f"http://{host}:{port}{DOWNLOAD_PATH}?id={urllib.parse.quote(entry['id'])}"
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


class Session:
    """Connection state shared between the send-input thread and the receive loop."""
    def __init__(self) -> None:
        self.host: str | None = None
        self.port: int | None = None
        self.stop = False
        self.lock = threading.Lock()


def receive_loop(host: str, port: int, session: "Session") -> None:
    """Watch the outbox. If the host stops responding for ~8s, return (= disconnected -> reconnect above)."""
    print(f"[recv] Watching the outbox... received files are saved to {INBOX}")
    seen: set[str] = set()
    fails = 0
    while not session.stop:
        entries = list_outbox(host, port)
        if entries is None:
            fails += 1
            if fails >= 4:  # ~8s with no response
                print("[recv] The connection to the host seems to have dropped.")
                return
        else:
            fails = 0
            for entry in entries:
                if entry["id"] in seen:
                    continue
                seen.add(entry["id"])
                print(f"[recv] Receiving: {entry['name']} ({entry['size']} bytes)")
                try:
                    dest = download(host, port, entry)
                    print(f"[recv] Saved: {dest}")
                except Exception as e:  # noqa: BLE001
                    print(f"[recv] Failed: {e}")
                    seen.discard(entry["id"])
        time.sleep(2.0)


def _parse_paths(line: str) -> list[str]:
    """Split an input line into file paths (supports drag & drop and quotes).
    On Windows, keep '\\' by splitting with posix=False and stripping quotes."""
    try:
        parts = shlex.split(line, posix=not IS_WIN)
    except ValueError:
        parts = [line]
    if IS_WIN:
        parts = [p.strip('"').strip("'") for p in parts]
    return [os.path.expanduser(p) for p in parts if p]


def input_send_loop(session: "Session") -> None:
    """Interactive send thread: type a path / drag & drop -> Enter to send. 'q' to quit."""
    while not session.stop:
        try:
            line = input().strip()
        except EOFError:
            return
        if line.lower() in ("q", "quit", "exit"):
            session.stop = True
            return
        if not line:
            continue
        host, port = session.host, session.port
        if not host or port is None:
            print("[send] Not connected to a host yet. Please try again after connecting.")
            continue
        for p in _parse_paths(line):
            if os.path.isfile(p):
                try:
                    upload(host, port, p)
                except Exception as e:  # noqa: BLE001
                    print(f"[send] Send failed: {e}")
            else:
                print(f"[send] File not found: {p}")


# =========================================================================== #
# Main
# =========================================================================== #
async def connect_once(args: argparse.Namespace, dev: str):
    """Get credentials (BLE auto or manual) -> join Wi-Fi -> verify reachability.
    Returns (host, port, ssid) on success, or None on failure."""
    if args.ssid and args.password:
        # Manual mode: pass the SSID/password shown on the Android host screen (no BLE).
        ssid, password = args.ssid, args.password
        port = args.port
        gateway = args.gateway or "192.168.49.1"
        print(f"[main] Manual: SSID='{ssid}' PORT={port}")
    else:
        hosts = await find_host(timeout=args.scan_timeout)
        if not hosts:
            return None
        device, name, rssi = hosts[0]
        print(f"[main] Host: {name}  rssi={rssi}  ({_mask(device.address)})")
        try:
            cred = await read_credential(device)
        except Exception as e:  # noqa: BLE001
            print(f"[main] Failed to read credentials: {e}")
            print("[main] Tip: if BLE is unstable on Windows, you can pass the SSID/password")
            print("       shown on the Android screen with --ssid \"AndroidShare_xxxx\" --password \"xxxxxxxx\".")
            return None
        ssid, password = cred["s"], cred["p"]
        port = int(cred.get("port", 53117))
        gateway = cred.get("h") or "192.168.49.1"
        print(f"[main] Credentials: SSID='{ssid}' PORT={port} GATEWAY={gateway}")

    if not join_wifi(dev, ssid, password):
        print("[main] Failed to join Wi-Fi. Check the SSID/password/signal.")
        return None

    # Prefer the DHCP gateway as the host IP (the host's self-reported IP can be wrong).
    for _ in range(6):
        cands = [c for c in (resolve_gateway(dev), gateway, "192.168.49.1") if c]
        for h in dict.fromkeys(cands):
            if ping(h, port):
                print(f"[main] Reached host server OK ({h}:{port})")
                return (h, port, ssid)
        time.sleep(1.5)
    print(f"[main] Cannot reach host server (:{port}).")
    return None


async def main(args: argparse.Namespace):
    dev = wifi_device()
    prev = current_ssid(dev)
    session = Session()
    sent = False

    # Interactive send thread (drag & drop a file -> Enter to send).
    t = threading.Thread(target=input_send_loop, args=(session,), daemon=True)
    t.start()
    print("=" * 60)
    print("[how-to] To send to the host (Android), drag & drop a file onto this")
    print("         terminal (or type a path) and press Enter.")
    print("         Receiving is automatic. Quit with 'q' + Enter or Ctrl+C.")
    print("=" * 60)

    # Manual mode can't reconnect after a host restart (SSID/pass change) -> exit on disconnect.
    # BLE mode can re-discover and fetch new credentials -> keep reconnecting.
    manual_mode = bool(args.ssid and args.password)
    exit_on_disconnect = args.exit_on_disconnect or manual_mode

    joined_ssid: str | None = None
    try:
        while not session.stop:
            target = await connect_once(args, dev)
            if not target:
                print("[main] Re-scanning for a host in 5s... (set Android to 'Waiting for connection')")
                await asyncio.sleep(5)
                continue
            session.host, session.port, joined_ssid = target

            # The --send argument sends once on the first connection.
            if not sent and args.send:
                for fp in args.send:
                    if os.path.isfile(fp):
                        upload(session.host, session.port, fp)
                    else:
                        print(f"[send] File not found: {fp}")
                sent = True

            # Watch for incoming files (interactive sending runs in another thread).
            # Returns here if the host disconnects.
            receive_loop(session.host, session.port, session)
            session.host = None
            if session.stop:
                break
            if exit_on_disconnect:
                if manual_mode and not args.exit_on_disconnect:
                    print("[main] Host disconnected (manual SSID/pass changes on restart -> can't reconnect). Exiting.")
                else:
                    print("[main] Host disconnected. Exiting.")
                break
            print("[main] Re-scanning for a host... (looking for a new host over BLE)")
    except KeyboardInterrupt:
        pass
    finally:
        session.stop = True
        print("\n[main] Cleaning up... (disconnecting Wi-Fi and restoring your network)")
        try:
            disconnect_wifi(dev, prev, joined_ssid)
        except Exception:  # noqa: BLE001
            pass
        print("[main] Done.")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ZZZ FlashShare PC client (macOS / Windows)")
    p.add_argument("--send", nargs="*", help="File(s) to send to the host (Android)")
    p.add_argument("--no-receive", action="store_true", help="Do not poll the outbox for incoming files")
    p.add_argument("--exit-on-disconnect", action="store_true",
                   help="Exit on host disconnect even in BLE mode (default when --ssid is used)")
    p.add_argument("--scan-timeout", type=float, default=8.0, help="BLE scan duration in seconds")
    # Manual mode (skip BLE): pass the SSID/password shown on the Android host screen.
    p.add_argument("--ssid", help="Manual: host Wi-Fi name (shown on the Android screen)")
    p.add_argument("--password", help="Manual: host Wi-Fi password (shown on the Android screen)")
    p.add_argument("--port", type=int, default=53117, help="Host server port (default 53117)")
    p.add_argument("--gateway", help="Manual: host IP (auto-resolved from DHCP if omitted)")
    return p.parse_args()


if __name__ == "__main__":
    if not (IS_MAC or IS_WIN):
        print("[warn] This client is intended for macOS / Windows.")
    try:
        asyncio.run(main(_parse_args()))
    except KeyboardInterrupt:
        print("\n[main] Interrupted")
