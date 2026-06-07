# 進度記錄：雙角色重設計（配戴者 Wearer / 守護者 Guardian）

> 2026-05-30 起。把 TB 的「Guardian_Device_001 + Worker_W-00X」混淆模型改成清楚兩角色，手機改 TB 帳號登入控制中心。

## 2026-06-01 架構搬遷：API server = 純 AI 推論;TB = 編排中樞(已實機驗證)

使用者原則:API server 只做 AI 推論,GPS/escalation/告警/清除/升級/訊息/雙向/統計全交給 TB。

**已完成並用實機 TB(CE 4.3)驗證:**
- **A. TB 清理乾淨**:只剩 Guardian 專案(Wearer_W-001/002/003、wearer/default profile、GuardianRules root、Guardian Live Ops、Guardian Ops customer、guardian1/2 user)+ 系統 Root Rule Chain。刪掉舊倉儲殘留(Smart_Carton_001/worker profile/PostureHazardRules/Smart Warehouse Workers Live)、TB demo 範例(Test Device A1~C1/DHT11/RaspberryPi/Thermostat T1T2/thermostat profile+chain/Firmware/Software/Rule Engine Statistics/Thermostats/Customer A~C+users)、手勢解鎖(AIoT Gesture Unlock Dashboard/gesture-phone-01)。刪除順序:dashboards→devices→customers→非root rulechains→profiles。
- **B0. server 瘦身**:`main.py` 改成只有 `/api/infer`(+`/api/evaluate_lift` 相容別名,回 `{situation,confidence,proba,features}`,無 escalation/GPS/告警)、`/api/training*`、`/api/demo/inject`(demo-only:推論後用 wearer token 把結果送 TB,讓 TB 規則鏈跑 escalation)、`/health`。**刪掉 `notify.py`、`positions.py`**(廣播/位置/escalation 都搬走)。schemas 移除 EvaluateLift/WorkerPosition/WorkerAlert/WorkersPositions,新增 InferRequest/InferResponse。`tb_client.py` 不動(MQTT bridge 仍停用)。`.env` TB_DEVICE_TOKEN 仍空。
- **B0. escalation 搬進 TB**:`decide_escalation` 改寫成 GuardianRules 的 JS transform node(`JS_TRANSFORM`)——吃 situation+features 算 escalation(FALL→hr_above_baseline>40/hr_max>150/post_impact_stillness<0.05→SOS_COLLAPSE 否則 ASK_OK;HEAT_STRAIN→hr_above_baseline>30→SOS_HEAT),寫 risk_score/is_emergency/last_situation/last_escalation/last_hr_max。**驗證**:inject collapse→server 回 situation=FALL(無 escalation)→TB 自己算出 escalation=SOS_COLLAPSE 並建 MedicalCollapse。
- **B1. 自動清除 + 統計**:situation==NORMAL → 3 個 TbClearAlarmNode 清 MedicalCollapse/HeatStrain/FallSuspected;JS 在 NORMAL 把 risk_score/hazard 歸零、寫 recovered/last_recovery_at。**驗證**:collapse→normal 後 active=0、cleared 帶 clearTs+duration(TB 原生告警歷史,不刪)。**坑:ClearAlarm 的 alarmDetailsBuildJs 用 JS 但 scriptLang=TBEL 會 silently 失敗 → 改成 `alarmDetailsBuildTbel:"return metadata;"` 才清得掉。**
- **B2. TB 內時間升級鏈**:ASK_OK filter(node6)→ 同時(a)建 FallSuspected WARNING、(b)原始 telemetry 進 `TbMsgDelayNode`(20s)→ `TbGetAttributesNode`(把最新 last_situation 載進 metadata)→ filter `metadata.last_situation!=='NORMAL'` → 建 MedicalCollapse CRITICAL(overwrite)。**驗證兩 case**:無回應→20s 後自動升級 MedicalCollapse;6s 後送 recovery(normal)→不誤升(active=0)。**坑:delay/alarm node 輸出的 msg 是告警 details 不含 escalation;且要從 node6 原始 telemetry 接 delay,不要從 alarm node 接。重 provision 會重置 debug 旗標。**
- **B3**:guardian device profile + `Guardian_guardian1/2` device(type=guardian)+ token(`data/guardian_tokens.json`);guardian USER -Manages-> wearer DEVICE 共 6 條 EntityRelation;dashboard 加 Guardians 地圖、恢復統計表、已清除告警表。**手機上傳守護者 GPS 已驗證**:customer-user 可用 REST `POST /api/plugins/telemetry/DEVICE/{id}/timeseries/ANY?scope=ANY` 寫自己 Guardian device(免 device token)。附近判斷採務實退路=phone 端用 TB 上各裝置位置算(已可行),廣播仍走 TB shared attr。
- **B4 雙向(已驗證全迴圈)**:手機(customer-user)`POST .../DEVICE/{wearerId}/SHARED_SCOPE {guardian_cmd:dispatch}` → 手錶用 device token `GET /api/v1/{token}/attributes?sharedKeys=...` 讀到 → 顯示「守護者前往中」+ 回寫 `ack_seen` telemetry → 規則鏈(node17 filter ack_seen → node18 transform → save)寫 `rescue_state=guardian_enroute`。**坑:transform node 要再接到 Save Timeseries(node0)才會落地。**

