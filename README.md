# 充桩条

开会话 → 计量脉冲累加 → 结算关账 → 超时/异常命运。

- 前端 http://127.0.0.1:6500
- API http://127.0.0.1:11500

## 开充正确性口径（全局唯一，禁止混用）

**架构：可告警仍开充 + 四者同命运原子重放。** 非空闲桩（占用/故障/维护）不开硬闸，仍允许落会话并推进占用，但成功灯只在四者同事务落定后点亮：

1. **空闲谓词**结果可重放（正常开充必须真正破空闲；异常开充保留真实开前态快照）；
2. **桩/枪占用钉**（`UPDATE ... WHERE status='空闲'/'可用'` 的条件更新）；
3. **充电中会话行**；
4. **开充审计「通过」行**。

任一侧在事务内读回校验不成立 → 抛 `OpenTorn` 整笔回滚并返回 409，绝不返回「回执绿而桩仍空闲」。其余约束：

- 回执带 `是否异常开充/异常原因/桩位开前态/枪线开前态`，以及 `成功/四者同命运/证据`（桩态、枪态、会话号、审计行号）；会话册与审计流水带 `abnormal_open` 与开前快照，事实只追加、不可静默抹掉；
- 总览设「异常占用在充数/异常占用分桶」暴露异常占用；另设「已开充(证据完整)/已开充证据撕裂」「开充缺口」册，缺口未清前读路径不亮已开充、不伪装已修完；
- 写侧用 `BEGIN IMMEDIATE` + 条件 UPDATE 重放同一空闲谓词，并发双开同一桩只破一次空闲（一笔正常 + N 笔异常）；
- 硬拒绝（充日封印、桩不存在、站点停用）不留会话、不改占用，仅在审计流水补登「拒绝」行，任何读路径不计入充电中；
- 空闲灯/可开充/证据撕裂标记由 `app/services/predicate.py` 同一谓词重放（SQL 与对象函数等价），前端不另写 `status==='空闲'` 判断，也不存在旁路改空闲灯的路径；
- 结算/中断/超时在同一立即事务内回收占用，只能把「占用→空闲」「占用→可用」，绝不借关账静默改空闲；脉冲入口在证据撕裂时返回 409，不得误亮。

关键接口：`GET /api/开充审计`（开充尝试流水，含通过/拒绝，通过行带 `replayable` 可重放标记）；`GET /api/桩位` / `GET /api/枪线` 带 `idle_lamp/open_sessions/evidence_conflict`；`GET /api/总览` 带 `已开充(证据完整)/已开充证据撕裂/开充缺口/缺口数`。

## 测试

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -I tests/test_open_session.py   # 服务层 74 项：含并发抢桩、半截失败回滚、对账
.venv/bin/python -I tests/test_http_e2e.py       # HTTP 20 项：真实 uvicorn 并发、旧库在线迁移
.venv/bin/python -I tests/test_open_tear.py      # 撕裂夹具 28 项：单号绿/占用缺会话/审计绿缺会话/连点半截/开充急停交错/脉冲入口
```

```powershell
# 本地 API
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 11500

# 本地前端（另开）
cd frontend
npm install
npm run dev
```

或 `docker compose up -d --build`。
