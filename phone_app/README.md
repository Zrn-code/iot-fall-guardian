# phone_app — 守護者控制中心 App（手機）

> Wearable Guardian 系統的 **ASN 守護者裝置**。守護者以 ThingsBoard 帳號登入，監看名下所有配戴者的
> 即時生理／情境／位置與告警，並可下行「呼叫／前往中」命令、上傳自身 GPS 供「附近優先」判斷。
>
> 系統總覽見根目錄 [README.md](../README.md)；架構細節見 [docs/architecture.md](../docs/architecture.md)。

## 角色定位

- **守護者 Guardian**：監看者，本身**沒有 BPM／情境**。
- 以 **TB 帳號（CUSTOMER_USER）** 真正登入，看得到所屬 customer（`Guardian Ops`）名下全部配戴者。
- 是純 ThingsBoard 客戶端：所有資料讀寫走 TB REST API，**不經過 posture-api**。

## 技術棧

| 項目 | 內容 |
| --- | --- |
| 框架 | Flutter（Android / iOS） |
| 地圖 | `flutter_map` + `latlong2` |
| 定位 | `geolocator`（上傳守護者自身 GPS、算與配戴者距離） |
| 認證 | TB `/api/auth/login` → JWT + refreshToken；401 自動 refresh（`TbSession` / `TbClient`） |
| 本地儲存 | `shared_preferences`（TB URL、session） |
| 網路 | `http` |

## 功能

### 登入

- 輸入 TB URL + email + 密碼 → `POST /api/auth/login` → `GET /api/auth/user` 解出 `customerId` 與正規 email。
- 預設帳號（demo）：`guardian1@guardian.local`，密碼於 provision 時設定（見 `scripts/provision_thingsboard.py` 的 `--guardian-password`）。
- TB URL 依實機網段填（例：`http://192.168.50.23:18080`）。

### 三個 Tab

| Tab | 來源 | 內容 |
| --- | --- | --- |
| **配戴者** | `GET /api/customer/{cid}/devices?pageSize=200`（type `wearer`） + 最新 telemetry | 每位配戴者一張卡：即時 `bpm` / `situation` / `escalation`；有告警時顯示「我看到了·前往中」按鈕 |
| **地圖** | 配戴者位置 + 守護者自身 GPS | `flutter_map` 標出所有配戴者與自己；本機算距離，**≤ 50 m 視為附近**（`kNearbyRadiusM = 50`）標黃強調 |
| **告警** | `GET /api/alarms?searchStatus=ACTIVE&sortProperty=createdTime&sortOrder=DESC` | 進行中告警清單（型別、嚴重度、配戴者） |

> 排序策略：**告警中 → 附近 → 依名稱**。附近的新告警會額外**強震 + 橫幅標「【附近】」**提示。

### 守護者動作

| 動作 | 行為 |
| --- | --- |
| **我看到了·前往中** | `sendWearerCommand(wearerId, 'dispatch')` → 寫 `guardian_cmd=dispatch` 到該配戴者 device 的 `SHARED_SCOPE` → 手錶輪詢讀到 → 顯示「守護者前往中」 |
| **上傳自身 GPS** | `uploadGuardianGps(...)` 定時把守護者手機 GPS 寫進對應的 `Guardian_<name>` device（CUSTOMER_USER 可直接寫自己的 device timeseries，免 device token） |

> 一鍵**呼叫**手錶（讓手錶大聲響鈴）目前由 **TB 儀表板**的 `two_segment_button` 觸發（寫 `guardian_cmd=call`）；手機端 App 走「派遣」通道。完整下行機制見 [docs/architecture.md](../docs/architecture.md) §4。

## 安裝

```powershell
adb connect <phone-IP>:5555
cd phone_app
flutter pub get
flutter build apk --debug
flutter install
```

## 端到端搭配

1. 後端 `POST /api/demo/inject?scenario=collapse&worker_id=W-001` 注入倒地事件。
2. TB `GuardianRules` 建 `MedicalCollapse`（CRITICAL）告警 + Notification Center 推播。
3. 本 App 登入後在「告警」Tab 看到該告警、地圖上配戴者標紅；按「我看到了·前往中」→ 手錶切「守護者前往中」。

> 守護者帳號由 `scripts/provision_thingsboard.py` 佈建（`ensure_customer_user`）。