**App 改動(三 app dart analyze 乾淨、APK 已 build):**
- `wear_os_app`:`_sendEvaluate` 改兩步——`POST /api/infer`(只拿 situation+features)→ `_postTb()` 把 situation+features+GPS 直接送 TB;`_localEscalation()` 只為驅動手錶 UI(權威判定在 TB);heartbeat 改成只 `_postTb`(不再打 /api/worker_position)+ `_pollGuardianCommand()` 讀 shared attr;新增 `WearStage.acked`「守護者前往中」畫面 + 回寫 ack_seen;`_cancelAlert` 送 NORMAL、`_fireSos` 送 FALL telemetry(取代已刪的 /api/sos);預設 URL 改 /api/infer;刪 `_gpsFix`/`_workerPositionUri`/`_sosUri`。
- `phone_app`:`TbClient` 加 `_post`/`findOwnGuardianDeviceId`/`uploadGuardianGps`/`sendWearerCommand`;`_readGps` 定時上傳守護者自己 GPS 到 Guardian device;配戴者卡片(有告警時)加「我看到了·前往中」按鈕寫 shared attr。
- demo:`/api/demo/inject` 改成 server 推論後用 wearer token 把結果送 TB(demo-only,讓 TB 規則鏈跑),不再自己算 escalation。

**統一端到端驗證(2026-06-01 全綠):** 6 情境 server 只回 situation、TB 自算 escalation+告警:normal/sit/lie→NONE 無告警、fall→ASK_OK→FallSuspected、heat→SOS_HEAT→HeatStrain、collapse→SOS_COLLAPSE→MedicalCollapse;recovery→自動 CLEARED;B2 延遲升級兩 case OK;B4 全迴圈 rescue_state=guardian_enroute;守護者 GPS 進 TB。`/health`=random-forest-v3、tb_workers_loaded=3。

**TB 關鍵 API/節點筆記(實測):**
- ClearAlarm 節點:`{alarmType, scriptLang:"TBEL", alarmDetailsBuildTbel:"return metadata;"}`。
- Delay 節點:`org.thingsboard.rule.engine.delay.TbMsgDelayNode {periodInSeconds, maxPendingMsgs}`,delay 後走 `Success`。
- 載屬性進 metadata:`org.thingsboard.rule.engine.metadata.TbGetAttributesNode {latestTsKeyNames:["last_situation"], fetchTo:"METADATA"}` → metadata.last_situation。
- alarm 物件原生有 startTs/endTs/clearTs/ackTs/cleared/acknowledged → 統計直接用,不必自造。
- debug:`POST /api/ruleChain/metadata` 時每 node 設 `debugSettings:{failuresEnabled:true,allEnabled:true}`;查 `GET /api/events/RULE_NODE/{nodeId}/DEBUG_RULE_NODE?tenantId={tid}&...`。重 provision 會清掉 debug 旗標。

## 決策（使用者確認）
- 角色命名：**配戴者 Wearer**（戴錶被守護，有 HR/IMU/情境/GPS）＋ **守護者 Guardian**（手機監看，無 BPM）。
- 手機切換人 = **真正 TB 帳號登入**（CUSTOMER_USER）。
- 監看範圍 = **全部 + 附近優先**（看所有配戴者，附近事件標紅）。

## TB API 流程（已用實機驗證可行）
- 建 customer：`POST /api/customer {title}`。
- 建 user：`POST /api/user?sendActivationMail=false {email,authority:CUSTOMER_USER,customerId,firstName,lastName}`。
- 設密碼：`GET /api/user/{id}/activationLink` → 取 `activateToken` → `POST /api/noauth/activate {activateToken,password}`（不要加 query）。
- 指派：`POST /api/customer/{cid}/device/{did}`、`POST /api/customer/{cid}/dashboard/{did}`。
- guardian：`POST /api/auth/login` → `GET /api/auth/user`（拿 customerId）→ `GET /api/customer/{cid}/devices` / telemetry / alarms。
- 告警讓 customer 看到 → 告警節點 `propagateToOwner=True`（已改）。

## 已完成
- [x] provision_thingsboard.py：
  - `ensure_wearer_devices`（`Wearer_<id>`, type `wearer`），回傳 token+device_id。
  - 新增 `ensure_customer`/`ensure_customer_user`/`assign_device_to_customer`/`assign_dashboard_to_customer`。
  - 三個告警節點 `propagateToOwner=True`。
  - dashboard 改單一 wearers 別名（deviceType `wearer`, resolveMultiple），刪除 Guardian 單裝置別名；widgets/標題改配戴者語境；`build_dashboard`/`provision_dashboard` 拿掉 device_id。

