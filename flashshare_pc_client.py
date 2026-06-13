"""
ZZZ FlashShare — PC子機(クライアント) (macOS / Python 3.9+)

役割逆転版: Androidを親機(ホットスポット)、Macを子機にする。
macOSはWi-Fi APになれない/不安定なので、Macは「参加する側」に徹する。

フロー:
  1. BLE central(bleak)で親機(Android)をスキャン → サービスUUIDで発見
  2. 接続して Wi-Fi資格情報(SSID/PASS/PORT/HOST) を読み取る
  3. networksetup でその Wi-Fi(AndroidのLocalOnlyHotspot)へ参加
  4. ファイル送信(POST /flashshare/upload) / 受信(送信箱をポーリングしてGET)

事前にAndroid側を「親機になる(受信)」にして「接続待機中」にしておくこと。

依存: pip install -r requirements.txt   (bleak)
実行例:
  python3 flashshare_pc_client.py                 # 受信待ち(送信箱をポーリング)
  python3 flashshare_pc_client.py --send a.jpg b.pdf   # 送信してから受信待ち
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

# ---- プロトコル定数(Flutter側と一致) -------------------------------------- #
SERVICE_UUID = "7a8b0001-2c3d-4e5f-8a9b-0c1d2e3f4a5b"
WIFI_CRED_UUID = "7a8b0002-2c3d-4e5f-8a9b-0c1d2e3f4a5b"

UPLOAD_PATH = "/flashshare/upload"
PING_PATH = "/flashshare/ping"
OUTBOX_PATH = "/flashshare/outbox"
DOWNLOAD_PATH = "/flashshare/download"

INBOX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "FlashShareInbox")

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


# =========================================================================== #
# 1) BLE: 親機を発見して資格情報を取得
# =========================================================================== #
async def find_host(timeout: float = 8.0):
    """サービスUUIDで親機(Android)をスキャンして返す。見つからなければ None。"""
    print(f"[ble] 親機をスキャン中… ({timeout:.0f}秒)")
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
    """親機へ接続し、Wi-Fi資格情報(JSON)を読み取る。"""
    # Windows(WinRT)はGATTサービスをキャッシュする。親機は接続毎にBLEを再構築するため
    # 古いキャッシュが残ると "Could not get GATT services: Unreachable" になる。
    # → use_cached_services=False で毎回フレッシュに探索する。
    kwargs = {"winrt": {"use_cached_services": False}} if IS_WIN else {}
    last = None
    for attempt in range(4):
        try:
            async with BleakClient(device, timeout=20.0, **kwargs) as client:
                raw = await client.read_gatt_char(WIFI_CRED_UUID)
                return json.loads(bytes(raw).decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[ble] 取得失敗(再試行 {attempt + 1}/4): {e}")
            await asyncio.sleep(1.5)
    raise RuntimeError(f"資格情報の取得に失敗: {last}")


# =========================================================================== #
# 2) Wi-Fi参加 (macOS: networksetup / Windows: netsh)
# =========================================================================== #
def _run(cmd: list[str], timeout: int = 20) -> str:
    """コマンド実行して標準出力+標準エラーを返す(失敗しても例外にしない)。
    日本語WindowsのnetshはCP932で出力するため、バイト取得してロケールで復号する。"""
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
    """Wi-FiインターフェイスのデバイスID。macOS=en0系, Windows=インターフェイス名。"""
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
    """周囲のスキャン結果に指定SSIDが見えているか。"""
    if IS_MAC:
        return ssid in _run(["system_profiler", "SPAirPortDataType"], timeout=25)
    if IS_WIN:
        return ssid in _run(["netsh", "wlan", "show", "networks"])
    return True


def resolve_gateway(dev: str) -> str | None:
    """参加中ネットワークのゲートウェイ(=親機)IPを取得。"""
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
                    gw = val  # 最後に見つかったIPv4(通常Wi-Fiアダプタ)
        return gw
    return None


def _win_join(ssid: str, password: str) -> bool:
    """Windows: WPA2プロファイルXMLを生成→追加→接続。"""
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
        # netshの成否メッセージは言語(CP932等)で変わり解析が不安定なため、
        # 「接続要求を出して数秒待つ」だけにし、実際の成否は呼び出し側のpingで判定する。
        for attempt in range(3):
            print(f"[wifi] '{ssid}' へ接続中… ({attempt + 1}/3)")
            out = _run(["netsh", "wlan", "connect", f"name={ssid}", f"ssid={ssid}"])
            if out.strip():
                print(f"[wifi] netsh: {out.strip()}")
            time.sleep(4.0)  # 関連付け+DHCP待ち
            if current_ssid("") == ssid:  # 現在SSIDが一致すれば確実
                print("[wifi] 接続OK")
                return True
        # 確認できなくても実際は繋がっていることがある(ロケール差)。pingに委ねる。
        print("[wifi] 接続要求を送信(疎通はこの後確認します)")
        return True
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def join_wifi(dev: str, ssid: str, password: str) -> bool:
    """Wi-Fi参加。疎通は呼び出し側の ping で最終確認。"""
    # Windows: netshはスキャン事前待ち不要。プロファイル追加→接続→pingで判定。
    if IS_WIN:
        return _win_join(ssid, password)

    # macOS: SSIDがスキャンに出るまで待ってから参加(出ないと参加が失敗しやすい)。
    print(f"[wifi] '{ssid}' を探索中…")
    visible = False
    for i in range(6):
        if ssid_visible(ssid):
            visible = True
            break
        print(f"[wifi]   スキャン中… ({i + 1}/6)")
    if not visible:
        print(f"[wifi] '{ssid}' がスキャンに出ません。")
        print("[wifi] ヒント: Android本体のWi-FiをOFF(自宅ルータから切断)してから")
        print("       「親機になる」にすると、ホットスポットが2.4GHzになりMacから見つかります。")

    subprocess.run(["networksetup", "-setairportpower", dev, "on"], capture_output=True)
    time.sleep(1.0)
    for attempt in range(4):
        print(f"[wifi] '{ssid}' へ接続中… ({attempt + 1}/4)")
        out = _run(["networksetup", "-setairportnetwork", dev, ssid, password])
        low = out.lower()
        if any(k in low for k in ("could not", "failed", "error", "not be found")):
            print(f"[wifi] {out.strip()}")
            time.sleep(3.0)
            continue
        if out.strip():
            print(f"[wifi] {out.strip()}")
        time.sleep(3.0)
        print("[wifi] 接続要求OK")
        return True
    return False


# =========================================================================== #
# 3) ファイル送受信 (HTTP)
# =========================================================================== #
def ping(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}{PING_PATH}", timeout=4) as r:
            return r.status == 200 and b"FLASHSHARE" in r.read()
    except Exception:  # noqa: BLE001
        return False


def disconnect_wifi(dev: str, prev: str | None, joined_ssid: str | None) -> None:
    """終了時にAndroidホットスポットから切断し、可能なら元のWi-Fiへ戻す。"""
    if IS_WIN:
        if joined_ssid:
            _run(["netsh", "wlan", "delete", "profile", f"name={joined_ssid}"])  # 一時プロファイル削除
        if prev:
            print(f"[wifi] 元のWi-Fi '{prev}' へ復帰中…")
            _run(["netsh", "wlan", "connect", f"name={prev}"])
        else:
            _run(["netsh", "wlan", "disconnect"])
    elif IS_MAC:
        if prev and prev != joined_ssid:
            print(f"[wifi] 元のWi-Fi '{prev}' へ復帰中…")
            _run(["networksetup", "-setairportnetwork", dev, prev])
        else:
            # 既知ネットワークへ自動再接続させるため電源を入れ直す。
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
    print(f"\r[send] {name}  完了 ({sent} bytes)        ")


def list_outbox(host: str, port: int):
    """成功: list、到達不可/エラー: None(=切断検知用に空[]と区別する)。"""
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
    """送信入力スレッドと受信ループで共有する接続状態。"""
    def __init__(self) -> None:
        self.host: str | None = None
        self.port: int | None = None
        self.stop = False
        self.lock = threading.Lock()


def receive_loop(host: str, port: int, session: "Session") -> None:
    """送信箱を監視。親機が約8秒応答しなくなったら戻る(=切断 → 上位で再接続)。"""
    print(f"[recv] 送信箱を監視中… 受信は {INBOX} に保存")
    seen: set[str] = set()
    fails = 0
    while not session.stop:
        entries = list_outbox(host, port)
        if entries is None:
            fails += 1
            if fails >= 4:  # ~8秒応答なし
                print("[recv] 親機との接続が切れたようです。")
                return
        else:
            fails = 0
            for entry in entries:
                if entry["id"] in seen:
                    continue
                seen.add(entry["id"])
                print(f"[recv] 受信中: {entry['name']} ({entry['size']} bytes)")
                try:
                    dest = download(host, port, entry)
                    print(f"[recv] 保存: {dest}")
                except Exception as e:  # noqa: BLE001
                    print(f"[recv] 失敗: {e}")
                    seen.discard(entry["id"])
        time.sleep(2.0)


def _parse_paths(line: str) -> list[str]:
    """入力行をファイルパス群に分解(ドラッグ&ドロップやクォートに対応)。
    Windowsは '\\' を保持するため posix=False で分解しクォートを除去する。"""
    try:
        parts = shlex.split(line, posix=not IS_WIN)
    except ValueError:
        parts = [line]
    if IS_WIN:
        parts = [p.strip('"').strip("'") for p in parts]
    return [os.path.expanduser(p) for p in parts if p]


def input_send_loop(session: "Session") -> None:
    """対話送信スレッド: パス入力/ドラッグ&ドロップ → Enter で親機へ送信。'q'で終了。"""
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
            print("[send] まだ親機に接続していません。接続後にもう一度どうぞ。")
            continue
        for p in _parse_paths(line):
            if os.path.isfile(p):
                try:
                    upload(host, port, p)
                except Exception as e:  # noqa: BLE001
                    print(f"[send] 送信失敗: {e}")
            else:
                print(f"[send] ファイルがありません: {p}")


# =========================================================================== #
# メイン
# =========================================================================== #
async def connect_once(args: argparse.Namespace, dev: str):
    """資格情報を得て(BLE自動 or 手動指定) → Wi-Fi参加 → 疎通確認。成功で host:port。失敗でNone。"""
    if args.ssid and args.password:
        # 手動モード: Androidの親機画面に表示されたSSID/パスを直接指定(BLE不要)。
        ssid, password = args.ssid, args.password
        port = args.port
        gateway = args.gateway or "192.168.49.1"
        print(f"[main] 手動指定: SSID='{ssid}' PORT={port}")
    else:
        hosts = await find_host(timeout=args.scan_timeout)
        if not hosts:
            return None
        device, name, rssi = hosts[0]
        print(f"[main] 親機: {name}  rssi={rssi}  ({device.address})")
        try:
            cred = await read_credential(device)
        except Exception as e:  # noqa: BLE001
            print(f"[main] 資格情報の取得に失敗: {e}")
            print("[main] ヒント: WindowsでBLEが不安定な場合は、Android画面のSSID/パスを")
            print("       --ssid \"AndroidShare_xxxx\" --password \"xxxxxxxx\" で手動指定できます。")
            return None
        ssid, password = cred["s"], cred["p"]
        port = int(cred.get("port", 53117))
        gateway = cred.get("h") or "192.168.49.1"
        print(f"[main] 資格情報: SSID='{ssid}' PORT={port} GATEWAY={gateway}")

    if not join_wifi(dev, ssid, password):
        print("[main] Wi-Fi参加に失敗しました。SSID/パスワード/電波状況を確認してください。")
        return None

    # 親機の実IPはDHCPゲートウェイを最優先(親機の自己申告IPは誤ることがある)。
    for _ in range(6):
        cands = [c for c in (resolve_gateway(dev), gateway, "192.168.49.1") if c]
        for h in dict.fromkeys(cands):
            if ping(h, port):
                print(f"[main] 親機サーバに到達 OK ({h}:{port})")
                return (h, port, ssid)
        time.sleep(1.5)
    print(f"[main] 親機サーバに到達できません (:{port})。")
    return None


async def main(args: argparse.Namespace):
    dev = wifi_device()
    prev = current_ssid(dev)
    session = Session()
    sent = False

    # 対話送信スレッド(ファイルをドラッグ&ドロップ→Enterで送信)。
    t = threading.Thread(target=input_send_loop, args=(session,), daemon=True)
    t.start()
    print("=" * 60)
    print("[操作] 親機(Android)へ送るには、ファイルをこのターミナルに")
    print("       ドラッグ&ドロップ(またはパス入力)して Enter。")
    print("       受信は自動。終了は 'q' + Enter または Ctrl+C。")
    print("=" * 60)

    # 手動指定モードは親機再起動でSSID/パスが変わると再接続不可 → 切断で終了が妥当。
    # BLEモードは再探索で新しい資格情報を取得できる → 再接続を継続。
    manual_mode = bool(args.ssid and args.password)
    exit_on_disconnect = args.exit_on_disconnect or manual_mode

    joined_ssid: str | None = None
    try:
        while not session.stop:
            target = await connect_once(args, dev)
            if not target:
                print("[main] 5秒後に親機を再探索します… (Android側を「接続待機中」に)")
                await asyncio.sleep(5)
                continue
            session.host, session.port, joined_ssid = target

            # 起動引数 --send は初回接続時に一度だけ送信。
            if not sent and args.send:
                for fp in args.send:
                    if os.path.isfile(fp):
                        upload(session.host, session.port, fp)
                    else:
                        print(f"[send] ファイルがありません: {fp}")
                sent = True

            # 受信監視(対話送信は別スレッドで並行)。親機が切れたら戻ってくる。
            receive_loop(session.host, session.port, session)
            session.host = None
            if session.stop:
                break
            if exit_on_disconnect:
                if manual_mode and not args.exit_on_disconnect:
                    print("[main] 親機が切断されました(手動指定はSSID/パスが変わるため再接続不可)。終了します。")
                else:
                    print("[main] 親機が切断されました。終了します。")
                break
            print("[main] 親機を再探索します…(BLEで新しい親機を探索)")
    except KeyboardInterrupt:
        pass
    finally:
        session.stop = True
        print("\n[main] 終了処理中… (Wi-Fiを切断します)")
        try:
            disconnect_wifi(dev, prev, joined_ssid)
        except Exception:  # noqa: BLE001
            pass
        print("[main] 終了しました。")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ZZZ FlashShare PC子機 (macOS / Windows)")
    p.add_argument("--send", nargs="*", help="親機(Android)へ送るファイル")
    p.add_argument("--no-receive", action="store_true", help="受信(送信箱ポーリング)を行わない")
    p.add_argument("--exit-on-disconnect", action="store_true",
                   help="BLEモードでも親機切断で終了する(手動--ssid指定時は既定で終了)")
    p.add_argument("--scan-timeout", type=float, default=8.0, help="BLEスキャン秒数")
    # 手動モード(BLE回避): Android親機画面のSSID/パスを直接指定する。
    p.add_argument("--ssid", help="手動指定: 親機のWi-Fi名(Android画面に表示)")
    p.add_argument("--password", help="手動指定: 親機のWi-Fiパスワード(Android画面に表示)")
    p.add_argument("--port", type=int, default=53117, help="親機サーバのポート(既定53117)")
    p.add_argument("--gateway", help="手動指定: 親機のIP(省略時はDHCPから自動解決)")
    return p.parse_args()


if __name__ == "__main__":
    if not (IS_MAC or IS_WIN):
        print("[warn] このクライアントはmacOS/Windows向けです。")
    try:
        asyncio.run(main(_parse_args()))
    except KeyboardInterrupt:
        print("\n[main] 中断されました")
