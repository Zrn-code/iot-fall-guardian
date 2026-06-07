# AI Platform = Azure IoT Edge

The project's **AI platform**: Azure IoT Hub centrally manages and deploys the
AI inference module (`posture-api`) to an **edge node** — your always-on dorm
computer (`140.113.123.43`). Inference runs at the edge (low latency / offline);
Azure is the control plane that deploys + tracks the module.

```
Azure 雲端: IoT Hub(F1 免費) + ACR  ──部署 postureApi 模組──►  宿舍電腦 140.113.123.43 (edge)
                                                              ├─ postureApi 模組 (:8000)
                                                              └─ docker-compose: ThingsBoard (:18080)
手錶/手機 ── 走校園網路 ──► 140.113.123.43:8000 (推論) / :18080 (戰情)
```

> **為什麼這在學校 WiFi 能用**：`140.113.123.43` 是校園路由 IP，手錶連它是「經校園路由到伺服器」，不是「同一 AP 的兩台裝置互連」，所以不受 AP 用戶端隔離影響。

---

## Runbook

### 0. 前置

```powershell
# Azure CLI + 登入（gmail 可開標準免費帳號）
az login
az extension add --upgrade -n azure-iot
# Docker Desktop 要在執行中
```

### 1. 建 Azure 資源（IoT Hub F1 + Edge 裝置 + ACR）→ 自動寫 .env

```powershell
.\cloud\azure\setup_azure.ps1
```

⚠️ `-HubName` / `-AcrName` 是**全域唯一**，被占用就換名：
`\.cloud\azure\setup_azure.ps1 -HubName my-hub-123 -AcrName myacr123`。
⚠️ 每個訂閱只能有 **1 個免費 F1 IoT Hub**。

### 2. 打包 posture-api 模組映像 → 推 ACR（重用現有 Dockerfile）

```powershell
.\cloud\azure\build_and_push.ps1
```

### 3. 產生部署清單（把 .env 代入模板）

```powershell
.\cloud\azure\gen_deployment.ps1     # -> deployment.generated.json
```

### 4. 在宿舍電腦用模擬器跑模組（Windows 家用版無 EFLOW，用 iotedgehubdev）

```powershell
.\cloud\azure\run_edge.ps1            # setup + start postureApi 模組
.\cloud\azure\run_edge.ps1 -SetModules  # 另把清單推到 IoT Hub（portal 可截圖佐證）
```

`iotedgehubdev setup` 可能需要**系統管理員 PowerShell**。

### 5. 開防火牆 + 起 ThingsBoard + 驗證

```powershell
# 系統管理員 PowerShell：放行入站 8000 / 18080
.\cloud\azure\open_firewall.ps1
# ThingsBoard（與模組分開，docker-compose 跑 postgres + tb）
docker compose up -d postgres thingsboard-ce
# 本機驗證推論
.\.venv\Scripts\python.exe scripts\verify_live.py
```

**Demo 前必測（最重要）**：拿一支手機**連教室 WiFi**，開
`http://140.113.123.43:8000/health` 與 `http://140.113.123.43:18080`。
通了走 live；不通就走下面的保險路徑。

---

## 保險路徑（絕不出包，不靠任何外部裝置連線）

後端既有的 demo 注入：在宿舍電腦本機把合成情境灌進 ThingsBoard，戰情儀表板用
**該電腦的瀏覽器**直接演完整告警升級流程。

```powershell
Invoke-RestMethod -Method Post "http://localhost:8000/api/demo/inject?scenario=collapse&worker_id=W-001"
# 然後在瀏覽器看 ThingsBoard (localhost:18080) 的 Guardian Live Ops 儀表板
```

---

## 兩支手錶 app

- **wear_os_trainer_app**（訓練）：選標籤→錄→`POST /api/training`，餵訓練資料。
- **wear_os_app**（日常）：偵測 `/api/infer`、只顯示結果。
- 兩者 server URL 都指 `140.113.123.43:8000`（已是預設）。

## 重訓 → 更新模組

```powershell
.\.venv\Scripts\python.exe scripts\train_fall_model.py   # 產新 data/models/*.joblib
.\cloud\azure\run_edge.ps1                                # 重啟模組即吃新模型（bind-mount）
```

## 檔案

| 檔案 | 作用 |
|---|---|
| `setup_azure.ps1` | 建 RG / IoT Hub F1 / Edge 裝置 / ACR，寫 `.env` |
| `build_and_push.ps1` | build posture-api 模組映像 → 推 ACR |
| `deployment.template.json` | IoT Edge 部署清單模板（`${VARS}`） |
| `.env.example` | `.env` 範本（setup 會自動產生 `.env`） |
| `gen_deployment.ps1` | 代入 `.env` → `deployment.generated.json` |
| `run_edge.ps1` | iotedgehubdev 模擬器 setup + start（+`-SetModules`） |
| `open_firewall.ps1` | 放行入站 8000 / 18080（需系統管理員） |

## 成本 / 限制

- IoT Hub **F1 永久免費**；ACR Basic 小額（demo 後 `az acr delete -n <acr>`）；模擬器免費。
- iotedgehubdev 是 **dev 模擬器**，非生產 runtime，但足以展示「Azure 把模組部署到邊緣」。
- 模型用 **bind-mount**（同機 demo 最省事）；要部署到別台再改成 build 階段 baked。