## 全部完成（2026-05-30，已端到端驗證）
- [x] provision `main()`：刪 Guardian_Device_001 + 舊 Worker_<id>、ensure_wearer_devices、建 customer `Guardian Ops` + guardian1/guardian2（密碼 `REDACTED`）、指派 wearer 裝置 + dashboard、印帳密。
- [x] **修掉 bug**：`/api/user/{id}/activationLink` 回傳純文字(URL)，`_request` 原本無條件 `json.loads` 會炸 → 改成 JSONDecodeError 時回傳原始字串。
- [x] MN `main.py`：推論 payload push 到配戴者本人裝置（含 situation/escalation/last_*/entity_type=wearer），移除單一 device push 與 push_telemetry import；`.env` TB_DEVICE_TOKEN 清空；容器重建 health=random-forest-v3。
- [x] phone_app 全面重寫為守護者控制中心：TB 登入/refreshToken/401 自動 refresh、登出切帳、`/api/customer/{cid}/devices`+telemetry+`/api/alarms?searchStatus=ACTIVE`、清單/地圖/告警三 Tab、本機 GPS 算距離 ≤50m 附近優先標黃+強震。dart analyze 乾淨。
- [x] wear_os_app：預設 TB token 改 Wearer_W-001（`<redacted>`）。dart analyze 乾淨。
- [x] 端到端（真實驗證）：inject W-001 collapse → guardian1@guardian.local 登入(CUSTOMER_USER, refreshToken)看得到 3 配戴者、Wearer_W-001=FALL/SOS_COLLAPSE/hr165、active alarms 2(Wearer_W-001:MedicalCollapse / Wearer_W-002:HeatStrain)。README/計畫書 §8 已更新。

## 實際 token / 帳號（這次 provision 後，已核對 data/worker_tokens.json）
- wearer tokens：W-001 / W-002 / W-003 = `<redacted>`（見 data/worker_tokens.json，未納入版控）。
- 守護者：`guardian1@guardian.local` / `<password>`、`guardian2@guardian.local` / `<password>`（兩者皆已驗證可登入並看到 3 配戴者 + 2 告警）。
- 手機登入預設 TB URL：`http://140.113.123.43:18080`（依實機網段改）。

## 踩到的兩個雷（已修）
1. `_request` 對 `/api/user/{id}/activationLink`（回傳純文字 URL）無條件 `json.loads` → 崩潰。改成 JSONDecodeError 時回原始字串。
2. activationLink 只在「未啟用」時有效；第一次 run 崩在 guardian1 啟用前 → guardian1 存在但未啟用、無法登入。`ensure_customer_user` 原本「存在就跳過」→ 永遠補不回。改成 `_try_activate`：對既有但未啟用的 user 也會啟用（已啟用則 activationLink 報錯，視為已啟用跳過）。
3. 重新 provision 若刪除並重建 wearer 裝置會換 token；目前 wearer 裝置是 idempotent（found 不重建），token 穩定，但**重跑前先核對 worker_tokens.json 再更新手錶 `_defaultTbToken`**。

## 注意
- 重命名 Worker_→Wearer_ 會「新建」裝置 → token 改變 → 更新 worker_tokens.json（腳本自動寫）與手錶預設 token。
- MN 內部變數仍叫 worker_id（邏輯 id W-00X），只有 TB 命名/型別/UI 改 wearer，降低改動風險。
- guardian 預設帳號：guardian1@guardian.local / `REDACTED`、guardian2@guardian.local / `REDACTED`（demo 切換用）。

---

## 2026-06-02 模型改用真實跌倒資料集重訓（Kaggle elderly-fall-detection-iot）

把情境分類器從「100% 合成資料」改成「真實跌倒資料 + 合成熱應激」的混合訓練。使用者選擇保留 5 類（混合）。

### 資料集真相（先盤點再用，避免誤用）
- 來源 `data/dataset/fall_detection.csv`：500 序列 × 50 timestep，10 標籤；其實是 Montreal「Multiple Cameras Fall」影片集（`archive/.../chuteNN/camN.avi`）衍生的**模擬** IoT 感測資料，非真實手錶錄製。
- **無心率**：熱應激（HEAT_STRAIN）無法由此訓練 → 仍用 `synth.build_scenario("heat")` 合成（低動作 + 持續高 HR）。
- **環境感測器是標籤洩漏**：`room_occupancy` 非跌倒恆為 1.0、跌倒恆 0.0；`floor_vibration`/`pressure_mat` 同樣。且手錶根本沒有這些感測器 → **全部丟棄**。
- **accel 是去重力的線性加速度**：靜止 ~0.3、跌倒衝擊 ~11。手錶與我們的特徵管線預期「含重力 g」(靜止~1g)。
- **sit ≈ stand**：在手錶可得的所有通道（accel/gyro/pitch 22.5/roll 0）統計上完全相同 → 二分類探針 CV=0.46（等同亂猜）。SIT_DOWN 與 NORMAL(stand) 必然糊在一起。

