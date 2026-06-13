# ZZZ FlashShare Tools — PC クライアント

[English](./README.md) / 日本語

Android の **ZZZ FlashShare** アプリ(親機)と、PC(macOS / Windows)でファイルを送受信するためのコマンドラインツールです。

PC は **子機(クライアント)** として動作します。Android を **親機(ホットスポット)** にして、PC がそこへ参加し、BLE で接続情報を受け取って Wi-Fi 経由でファイルをやり取りします。インターネットやアカウントは不要です。

> なぜ Android が親機か:macOS / Windows は「Wi-Fi アクセスポイント + BLE 公開」を安定して両立できないため、確実に親機になれる Android を親機にし、PC は「参加する側」に徹します。

---

## 動作環境

- **OS**: macOS または Windows
- **Python**: 3.9 以上
- **Bluetooth**: PC に内蔵 or USB(BLE 対応)。**Bluetooth を ON** にしておくこと
- 相手: **ZZZ FlashShare** アプリを入れた Android 端末

## インストール

```bash
pip install -r requirements.txt
```

(依存は `bleak` のみです)

---

## 使い方

### 1. Android 側(親機)
1. ZZZ FlashShare アプリを開く
2. **「親機になる(受信)」** を選ぶ
3. **「接続待機中」** の状態にしておく
   - ヒント: Android 本体の Wi-Fi を一度 **OFF**(自宅ルータから切断)してから親機にすると、ホットスポットが 2.4GHz になり PC から見つかりやすくなります

### 2. PC 側(子機)
```bash
python3 flashshare_pc_client.py
```
起動すると自動で:
1. BLE で親機(Android)をスキャンして発見
2. Wi-Fi 接続情報(SSID / パスワード / ポート)を読み取り
3. その Android ホットスポットへ参加
4. 受信待ち(親機の送信箱を監視)に入る

### ファイルを送る
- **ドラッグ&ドロップ**: 送りたいファイルをターミナルにドロップして **Enter**
- **パス入力**: ファイルパスを入力して Enter(複数可・スペース区切り)
- **起動時に指定**:
  ```bash
  python3 flashshare_pc_client.py --send a.jpg b.pdf
  ```

### ファイルを受け取る
- 受信は**自動**です。親機(Android)から送られたファイルは、スクリプトと同じ場所の **`FlashShareInbox/`** フォルダに保存されます

### 終了
- ターミナルで **`q` + Enter**、または **Ctrl+C**
- 終了時に自動で Android ホットスポットから切断し、可能なら元の Wi-Fi に復帰します

---

## オプション

| オプション | 説明 |
|---|---|
| `--send <ファイル...>` | 起動直後に指定ファイルを送信(その後は受信待ち) |
| `--no-receive` | 受信(送信箱ポーリング)を行わない |
| `--exit-on-disconnect` | 親機が切断したら終了する(BLEモードでも) |
| `--scan-timeout <秒>` | BLE スキャン時間(既定 8 秒) |
| `--ssid <名前>` | 手動指定: 親機の Wi-Fi 名(BLE を使わない) |
| `--password <パス>` | 手動指定: 親機の Wi-Fi パスワード |
| `--port <番号>` | 親機サーバのポート(既定 53117) |
| `--gateway <IP>` | 手動指定: 親機の IP(省略時は自動解決) |

### 手動モード(BLE を使わない)
BLE が不安定なとき(特に Windows)は、Android の親機画面に表示される **SSID / パスワード**を直接指定できます:

```bash
python3 flashshare_pc_client.py --ssid "AndroidShare_xxxx" --password "xxxxxxxx"
```

> 手動モードでは、親機を再起動すると SSID / パスワードが変わるため、切断されると再接続できません(その場合は終了します)。

---

## 仕組み(概要)

1. **発見**: `bleak` で BLE スキャンし、サービス UUID `7a8b0001-…` を持つ親機を探す
2. **資格情報**: 親機の GATT 特性 `7a8b0002-…` から Wi-Fi 情報(JSON)を読む
3. **参加**: macOS は `networksetup`、Windows は `netsh`(WPA2 プロファイル)でホットスポットへ参加
4. **送信**: `POST /flashshare/upload` でアップロード
5. **受信**: `GET /flashshare/outbox` をポーリングし、新着を `GET /flashshare/download` で取得

到達先の親機 IP は DHCP ゲートウェイを最優先で解決します(既定フォールバックは `192.168.49.1`)。

---

## トラブルシューティング

- **親機が見つからない**
  - PC の Bluetooth が ON か確認
  - Android 本体の Wi-Fi を OFF にしてから「親機になる」(ホットスポットが 2.4GHz になり見つかりやすい)
  - macOS: ターミナルに Bluetooth / 位置情報の権限を許可(初回ダイアログで許可)
- **Wi-Fi 参加はできるがサーバに到達できない**
  - Android アプリが「接続待機中 / 受信可能」な状態か確認
  - `--gateway` で親機 IP を手動指定して再試行
- **Windows で BLE が不安定**
  - 手動モード `--ssid / --password` を使う
- **受信ファイルの場所**
  - スクリプトと同じフォルダの `FlashShareInbox/`

---

## ライセンス / 提供

SMOLT STAGE, K.K. — ZZZ FlashShare
