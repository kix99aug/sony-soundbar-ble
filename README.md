# sony-soundbar-ble

在 Windows 上透過藍牙低功耗（BLE）控制 Sony 聲霸的音量，不需要官方 App。

- **對應機種**：Sony HT-B600（BRAVIA Theatre Bar 6）
- **平台**：Windows 10 / 11
- **功能**：讀取／設定／增減音量，並在畫面上顯示音量 OSD（用實體遙控器調整時也會顯示）

Sony 沒有公開 API，所有指令都是從 iOS App 的藍牙封包（btsnoop）逆向分析出來的。

## 安裝

需要 [uv](https://docs.astral.sh/uv/) 與 Python 3.13。

```powershell
git clone <repo-url> sony-soundbar-ble
cd sony-soundbar-ble
uv sync
```

聲霸的 MAC 位址寫死在 `bravia_control.pyw` 的 `DEVICE_MAC`，換成你自己的裝置位址。

## 使用方式

```powershell
uv run python bravia_control.pyw status     # 顯示目前音量
uv run python bravia_control.pyw set 20     # 設定音量（0–100）
uv run python bravia_control.pyw up 2       # 音量 +2（預設 1）
uv run python bravia_control.pyw down       # 音量 -1
uv run python bravia_control.pyw daemon     # 前景啟動常駐服務（會印出 log）
```

### 常駐服務（daemon）

每次直接連線聲霸要 3–5 秒。daemon 會保持 BLE 連線，並在 `127.0.0.1:9105` 接收指令，讓 CLI 幾乎即時回應：

- CLI 會先嘗試連 daemon；daemon 沒在跑或沒連上聲霸時，自動改用直接連線。
- daemon 會監看連線狀態，斷線或卡住時自動重連，必要時重新啟動自己。
- 用實體遙控器調音量時，畫面上會跳出音量 OSD（點擊穿透、不出現在 Alt-Tab）。

TCP 協定很簡單，一個連線送一行指令（`status`、`set 20`、`up 2`…），回應 `OK <音量>` 或 `ERROR: …`，方便接到 AutoHotkey、Stream Deck 等工具。

### 開機自動啟動

`bravia_daemon.xml` 是 Windows 工作排程器的設定範本，登入時用 `pythonw.exe` 在背景啟動 daemon（不會出現視窗）。先執行過 `uv sync`，再以系統管理員身分在 repo 目錄執行：

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

腳本會自動填入 repo 位置、Python 路徑與目前使用者後註冊工作。搬移 repo 或更新 Python 後重跑一次即可。

管理：

```powershell
schtasks /Run /TN bravia_daemon    # 啟動
schtasks /End /TN bravia_daemon    # 停止
```

daemon 的 log 寫在 `bravia_daemon.log`，出問題時先看這裡。

## 協定摘要

音量位於 Sony 自訂的 GATT 服務下：寫入 `5b833c66-6bc7-4802-8e9a-723ceca4bd8f`，從 `5b833c67-6bc7-4802-8e9a-723ceca4bd8f` 接收通知。

- **讀取**：送出 17 bytes 的查詢 `3e070000000010000001000a025661db3c`，在通知資料中尋找 `0a025661120208`，其後第 7 個 byte 就是音量。
- **寫入**：必須依序送出四個封包（標頭 → 音量與校驗碼 → ACK `3e0100000000023c` → 結束交易 `3e0100000000013c`），少了後兩個，聲霸的狀態機會卡住。

更詳細的實作細節與注意事項見 [`CLAUDE.md`](CLAUDE.md)。

## 免責聲明

本專案與 Sony 無關，為非官方的逆向工程成果，使用風險自負。