### 標籤對應（10 → 5 類）
fall_forward/backward/side_left/side_right/slump → **FALL**；lie_down → **LIE_DOWN**；sit → **SIT_DOWN**；stand/walk/bend → **NORMAL**；（無 → HEAT_STRAIN 由合成補）。
分佈：FALL 235、NORMAL 162、SIT_DOWN 56、LIE_DOWN 47、HEAT_STRAIN 80（合成）。

### 關鍵工程處理
- **重建含重力訊號**（`app/fall_dataset.py:gravity_vector`）：由 pitch/roll 算單位重力向量加回線性 accel → 還原成「含重力 g」，與手錶/既有 27 維特徵管線一致，**手錶 app 與 schema 不需改、特徵維度不變**。
- **HR 不可洩漏標籤**：四個 IMU 類別共用同一組靜息 HR 分佈（66–84），HR 對 FALL/LIE/SIT/NORMAL **不帶任何類別訊息**；只有 HEAT_STRAIN（合成）有持續高 HR。一開始我誤把 HR 依類別給（NORMAL=78/SIT=72…），讓 HR 變成後門標籤編碼器 → 模型靠 HR 而非動作分類、collapse 被誤判 HEAT。修正後 IMU 才真正做事。
- 單一真相來源：`app/fall_dataset.py` 同時給訓練腳本與 demo 注入器使用，兩邊絕不漂移。

### 誠實的評估數字（5-fold CV）
- 整體 **89.5%**。FALL 235/235、HEAT_STRAIN 80/80、LIE_DOWN 47/47 全對；**SIT_DOWN 15/56（其餘落入 NORMAL）** = sit≈stand 的資料天花板，非程式 bug。
- 合成 demo 形狀（舊 synth）對新模型會「失準」是**分佈落差**（synth 靜止噪聲近 0、資料集靜止噪聲大），不是退步 → 因此 **demo 注入器改成回放真實資料集視窗**（heat 仍合成），讓 demo 餵給模型「分佈內」資料。

### 套用（部署）
- posture-api 是 **docker image**（`COPY app ./app`），`data/` 是 volume。所以：
  - **模型**：寫到 `data/models/posture_classifier.joblib`（host volume）→ 容器即時生效；亦可 `POST /api/training/rebuild` 熱重建（已驗證 accuracy 0.910）。
  - **程式**（demo 回放、fall_dataset.py）：需 `docker compose up -d --build posture-api` 重建映像（已執行）。
- 舊合成模型備份：`data/models/posture_classifier.synthetic-backup.joblib`（可回滾）。舊合成訓練檔移到 `data/posture_training_synthetic_backup/`。
- 重訓：`./.venv/Scripts/python.exe scripts/train_fall_model.py`；評估：`scripts/eval_fall_model.py`；活體驗證：`scripts/verify_live.py`。

### 端到端（真實容器驗證 2026-06-02）
`/api/demo/inject`（已回放真實視窗）：normal→NORMAL、sit→SIT_DOWN、lie→LIE_DOWN、fall→FALL(1.00, acc_mag_max≈10)、collapse→FALL(hr159→TB 升 SOS_COLLAPSE)、heat→HEAT_STRAIN(1.00, hr124)；全部 pushed_to_tb=True；最後 reset W-001→NORMAL 清乾淨。/health=random-forest-v3。

### 已知限制（給論文誠實交代）
- 資料集是影片衍生的**模擬**，靜息噪聲比真手錶大 → 對真實手錶的領域轉移仍有落差；可信賴的可轉移訊號是「跌倒衝擊量級」與（熱應激的）HR 通道。
- sit vs stand 在手錶感測器上不可分 → SIT_DOWN 召回率天生偏低（已如實回報，未用洩漏特徵硬拉高）。
- 若日後拿到「含 HR 的真實穿戴資料」可把 HEAT_STRAIN 也換成真實，並重估領域落差。

---

## 2026-06-02 README 補強（圖表 + 訓練成果）＋ 倉庫清理

- **README 重寫為 v3.2**：mermaid 架構圖、摘要表、§2 完整「情境分類模型」段（資料集→訊號特性→27 維特徵→訓練成果→特徵重要性→誠實限制），嵌入 5 張由真實結果產生的圖。
- **新增 `scripts/make_figures.py`** → `docs/img/`：`confusion_matrix.png`（5-fold CV）、`per_class_metrics.png`、`dataset_distribution.png`、`feature_importance.png`、`signal_examples.png`（各情境 |acc| 量級，FALL 衝擊尖峰 vs 其餘貼 1g）。
- **清理（此倉非 git，故謹慎）**：
  - 硬刪（可重下載/build 產物）：`data/dataset/archive (14)` 的 192 個未用 .avi（~3.6GB，保留 `technicalReport.pdf`）、WISDM 整套 + pdf、`scripts/__pycache__`。`data/dataset` 由 3.6G→8.5M。
  - 移到 `_archive/`（可復原，自著內容）：v2.0 舊文件 6 份（architecture/demo-guide/install-wearos-app/thingsboard-dashboard/index.html/plan-guardian-v3）→ `_archive/docs-v2/`；孤兒腳本 `bootstrap_training.py`/`replay_pick_events.py` + `tb_backups/` → `_archive/scripts-orphaned/`。
  - 結果：`docs/` 只剩 `img/` + 本進度日誌；`scripts/` 只剩 6 個活躍腳本；README 連結/圖片/腳本參照全數驗證存在、無斷鏈。

