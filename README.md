# 充桩条

开会话 → 计量脉冲累加 → 结算关账 → 超时/异常命运。

- 前端 http://127.0.0.1:6500
- API http://127.0.0.1:11500

## 开充正确性口径（全局唯一，禁止混用）

**架构 B：可告警仍开充。** 非空闲桩（占用/故障/维护）不开硬闸，仍允许落会话并推进占用，但：

- 回执带 `是否异常开充/异常原因/桩位开前态/枪线开前态`，会话册与开充审计流水带 `abnormal_open` 与开前快照，事实只追加、不可静默抹掉；
- 总览设「异常占用在充数/异常占用分桶」暴露异常占用，不假装桩空闲可办；
- 写侧用 `BEGIN IMMEDIATE` + 条件 UPDATE（`WHERE status='空闲'`）重放同一空闲谓词，并发双开同一桩只破一次空闲（一笔正常 + N 笔异常）；
- 开充与占用推进同一事务同刷（会话行/桩态/枪态/事件/审计/序号），失败整笔回滚到开充前快照；
- 硬拒绝（充日封印、桩不存在、站点停用）不留会话、不改占用，仅在审计流水补登「拒绝」行，任何读路径不计入充电中；
- 读路径的空闲判定由 `app/services/predicate.py` 同一谓词重放（SQL 与对象函数等价），前端不另写判定。

新增接口：`GET /api/开充审计`（开充尝试流水，含通过/拒绝）。

## 测试

```bash
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -I tests/test_open_session.py   # 服务层 74 项：含并发抢桩、半截失败回滚、对账
.venv/bin/python -I tests/test_http_e2e.py       # HTTP 20 项：真实 uvicorn 并发、旧库在线迁移
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
