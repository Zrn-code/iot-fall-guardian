# wear_os_app — 配戴者手錶 App（Galaxy Watch / Wear OS）

> Wearable Guardian 系統的 **ASN 末端配戴者裝置**。連續守護被照護者：採集 IMU + 心率、呼叫邊緣 AI 推論、
> 把情境結果直送 ThingsBoard、本地偵測跌倒並跑 SOS 倒數、接收守護者的呼叫／派遣命令。
>
> 系統總覽見根目錄 [README.md](../README.md)；架構細節見 [docs/architecture.md](../docs/architecture.md)。

## 角色定位

- **配戴者 Wearer**：戴錶被守護的人，產生 HR / IMU / 情境 / GPS。
- 手錶**不自己判定告警升級**——它把推論結果與 GPS 送進 ThingsBoard，由 `GuardianRules` 規則鏈做權威判定；
  手錶端只保留同門檻的本地 hint 來驅動自己的 UI（倒數、響鈴）。

## 技術棧

| 項目 | 內容 |
| --- | --- |
| 框架 | Flutter（Wear OS） |
| IMU | `sensors_plus`：accelerometer + gyroscope，採樣週期 20 ms（≈ 50 Hz） |
| 心率 | Wear OS Health Services，經 Kotlin method channel `wear_os/heart_rate` 取得 bpm |
| 定位 | `geolocator`（背景 GPS） |
| 震動 | `vibration`（SOS／呼叫的觸覺回饋） |
| 本地儲存 | `shared_preferences`（MN URL / Wearer / TB token） |
| 權限 | `permission_handler` |
| 網路 | `http`（REST 到 posture-api 與 ThingsBoard） |

## 行為流程

```text
點圓鈕開始錄製
  → IMU 50 Hz 緩衝（_imuBuffer），HR 由原生 method channel 取得
  → 停止條件：
      • 點按結束           (tap)
      • 3 秒靜止（變異 < 0.02 g²）  (timeout)
      • 錄滿 30 秒          (max_recording)
      • 自由落體→撞擊 自動觸發  (fall)：|acc| < 0.45 g 之後 > 1.8 g
  → POST /api/infer {samples, hr_samples} → {situation, confidence, features}
  → POST /api/v1/{tbToken}/telemetry {situation, features, bpm, lat, lon, active}（直送 TB）
  → 依 situation 進入 SOS 狀態機
```

### 狀態機 `WearStage`

`idle → recording → sending → ok | error | alerting → sosSent | acked | called`

| 狀態 | 觸發 | 行為 |
| --- | --- | --- |
| `alerting`（ASK_OK） | 推論 FALL 且未達崩潰門檻 | 全螢幕 **15 秒倒數**（`_alertCountdownSeconds = 15`），每秒震動；點按取消 → 送 `NORMAL` 清告警；倒數歸零 → 自動送 `FALL`（no_response）求救 |
| `sosSent`（SOS_COLLAPSE） | 本地門檻判定崩潰（HR 飆高／撞擊後靜止） | 直接顯示「SOS 已送出」數秒後復位（權威 SOS 由 TB 規則鏈建 MedicalCollapse） |
| `acked`（守護者前往中） | 輪詢讀到 `guardian_cmd=dispatch` | 顯示「守護者前往中」+ 回寫 `ack_seen`，10 秒後復位 |
| `called`（守護者呼叫） | 輪詢讀到 `guardian_cmd=call` | 大聲響鈴震動 `[0,500,200,500,200,500,200,800]` + 全螢幕「守護者正在呼叫你」+ 回寫 `call_seen`，12 秒後復位 |

> 本地 escalation（`_localEscalation`）只是 UI 提示，**權威判定在 TB**：`FALL` 且 `hr_above_baseline>40`／`hr_max>150`／撞擊後持續靜止 → COLLAPSE；TB 另有「20 秒無回應自動升級」的伺服端安全網。

### 下行命令輪詢

- 每 5 秒 heartbeat（`Timer.periodic(5s)`）送最小 telemetry（`active=true`）並 `GET /api/v1/{token}/attributes?sharedKeys=guardian_cmd,guardian_ack,guardian_cmd_ts`。
- `guardian_cmd` 變動（依 `guardian_cmd_ts`）→ 進入 `called`（呼叫）或 `acked`（派遣）。
- 守護者端寫入這些 shared attribute 的來源：手機 App 的「我看到了·前往中」與儀表板的 `two_segment_button`。

## 設定項（齒輪）

App 內設定對話框可填，存進 `shared_preferences`：

| 欄位 | 預設 | 說明 |
| --- | --- | --- |
| MN URL | `http://140.113.123.43:8000/api/infer` | posture-api 的 `/api/infer`（或相容別名 `/api/evaluate_lift`） |
| Wearer | `W-001` | 邏輯配戴者 id（送 telemetry 帶 `worker_id`） |
| TB Token | `<YOUR_TB_DEVICE_TOKEN>` | 該配戴者的 ThingsBoard device token；App 內設定填入，或重 provision 後見 [data/worker_tokens.json](../data/worker_tokens.json)（未納入版控） |

> ⚠️ 重新跑 `scripts/provision_thingsboard.py` 若重建 wearer 裝置會換 token；目前裝置是 idempotent（found 不重建）token 穩定，但更新手錶前請先比對 `worker_tokens.json`。

## 安裝

```powershell
adb connect <watch-IP>:5555
cd wear_os_app
flutter pub get
flutter build apk --debug
flutter install
```

輔助腳本：

- [scripts/install_wear_os_app.ps1](../scripts/install_wear_os_app.ps1) — 安裝 APK 到手錶。
- [scripts/watch_logcat.ps1](../scripts/watch_logcat.ps1) — 串流手錶 logcat 方便除錯。

> 沒有實體手錶時，可用 `POST /api/demo/inject?scenario=...&worker_id=W-001` 在後端注入各情境，端到端跑完 TB 規則鏈（見根 README [§7](../README.md#7-一鍵啟動與-demo)）。