---

## 2026-06-02 護理戰情升級：呼叫派發通知 + 數位分身 Avatar + 一鍵呼叫手錶

使用者要「更厲害的護理功能、dashboard 上的 calling、avatar，但不要太花俏」。三項都做在 TB 端，已實機驗證。

### A. 護理呼叫派發（TB Notification Center，純 TB、零外部設定）
- TB CE 4.3.1 內建 Notification Center，delivery methods = WEB / EMAIL / MICROSOFT_TEAMS。
- 在 `provision_thingsboard.py` 新增 `ensure_notification_target/template/rule` + `provision_notifications`：
  - **target** `On-call Guardians 護理值班`：`usersFilter.type = ORIGINATOR_ENTITY_OWNER_USERS` → 因為 wearer 裝置屬 Guardian Ops customer，告警 originator 的 owner users 正好是守護者，免綁 customerId。
  - **template**（notificationType=ALARM）WEB：subject `🚑 ${alarmType} · ${alarmSeverity}`、body 用 `${alarmOriginatorName}`。
  - **rule**（triggerType=ALARM）：alarmTypes=[MedicalCollapse,HeatStrain,FallSuspected]、severities=null（全部）、notifyOn=[CREATED]、recipientsConfig.escalationTable={"0":[target]}。
- **驗證**：inject collapse W-001 → guardian1@guardian.local 收到通知「🚑 MedicalCollapse · critical / 配戴者 Wearer_W-001 觸發 MedicalCollapse（critical）— 請立即查看並前往。」✅

### B. 數位分身 Avatar 卡片（每位配戴者一張，活的狀態色）
- widget = `cards.markdown_card`，`settings.useMarkdownTextFunction=true` + `markdownTextFunction`（JS 回傳 HTML），資料合約：`data[0]['<dataKey label>']` 與 `data[0]['entityName']`。
- 每位 wearer 一個 **singleEntity** alias（`filter.type=singleEntity`, `singleEntity={entityType:DEVICE,id}`）；卡片顯示頭像圓圈（姓名縮寫）+ 即時 bpm + 狀態徽章，顏色依 `last_escalation/last_situation`（綠正常/青坐躺/橘疑跌·熱/紅倒地）。
- `build_dashboard(dashboard_name, wearers)` 新增 `wearers` 參數（{wearer_id,device_id,name}）；版面改 row 游標，最上排是 avatar(h5)+呼叫鈕(h2) 的 care console。

### C. 儀表板一鍵呼叫手錶（dashboard → watch，走既有 shared-attr 通道）
- call 卡也是 `cards.markdown_card`，但帶 `config.actions.elementClick`=[{type:custom, customFunction}]，JS 用 `widgetContext.attributeService.saveEntityAttributes(entityId,'SHARED_SCOPE',[{guardian_cmd:'call'},{guardian_cmd_ts:Date.now()}])` + `showSuccessToast`。
- **手錶** `wear_os_app`：`WearStage.called` + `_enterCalled()`，`_pollGuardianCommand` 多讀 `guardian_cmd_ts`，`guardian_cmd=='call'` 且 ts 變動 → 響鈴震動 `[0,500,200,500,200,500,200,800]` + 全螢幕「守護者正在呼叫你」+ 回寫 `call_seen`。dart analyze 乾淨。
- **驗證**：寫 guardian_cmd=call 到 Wearer_W-001 SHARED_SCOPE（HTTP 200）→ 用 W-001 device token `GET /api/v1/{token}/attributes?sharedKeys=...` 讀回 `guardian_cmd=call` → would RING=True；清除後 200。✅（手錶端實機響鈴需接上手錶；APK 已重建。）

### 驗證彙整
- 儀表板 `Guardian Live Ops` 現有 6 張 markdown_card（3 avatar + 3 call）、3 個 singleEntity alias（Wearer_W-001/002/003），call action 確認寫 guardian_cmd=call。
- provision 全程 idempotent（target/template/rule/dashboard 都是 find-or-create / update）。
- 一個 gotcha：notification rule 的 `recipientsConfig` 需要 `triggerType:"ALARM"` 與 `escalationTable`；template 的 deliveryMethodsTemplates key 用大寫 `WEB`。

