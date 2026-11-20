# 水质平台 · 报文契约发布

固件升级前确认新报文能被分析程序读取、程序升级后仍能读旧归档的契约发布网页。
按设备型号登记**历代固件写入格式**（只追加）与**现用程序读取格式**（按程序号替换），
各至多 8 份。每次发布前用 **z3** 对「全部历代写入格式 × 全部现用程序」做**全量**兼容判定
（逻辑证明，非抽样试样例）：任一写入格式允许的全部报文必须被每个现用程序接受，
否则拒绝发布，并给出**合法反例报文**与**拒收程序**。

型号页另提供**入仓分流核查**：选一版已发布固件，粘贴最多 8 条分流规则（队列名 + 受限
JSON Schema），用 z3 枚举该固件全部合法报文的队列命中组合，找出**漏投**（没有队列收件）
与**多投**（多个队列同时收件），每个组合给一条合法报文及实际命中的完整队列名单。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 操作入口（网页）：http://localhost:8000
- 健康检查：http://localhost:8000/api/health
- 数据保存在命名卷 `pgdata`（PostgreSQL），服务重启后已发布格式保留。

## 网页操作

1. 首页创建设备型号（如 `WQ-5000`），进入型号管理页。
2. 在「发布新格式」粘贴受限 JSON Schema：
   - 左栏**追加固件写入格式**（版本号自动递增，只追加）；
   - 右栏**发布/替换程序读取格式**（同一程序号覆盖旧版）。
3. 发布结果即时展示：成功，或拒绝 + 每个不兼容组合的反例报文与拒收程序；
   页面下方「发布日志」保留最近 50 条成功/拒绝/无效记录（含拒绝依据）。
4. 页面内置三个示例，点按钮即可填入编辑器：
   - **温度扩范围**：固件 v2 扩到 -40~100℃，直接发布会被旧程序（-10~50℃）拒收，
     反例如 `{"temp": -40, ...}`；先升级程序再发固件则通过。
   - **质控码缺省**：固件缺省 `qc`（或写 `null`），把 `qc` 列为 `required` 的程序会被拒，
     反例为不含 `qc` 的报文；程序改为可选并允许 `null` 后通过。
   - **顶层 anyOf**：程序同时接受多种报文形态。

## 入仓分流核查（型号页）

水质报文进仓前会被分到自动入仓、人工复核等队列。在型号页「入仓分流核查」区：

1. 选择一版**已发布固件**，粘贴最多 8 条规则的 JSON 数组，每条
   `{"queue": "队列名", "schema": {受限JSON Schema}}`；
2. 报文命中某条规则当且仅当满足其 Schema；**同一报文匹配同名队列的多条规则时只算一个去向**；
3. 系统在该固件允许的**全部**合法报文上枚举命中组合（支持顶层 `anyOf`，
   由 present/kind 双变量**区分字段缺省与显式 null**）：
   - 命中 0 个队列 → **漏投**；命中 ≥2 个不同队列 → **多投**；
   - 每个异常/正常组合附一条合法报文样本，并列出它实际命中的完整队列名单（含命中规则下标）；
   - 只有证明**每条合法报文都恰好进入一个队列**才显示**通过**；
     z3 超时/未知、未能判全时显示**待核**，并附已枚举到的组合。

内置三个分流示例（点击一键填入）：恰好一个队列（通过，含同名队列两条规则）、
漏投（qc 缺省/null 无队列收件）、多投（人工复核与自动入仓重叠）。

## 受限 JSON Schema 语法（其余一律拒绝）

- 平面对象：`{"type":"object","properties":{...},"required":[...],"additionalProperties":false}`
  （`required` 可省略，默认空；`additionalProperties` 必须显式为 `false`）
- 顶层 `anyOf`：`{"anyOf":[<平面对象>, ...]}`，且只能单独出现在顶层
- 整数上下界：`{"type":"integer","minimum":m,"maximum":M}`（上下界均可省略）
- 字符串/整数/空值枚举：`{"enum":[...]}`，可配 `"type"` 限定；枚举值只允许字符串、整数、`null`
- 空值类型：`{"type":"null"}`

