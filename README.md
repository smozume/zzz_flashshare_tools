# ZZZ FlashShare Tools — PC Client

English / [日本語](./README.ja.md)

A command-line tool to send and receive files between a PC (macOS / Windows) and an Android device running the **ZZZ FlashShare** app (acting as the host).

The PC runs as the **client**. The Android device becomes the **host (hotspot)**; the PC joins it, reads the connection details over BLE, and transfers files over Wi-Fi. No internet connection or account is required.

> Why Android is the host: macOS / Windows can't reliably act as a Wi-Fi access point **and** advertise BLE at the same time. Android does both well, so it hosts and the PC simply joins.

---

## Requirements

- **OS**: macOS or Windows
- **Python**: 3.9+
- **Bluetooth**: built-in or USB (BLE-capable), and **turned ON**
- A peer Android device running the **ZZZ FlashShare** app

## Install

```bash
pip install -r requirements.txt
```

(The only dependency is `bleak`.)

---

## Usage

### 1. On Android (host)
1. Open the ZZZ FlashShare app
2. Choose **"Become host (receive)"**
3. Leave it on the **"Waiting for connection"** screen
   - Tip: turn the phone's Wi-Fi **OFF** (disconnect from your home router) before becoming the host. The hotspot then runs on 2.4 GHz and is easier for the PC to find.

### 2. On the PC (client)
```bash
python3 flashshare_pc_client.py
```
On launch it automatically:
1. Scans for the Android host over BLE
2. Reads the Wi-Fi credentials (SSID / password / port)
3. Joins the Android hotspot
4. Starts watching for incoming files

### Sending files
- **Drag & drop**: drop files onto the terminal and press **Enter**
- **Type a path**: enter a file path and press Enter (multiple allowed, space-separated)
- **At launch**:
  ```bash
  python3 flashshare_pc_client.py --send a.jpg b.pdf
  ```

### Receiving files
- Receiving is **automatic**. Files sent from the Android host are saved to the **`FlashShareInbox/`** folder next to the script.

### Quitting
- Press **`q` + Enter**, or **Ctrl+C**
- On exit it disconnects from the Android hotspot and, when possible, reconnects to your previous Wi-Fi network.

---

## Options

| Option | Description |
|---|---|
| `--send <files...>` | Send the given files once on startup, then keep receiving |
| `--no-receive` | Do not poll the host outbox for incoming files |
| `--exit-on-disconnect` | Exit when the host disconnects (even in BLE mode) |
| `--scan-timeout <sec>` | BLE scan duration (default 8) |
| `--ssid <name>` | Manual mode: host Wi-Fi name (skips BLE) |
| `--password <pass>` | Manual mode: host Wi-Fi password |
| `--port <num>` | Host server port (default 53117) |
| `--gateway <ip>` | Manual mode: host IP (auto-resolved if omitted) |

### Manual mode (no BLE)
When BLE is unreliable (notably on Windows), you can pass the **SSID / password** shown on the Android host screen directly:

```bash
python3 flashshare_pc_client.py --ssid "AndroidShare_xxxx" --password "xxxxxxxx"
```

> In manual mode, restarting the host changes its SSID / password, so the client cannot reconnect after a disconnect (it will exit instead).

---

## How it works

1. **Discover**: BLE scan via `bleak` for a host advertising service UUID `7a8b0001-…`
2. **Credentials**: read the Wi-Fi info (JSON) from GATT characteristic `7a8b0002-…`
3. **Join**: connect to the hotspot via `networksetup` (macOS) or `netsh` WPA2 profile (Windows)
4. **Send**: `POST /flashshare/upload`
5. **Receive**: poll `GET /flashshare/outbox`, fetch new items via `GET /flashshare/download`

The host IP is resolved from the DHCP gateway first (default fallback `192.168.49.1`).

---

## Troubleshooting

- **Host not found**
  - Make sure the PC's Bluetooth is ON
  - Turn the phone's Wi-Fi OFF before becoming the host (puts the hotspot on 2.4 GHz)
  - macOS: grant Bluetooth / Location permission to the Terminal (allow on the first prompt)
- **Joins Wi-Fi but can't reach the server**
  - Confirm the Android app is on the "Waiting / ready to receive" screen
  - Retry with an explicit host IP via `--gateway`
- **BLE unstable on Windows**
  - Use manual mode `--ssid / --password`
- **Where received files go**
  - The `FlashShareInbox/` folder next to the script

---

## License / Provided by

MIT License — see [LICENSE](LICENSE).