---

## 2026-06-02 簡化為單一配戴者

使用者要求假設只有一個 wearer、移除其餘。`provision_thingsboard.py`：`--wearers` 預設改 `W-001`；`ensure_wearer_devices` 不再合併舊 token（檔案＝恰好請求的 wearers，自動裁掉舊的）；main 新增「刪除不在請求集內的 Wearer_*」。執行後 TB 只剩 `Wearer_W-001`、worker_tokens.json 只剩 W-001、儀表板 1 avatar+1 呼叫卡、guardian→wearer 關係 2 條、posture-api 重啟後 tb_workers_loaded=1。要恢復多人：`--wearers W-001,W-002,...`。README §1.1/§5 同步。

---

## 2026-06-02 Dashboard 重新設計（密度更高、更好看）

使用者覺得儀表板太空、不好看。重排成全寬、單一配戴者導向的密集照護戰情：
- **第一排 care console**：數位分身 avatar 卡（w9，頭像+大字 HR+狀態徽章+ΔHR）｜生理指標 KPI 2×2 格（w8：最高HR/ΔHR/信心%/撞擊後靜止，色彩依值）｜心率趨勢 time-series（w7）。
- **第二排**：一鍵呼叫鈕（w9）｜救援與狀態 strip（w15：救援狀態/裝置/守護者確認/最近恢復）。
- **第三排**：配戴者地圖（w15）｜進行中告警（w9）。
- **第四排**：最新判讀｜恢復統計｜已清除告警（各 w8，3 欄並排）。
- **第五排**：守護者地圖（w15）｜值班守護者面板（w9，html）。
- 新增 markdown 面板 builder：`vitals_panel` / `status_strip`（用 `_md_panel`，JS value function + 色彩）；avatar JS 加 bpm→hr_max fallback、ΔHR 副標、icon。
- **demo 也推 `bpm`**（=hr_mean）→ 沒手錶時儀表板也有即時 HR；posture-api 已重建。
- 結果：13 widgets、頁高 34 列（原本很稀疏）。widget 型別：html_card×2 / markdown_card×4 / basic_timeseries×1 / maps×2 / alarms_table×2 / entities_table×2。卡片讀的 telemetry（bpm/hr_max/ΔHR/confidence/situation/escalation）實測皆有值。
- 渲染需使用者目視確認（我無法截圖 TB）；markdown 面板沿用已會渲染的 avatar 機制。

---

## 2026-06-02 Dashboard 改走 TB Assisted-Living（healthcare）風格

使用者提供 TB「assisted living / healthcare」prototype 參考圖（淺色、卡片式、resident profile + alarm tables）。把儀表板從密集深色改成精簡淺色：
- **淺色主題**（grid bg #eef2f6），元素從 13 → **7 個 widget**。
- **Resident profile 卡**（新 `profile_card`，markdown_card）= 中心：頭像圓圈(縮寫+在線點)+姓名+狀態 pill+**藍色「呼叫配戴者」鈕**+3 格 metric(心率/信心/風險)+細節格(最高HR/ΔHR/救援狀態/守護者)。**呼叫鈕用 targeted 自訂動作**：`$event.target.closest('.call-btn')` 才寫 guardian_cmd=call，點卡片其他處不會誤觸。
- 版面：header(h2) ｜ profile(w9)+Resident alarms(w15) ｜ 位置地圖(w15)+心率趨勢(w9) ｜ 已恢復事件(w15)+值班守護者卡(w9)。
- 拿掉冗餘的 entities_table（最新判讀/恢復統計）與守護者地圖（守護者改顯示在 on-call 卡）；保留 alarms_table×2、map、time-series。
- 驗證：7 widgets、profile 有 Call action、淺色 bg。渲染需目視；avatar 目前用乾淨的縮寫圓圈（參考圖是真人照片，可再加 `avatar_url` 屬性換真實照片，但需 TB 允許外部圖）。

---

## 2026-06-02（晚）把核可的 mockup 正式移植進 TB（Row A–D）