嵌套对象、数组、`pattern`、`oneOf`/`allOf`/`not`、浮点/布尔值、`string` 无枚举等语法均被拒绝。

## 校验原理

对每对（写入格式 W，读取格式 R），把报文编码为 z3 变量（每个字段：是否出现、
值类型 null/int/string、整数值、字符串枚举下标），求解 `W 合法 ∧ R 拒收`：

- **UNSAT** ⇒ 不存在反例，`L(W) ⊆ L(R)` 成立——这是对全部报文的证明，不是抽样；
- **SAT** ⇒ 从模型还原一个反例报文，并用独立的纯 Python 判定器复核
  （确实满足 W、确实被 R 拒收）后随拒绝结果返回。

分流核查复用同一套报文编码：在「固件合法」约束下，对 n 条规则（n≤8）的命中布尔向量
逐一枚举（每次 SAT 后封锁该向量，至多 2^n 次查询），把向量按**队列名去重**为去向组合。
所有可实现向量枚举完毕（或跑满 2^n 个）即证明已覆盖全部合法报文；
全部组合都恰好含一个队列才判**通过**，含 0 个（漏投）或 ≥2 个队列（多投）判**异常**，
求解超时/未知判**待核**。每条样本同样经纯 Python 判定器复核命中名单后才展示。

## 并发与事务

- 每次发布在事务内先对型号行加锁（PostgreSQL `SELECT ... FOR UPDATE`），
  再基于最新已提交状态做**全量交叉检查**后落库——并发升级被串行化，
  不会各自通过却留下不兼容组合；
- 检查失败整体回滚，**已发布格式不变**；拒绝/无效尝试另写发布日志留痕；
- 固件格式只追加（无修改/删除入口），程序格式按程序号替换；各至多 8 份。

## JSON API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/models` | 创建型号 `{"name": "WQ-5000"}` |
| GET | `/api/models/{name}` | 查看当前固件/程序格式 |
| GET | `/api/models/{name}/log` | 发布日志（含拒绝依据） |
| POST | `/api/models/{name}/firmware` | 追加固件格式 `{"schema": {...}}` |
| POST | `/api/models/{name}/programs` | 发布/替换程序格式 `{"program_id": "...", "schema": {...}}` |
| POST | `/api/models/{name}/routing` | 分流核查 `{"firmware_version": 1, "rules": [{"queue": "...", "schema": {...}}]}` |

分流核查响应：`200 {"ok": true, "status": "pass|anomaly|pending", "complete": bool,
"combos": [{queues, kind: none|single|multi, message, matched_rules}], ...}`；
固件不存在、规则超 8 条、JSON/Schema 不合法返回 `422 {"ok": false, "error": "..."}`。

响应：成功 `200 {"ok": true, ...}`；不兼容 `409 {"ok": false, "problems": [{firmware_version, program_id, counterexample}]}`；语法/上限错误 `422`。

## 本地开发与自测

```bash
pip install -r app/requirements.txt httpx
python3 tests/test_units.py                 # 语法子集 + z3 判定单元自测
python3 tests/test_routing.py               # 分流核查单元自测（漏投/多投/去重/null/anyOf）
cd app && DATABASE_URL=sqlite:////tmp/contracts.db uvicorn main:app --port 8000 &
python3 tests/selftest.py                   # 集成自测（示例/上限/并发/页面/分流核查）
```

`DATABASE_URL` 缺省为 SQLite（便于本地自测）；Compose 中注入 PostgreSQL 连接串。

## 目录结构

```
app/
  main.py         FastAPI 入口（HTML 页面 + JSON API，含分流核查路由）
  service.py      发布事务：加锁 → 全量检查 → 落库/回滚
  solver.py       z3 编码与全量兼容判定（含反例复核）
  routing.py      分流核查：命中向量全量枚举、漏投/多投/待核判定与样本复核
  schema_spec.py  受限 JSON Schema 子集解析与纯 Python 报文判定
  db.py           SQLAlchemy 模型（PostgreSQL JSONB / SQLite JSON）
  examples.py     兼容发布示例与分流核查示例
  templates/      页面模板
tests/
  test_units.py   契约发布单元自测
  test_routing.py 分流核查单元自测
  selftest.py     集成自测
docker-compose.yml
```