先在 `docs/dashboard-mockup.html` 反覆收斂出核可版（我無法看到 TB 實際像素，故先用瀏覽器可見的 mockup 迭代），使用者核可後移植：
- **規則鏈 `Enrich derived telemetry` JS** 新增三項衍生欄位：命名區域 `zone`（內建 6 個校園 landmark + haversine 取最近，半徑 130m，無外部 API）、`situation_code`(0正常/1坐/2躺/3熱/4跌倒)、`hr_baseline = hr_mean − hr_above_baseline`。三個 CreateAlarm node 的 `alarmDetailsBuildJs` 都加 `details.zone`。
- **profile_card 重寫**：拿掉「呼叫配戴者」鈕（不應打電話叫王伯伯做事；指派改放守護者列）。改成住民識別卡：頭像+姓名+狀態 pill+3 指標(HR/情境/風險)+細節(目前位置=zone/年齡性別/慢性病/手錶電量/最後同步)+緊急聯絡。住民靜態資料與電量讀 device **SERVER_SCOPE 屬性**。
- **新 widget**：`stats_strip`（今日統計 4 格 KPI，讀 stat_* 屬性，聚合摘要而非即時 HR，避免重複）；`guardians_card`（守護者名冊 markdown，多實體 data 陣列逐列渲染，距配戴者距離=guardian GPS vs 校園中心 haversine，每列「指派前往」鈕——自訂動作寫 `guardian_cmd=dispatch` 到**固定 wearer device id**，`data-g` 帶哪一位）。
- **版面 Row A–D**：header ｜ A: 校園地圖(w15,雙角色,tooltip 含 zone)+住民卡(w9) ｜ B: 今日統計(w24) ｜ C: 心率 vs 個人基線(w12, bpm+hr_baseline)+情境時間軸(w12, situation_code) ｜ D: 告警歷史(w15)+守護者卡(w9)。拿掉舊的 entities_table/已恢復事件/on-call html 卡（去重）。
- **main() 種子**：`seed_wearer_profile` 寫住民屬性+今日統計+一筆 NORMAL 校園遙測（zone/hr_baseline/situation_code 齊全），卡片一載入即有資料。
- **端到端實測（localhost:18080）**：佈建成功（規則鏈 20 node/21 連線、儀表板更新）。inject collapse → `situation_code=4`、`escalation=SOS_COLLAPSE`、`hr_baseline` 算出、`zone` 落地、MedicalCollapse 告警 details 帶 zone。inject normal → 轉綠（code=0/risk=0）、ClearAlarm 生效（searchStatus=ACTIVE → 0、CLEARED → 10）。狀態收尾為 NORMAL 供首次檢視。
- **TB 形狀記憶**已更新 [[tb-care-console-shapes]]：自訂動作可寫「別的」實體、規則鏈算 zone/code/baseline、device alarm 端點用 `searchStatus`（非 `statusList`）。
- 待目視：markdown 卡（profile/stats/guardians）與兩張 timeseries 的實際渲染；情境時間軸目前是數值階梯線（0–4），非 mockup 的彩色色塊（markdown value function 拿不到歷史，只能用原生 timeseries 畫歷史）。

---

## 2026-06-02（深夜）兩頁切換 + 精簡統一 + 功能性指派（多輪實機目視修正）

使用者用實際截圖回饋，逐步收斂（這次能看到 TB 真實渲染）：
- **留白問題**：TB grid 是固定格高（~70px），markdown 卡內容填不滿就留白。把每個 widget 的 `sizeY` 縮到貼合內容（map/profile 8、KPI 2、charts 6）。種了 12h 歷史遙測（`_BPM_TRACE`/`_SIT_TRACE`，含跌倒尖峰）讓兩張圖有完整曲線；圖時間窗改 12h 鋪滿。
- **移除頂部 header bar**，改成**兩頁切換**：`configuration.states` = `default`(總覽) + `history`(趨勢與告警)，每頁 ~一個螢幕高、免長捲。
- **⚠️ 關鍵踩雷（實機查證）**：`cards.markdown_card`/`html_card` 的 `descriptor.actionSources` 是**空 `{}`** → 掛在上面的 `elementClick`（自訂 stateController、內建 updateDashboardState 都試過）**全被忽略，點了沒反應**。先前的呼叫/指派/分頁鈕其實都不會動。
- **解法 = 原生 `two_segment_button`**（fqn `system.two_segment_button`，非 `system.buttons.…`）：點擊由共用 `actionWidget` 基底處理。
  - **分頁**：left/right `updateDashboardState` → default/history；每頁一顆、`initialState.defaultValue` 預選當前頁；`autoScale:false` 否則字被撐到 40px。使用者**確認分頁可切換**。
  - **功能性指派**：同顆 widget 的 left/right click 用 `type:"custom"` + `customFunction`，寫 `guardian_cmd=dispatch`/`guardian_cmd_by` 到 wearer SHARED_SCOPE（單一 wearer→device id 嵌進 JS）。**使用者實點 → toast 出現、REST 查得 `guardian_cmd=dispatch/by=guardian1`**，端到端成立（真手錶輪詢 `sharedKeys=guardian_cmd` 即切「守護者前往中」）。新增「派遣狀態」display 卡讀回 `guardian_cmd_by`/ts/`rescue_state`。
- **守護者卡精簡**：改用 TB 原生標題列、兩位垂直置中、移除不會動的假按鈕；底部「告警→自動通知」是真的（Notification Center）。
- TB 形狀全部記進 [[tb-care-console-shapes]]：markdown 無 actionSources、兩頁 states、`two_segment_button` 切 state 與寫 shared attr、`searchStatus` 查告警、device alarm 端點等。

---

## 2026-06-03 情境時間軸改 state chart + 住民卡去除假資料（真實/計算/標示）

使用者指出兩處：情境時間軸用 0–4 數字折線「沒人看得懂」、住民卡數值「不應該隨便亂填、有些要更新」。
- **情境時間軸**：改用原生 `charts.state_chart`（type timeseries），`yaxis.ticksFormatter` 把 0–4 映射成 **正常/坐下/躺下/熱應激/跌倒**，`steppedLine`+`fillLines` 著色帶。讀起來是「y 軸=情境名、階梯帶顯示一天狀態與轉換」。
- **住民卡去假資料**（使用者拍板：住民基本資料保留為**明確標示的示範人物**、今日統計**改由真實告警計算**）：
  - **手錶電量**：從寫死屬性 72 → 真實 `battery` **遙測**（seed 一段緩降，手錶會覆寫；無資料顯示「—」）。
  - **最後同步**：從假的「剛剛」→ 規則鏈每筆遙測蓋 `last_seen` 時戳（`lastActivityTime` 持久化太慢不適用），卡片算「X 分鐘前」，**真的跟著資料更新**（實測 2s 前）。
  - **今日統計 KPI**：`compute_today_stats()` 由 `/api/alarm`（近 24h）算 跌倒數/平均恢復(clearTs−startTs)/自行恢復率(已清除÷跌倒)，活動時間由 situation 非臥床推算。實測 = 4 跌倒 / 10s / 100% / 11.0h，與真實告警對得上。
  - **年齡/慢性病/緊急聯絡**：保留王伯伯示範值，但卡片加「※ 示範資料 Demo profile」明確標示，不偽裝成真實個案。

---

## 2026-06-04 移除熱應激（HEAT_STRAIN），系統聚焦單一「跌倒守護」主軸

> 註：上方 2026-06-01 ~ 06-03 的 `HEAT_STRAIN / SOS_HEAT / HeatStrain` 內容是當時的歷史記錄，保留不改；本條說明其全面移除。

**動機**：實機手錶拿不到可靠的熱應激（HR 持續高於基線）訊號，且 `HEAT_STRAIN` 一直是**唯一純合成**的類別（公開跌倒資料集無心率）。為了誠實與聚焦，把熱應激從程式碼、模型、效能圖、文件全面移除；情境分類器由 **5 類縮成 4 類**（`NORMAL / SIT_DOWN / LIE_DOWN / FALL`）。

**保留 HR**：心率特徵（`hr_above_baseline / hr_max`）與個人 HR 基線（EWMA）**留著**——它們是「跌倒後生命徵象驟變 → `SOS_COLLAPSE`」升級與儀表板 HR 卡片的依據，與熱應激無關。升級體系只剩 `NONE / ASK_OK / SOS_COLLAPSE`，告警只剩 `MedicalCollapse / FallSuspected`。

**改動範圍：**

- **後端**：`schemas.py` 的 `SITUATIONS`/`Situation`/`LabelStats` 去 `HEAT_STRAIN`；`synth.py` 移除 `heat` 情境；`classifier.py` 啟發式後援去 heat 分支；`main.py` demo 注入器一律回放真實視窗（不再有 heat 合成特例）；`fall_dataset.py`/`training_store.py` 註解與統計改 4 類。
- **腳本**：`train_fall_model.py` 去 `build_heat_records`/`N_HEAT_SYNTH`（不再寫 `posture_heat_synth.json`）；`eval_fall_model.py`/`verify_live.py` 去 heat case；`make_figures.py` 改 4 類出圖。
- **TB 規則鏈（`provision_thingsboard.py`）**：移除 `Filter: SOS_HEAT`/`Alarm: HeatStrain`/`Clear: HeatStrain` 三節點與 escalation JS 的 heat 分支；連線改用**節點名稱**解析（不再硬編索引，日後增刪節點免重編號）；`situation_code` 把 `FALL` 由 4→3（去掉 heat 的 3）、state chart `ticksFormatter` 與 y 軸 `max` 同步改；Notification rule `alarmTypes` 去 `HeatStrain`。
- **App**：`wear_os_app` 去 `SOS_HEAT` 升級/震動/HEAT 環、移除已無用的 `_escalation` 欄位；`phone_app` 去 `HeatStrain/熱負荷` 顯示字串；`wear_os_trainer_app` 去 HEAT_STRAIN 採集標籤。
- **資料/模型**：刪 `posture_heat_synth.json`、舊全合成備份 `posture_classifier.synthetic-backup.joblib`、`posture_bootstrap_heat_*.json`。
- **文件**：README、architecture.md、ai-platform-azure-report(md+html)、dashboard-mockup.html 全部改 4 類（保留「熱重建 hot-reload」字樣，與熱應激無關）。

**重訓結果**：4 類 5-fold CV **87.0%**（先前 5 類 89.5%）；`FALL` 235/235 完美、`LIE_DOWN` recall 0.98；`SIT_DOWN` recall ~0.21（sit≈stand 資料天花板，非 bug）。docs/img 五張效能圖已重出為 4 類。
