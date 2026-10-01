# 食品过敏原配方合规 API（Food Allergen Recipe Compliance API）

基于 **Python + FastAPI + PostgreSQL** 的过敏原合规评估服务：

- 登记**不可变原料**及其过敏原标记（含量 + 交叉接触）
- 登记**法域阈值规则**（草稿 → 发布，发布后不可变；旧接口继续保留）
- 管理**可追溯规则集**：按法域建立规则集草稿，提交即归一化到 **ppm**，
  乐观版本控制（`base_version`）；发布生成不可变、带摘要的 `rule_set_version`
- 提交**带版本的嵌套配方**（配方可引用子配方，版本发布后不可变）
- **评估**：按用量递归展开子配方，完成质量/浓度单位换算与交叉接触累积，
  检测循环引用、未知原料与超阈值，输出**按稳定顺序排列、含触发路径**的
  标签义务（obligations）、警告（warnings）与违规（violations）
- 评估请求**绑定配方版本**，并可显式绑定 `rule_set_code/version`，
  在不可变规则快照上**重放**；通过幂等键支持**幂等重放**
- 输入不完整时返回**最小缺失集合**（minimal missing set）

---

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动两个服务：

| 服务 | 说明 | 端口 |
| ---- | ---- | ---- |
| `db`  | PostgreSQL 16（数据持久化到 `pgdata` volume） | 5432 |
| `api` | FastAPI + Uvicorn（等待 db 健康检查后启动，自动建表） | 8000 |

## 2. 访问

| 地址 | 说明 |
| ---- | ---- |
| <http://localhost:8000/docs> | Swagger UI 交互式文档 |
| <http://localhost:8000/redoc> | ReDoc 文档 |
| <http://localhost:8000/health> | 健康检查 |
| <http://localhost:8000/units> | 支持的质量/浓度单位 |

## 3. 快速示例

启动后可直接运行端到端示例脚本（覆盖：登记 → 单条旧规则 → 规则集草稿/冲突/
发布 → 配方 → 绑定快照评估/重放 → 版本演进与不可变重放 → 最小缺失集合）：

```bash
bash examples/quickstart.sh
```

或按下面的 curl 步骤手动执行。

### 3.1 登记不可变原料

```bash
curl -X POST http://localhost:8000/ingredients \
  -H 'Content-Type: application/json' \
  -d '{
        "code": "almond_flour",
        "name": "Almond Flour",
        "allergens": [{"allergen": "tree_nuts", "concentration": 100, "unit": "%"}],
        "cross_contact": ["peanut"]
      }'
```

- `allergens[].unit` 支持浓度单位：`ppm`、`ppb`、`mg/kg`、`g/kg`、`%`
- `cross_contact` 为“可能含有”的交叉接触过敏原清单
- 原料**一经登记不可修改**；重复登记同一 `code` 返回 `409 INGREDIENT_EXISTS`

### 3.2 登记并发布法域阈值规则

```bash
curl -X POST http://localhost:8000/rules \
  -H 'Content-Type: application/json' \
  -d '{
        "code": "eu-gluten-prohibit-20ppm",
        "jurisdiction": "EU",
        "allergen": "gluten",
        "threshold": 20,
        "unit": "ppm",
        "action": "prohibit",
        "applies_to": "contains"
      }'

curl -X POST http://localhost:8000/rules/eu-gluten-prohibit-20ppm/publish
```

- `action`：`declare`（标签义务）/ `warning`（警告）/ `prohibit`（超阈值违规）
- `applies_to`：`contains`（配方带入）/ `cross_contact`（交叉接触）
- 规则**发布后不可变**（`PUT/DELETE` 返回 `409 RULE_IMMUTABLE`）；评估只使用已发布规则
- 这是**旧的单条规则接口**，继续完全可用；新的可追溯规则集见 3.3

### 3.3 可追溯规则集（按法域草稿 → 提交归一化 → 不可变发布）

规则集是**按法域**组织的整包规则，草稿用乐观版本号 `base_version` 保护
（初始为 `0`，每次成功写入后加 1）。

**① 创建草稿**

```bash
curl -X POST http://localhost:8000/rule-sets \
  -H 'Content-Type: application/json' \
  -d '{"code":"eu-pack","jurisdiction":"EU","name":"EU allergen pack"}'
# -> {"code":"eu-pack","base_version":0,"published_version":null,"rules":[]}
```

**② 提交规则（整包替换草稿，阈值在服务端归一化到 ppm）**

```bash
curl -X PUT http://localhost:8000/rule-sets/eu-pack \
  -H 'Content-Type: application/json' \
  -d '{
        "base_version": 0,
        "rules": [
          {"code":"gluten-declare","allergen":"gluten","threshold":0,"unit":"ppm","action":"declare","applies_to":"contains"},
          {"code":"gluten-prohibit","allergen":"gluten","threshold":20,"unit":"mg/kg","action":"prohibit","applies_to":"contains"}
        ]
      }'
```

- 提交即把阈值**归一化为 ppm**（`20 mg/kg == 20 ppm == 0.002%`），同时保留原始
  `threshold`/`unit` 以便追溯；响应中 `base_version` 变为 `1`
- 提交为**整包替换**；同一次提交内 `code` 重复返回 `422 DUPLICATE_RULE_CODE`
- **并发控制**：`base_version` 与当前版本不符时返回 `409 VERSION_CONFLICT`
  并在 `current_version` 中给出当前值，**不写入任何数据**

**③ 发布：重叠检测 → 不可变版本 + 摘要**

同一法域（即同一规则集）、同一过敏原、同一 `applies_to` 且 **ppm 阈值相等
（含边界）**的规则视为**重叠**：

- `declare` 与 `warning` 可并存
- `prohibit` 与任一**其他** action 冲突（相同 action 的重复视为冗余，不冲突）

存在冲突时拒绝发布，返回**稳定排序**的冲突明细（按
`(allergen, applies_to, threshold_ppm, rule_code...)`）：

```bash
curl -X POST http://localhost:8000/rule-sets/eu-pack/publish \
  -H 'Content-Type: application/json' -d '{"base_version":1}'
# 409 RULE_SET_CONFLICT
# {"detail":{"code":"RULE_SET_CONFLICT","conflicts":[
#   {"allergen":"gluten","applies_to":"contains","threshold_ppm":20.0,
#    "rules":[{"code":"gluten-declare","action":"declare"},
#             {"code":"gluten-prohibit","action":"prohibit"}], ...}]}}
```

无冲突时生成**不可变**版本（版本号从 1 递增），带 **SHA-256 内容摘要**
（与输入单位无关，仅取决于 ppm 归一化结果）与**摘要统计**；发布同样带
`base_version`，成功后草稿版本继续加 1：

```json
{
  "rule_set_code": "eu-pack", "jurisdiction": "EU", "version": 1,
  "digest": "f04ec59a…4052",
  "summary": {"rule_count": 4,
              "actions": {"declare": 2, "prohibit": 1, "warning": 1},
              "applies_to": {"contains": 3, "cross_contact": 1},
              "allergens": ["gluten", "peanut", "tree_nuts"]},
  "rules": [ "…不可变 ppm 快照…" ]
}
```

查询：`GET /rule-sets`（支持 `jurisdiction` 过滤）、`GET /rule-sets/{code}`、
`GET /rule-sets/{code}/versions`（摘要列表）、
`GET /rule-sets/{code}/versions/{v}`（完整不可变快照）。发布后可继续在草稿上
演进并发布 v2、v3……历史版本永不改变（同内容再次发布，摘要相同）。

### 3.4 提交带版本的嵌套配方

```bash
# 子配方 filling v1：100 g 杏仁粉 + 100 g 糖
curl -X POST http://localhost:8000/recipes -H 'Content-Type: application/json' \
  -d '{"code":"filling","name":"Almond Filling"}'
curl -X POST http://localhost:8000/recipes/filling/versions -H 'Content-Type: application/json' \
  -d '{
        "version": 1,
        "items": [
          {"kind":"ingredient","code":"almond_flour","quantity":100,"unit":"g"},
          {"kind":"ingredient","code":"sugar","quantity":100,"unit":"g"}
        ]
      }'
curl -X POST http://localhost:8000/recipes/filling/versions/1/publish

# 蛋糕 cake v1：400 g 子配方 filling + 1 kg 小麦粉
curl -X POST http://localhost:8000/recipes -H 'Content-Type: application/json' \
  -d '{"code":"cake","name":"Almond Cake"}'
curl -X POST http://localhost:8000/recipes/cake/versions -H 'Content-Type: application/json' \
  -d '{
        "version": 1,
        "items": [
          {"kind":"recipe","code":"filling","quantity":400,"unit":"g"},
          {"kind":"ingredient","code":"wheat_flour","quantity":1,"unit":"kg"}
        ]
      }'
curl -X POST http://localhost:8000/recipes/cake/versions/1/publish
```

- 条目 `kind` 为 `ingredient`（叶子原料）或 `recipe`（子配方）
- 子配方可**钉版本**（`"version": 2`）或省略版本号（评估时解析为**最新已发布版本**，解析结果记录在 `resolved_recipes` 中）
- 质量单位支持：`mg`、`g`、`kg`、`oz`、`lb`
- 可选 `yield_quantity`/`yield_unit` 指定批次产出（如烘焙失水）；缺省为各原料用量之和
- 版本**发布后不可变**；同一版本号不可重复创建

### 3.5 评估（绑定配方版本 + 幂等 + 可选规则集快照）

```bash
curl -X POST http://localhost:8000/evaluations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cake-eu-v1-20260923' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU"}'
```

首次返回 `201 Created`；**相同幂等键 + 相同请求体重放**返回 `200 OK` 且
`"replayed": true`、`evaluation_id` 不变；**相同键 + 不同请求体**返回
`409 IDEMPOTENCY_CONFLICT`。幂等键也可放在请求体 `idempotency_key` 字段。

**显式绑定规则集版本（规则快照重放）**：请求体可带
`rule_set_code` + `rule_set_version`（两者必须同时出现），评估将**只**使用该
已发布版本的不可变 ppm 快照：

```bash
curl -X POST http://localhost:8000/evaluations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: cake-eu-pack-v1' \
  -d '{"recipe_code":"cake","recipe_version":1,"jurisdiction":"EU",
       "rule_set_code":"eu-pack","rule_set_version":1}'
```

绑定错误均为**稳定错误**：

| 情况 | 状态码 | `detail.code` |
| ---- | ---- | ---- |
| 只给 code / 只给 version | 400 | `RULE_SET_BINDING_INCOMPLETE` |
| 规则集不存在 | 404 | `RULE_SET_NOT_FOUND` |
| 版本不存在或未发布 | 404 | `RULE_SET_VERSION_NOT_FOUND` |
| 规则集法域与请求法域不一致 | 422 | `RULE_SET_JURISDICTION_MISMATCH` |

结果中带 `rule_set` 引用；之后即便发布了 v2，用同一幂等键或按
`GET /evaluations/{id}`、
`GET /evaluations?rule_set_code=eu-pack&rule_set_version=1` 查询，都**固定**
返回该摘要（digest）、配方版本与结果，即始终重放同一份规则快照；
未绑定规则集的旧请求返回 `"rule_set": null`（旧行为不变，使用已发布单条规则）。

响应节选（数值为上述示例的真实计算结果）：

```json
{
  "recipe": {"code": "cake", "version": 1},
  "jurisdiction": "EU",
  "rule_set": {"code": "eu-pack", "version": 1, "digest": "f04ec59a…4052"},
  "status": "completed",
  "total_mass_g": 1400.0,
  "resolved_recipes": [{"code": "cake", "version": 1}, {"code": "filling", "version": 1}],
  "allergens": [
    {"allergen": "gluten",    "concentration_ppm": 71428.571429,  "concentration_percent": 7.142857},
    {"allergen": "tree_nuts", "concentration_ppm": 142857.142857, "concentration_percent": 14.285714}
  ],
  "obligations": [
    {
      "allergen": "tree_nuts",
      "rule_code": "tree-nuts-declare",
      "action": "declare",
      "source": "contains",
      "threshold_ppm": 0.0,
      "concentration_ppm": 142857.142857,
      "trigger_paths": [
        {"path": ["cake@v1", "filling@v1", "almond_flour"],
         "ingredient": "almond_flour", "ingredient_mass_g": 200.0,
         "allergen_mass_g": 200.0, "contribution_ppm": 142857.142857}
      ]
    }
  ],
  "warnings": [
    {"allergen": "peanut", "rule_code": "peanut-may-contain", "action": "warning",
     "source": "cross_contact",
     "trigger_paths": [{"path": ["cake@v1", "filling@v1", "almond_flour"], "...": "..."}]}
  ],
  "violations": [
    {"allergen": "gluten", "rule_code": "gluten-prohibit", "action": "prohibit",
     "threshold_ppm": 20.0, "concentration_ppm": 71428.571429, "...": "..."}
  ],
  "unregulated_allergens": [],
  "evaluation_id": "…",
  "replayed": false
}
```

所有列表均**稳定排序**：义务/警告/违规按 `(allergen, rule_code)`，触发路径按
路径字符串排序——同一输入永远得到逐字节一致的结果。

### 3.6 输入不完整 → 最小缺失集合

```bash
curl -X POST http://localhost:8000/evaluations \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: broken-1' \
  -d '{"recipe_code":"broken","recipe_version":1,"jurisdiction":"EU"}'
```

```json
{
  "detail": {
    "code": "INCOMPLETE_INPUT",
    "message": "evaluation input is incomplete; supply the minimal missing set",
    "missing": [
      {"type": "ingredient", "code": "mystery", "reason": "unknown_ingredient"},
      {"type": "recipe", "code": "nonexistent", "version": 7, "reason": "unknown_recipe"}
    ]
  }
}
```

缺失集合**去重且按稳定顺序排列**，只包含直接缺失的引用（未知原料、未知配方、
未知/未发布版本），补齐后重发同一请求即可。

### 3.7 循环检测

配方图存在环（如 `a → b → a`）时返回 `422`：

```json
{"detail": {"code": "RECIPE_CYCLE", "cycle": ["a@v1", "b@v1", "a@v1"], "message": "..."}}
```

### 3.8 并发复验：草稿写与发布的原子胜负

配方版本的草稿替换（`PUT`）、删除（`DELETE`）与发布（`publish`）在**数据库层**
以版本状态原子地决定胜负：三个写操作都是带 `status = 'draft'` 守卫、且**按最初
读取的行主键（`id`）定位**的条件 UPDATE/DELETE，行锁 + 条件重估保证只有一个请求
先提交生效，**不存在**“读到草稿 → 对方已发布 → 旧写仍然落库”的窗口。因此：

| 竞速 | 先提交者 | 后到请求的结果 | 最终状态 |
| ---- | ---- | ---- | ---- |
| PUT vs publish | publish | `409 RECIPE_VERSION_IMMUTABLE` | 已发布行及其内容不变 |
| PUT vs publish | PUT | publish 200 | 发布冻结**更新后**的草稿 |
| DELETE vs publish | publish | `409 RECIPE_VERSION_IMMUTABLE` | 已发布行不被删除 |
| DELETE vs publish | DELETE | `404 RECIPE_VERSION_NOT_FOUND` | 发布不成功，也不留快照 |
| PUT/DELETE/publish vs **删除后同号重建** | 删除并新建草稿 | `404 RECIPE_VERSION_NOT_FOUND` | 原行已消失；**新草稿及其内容不受影响** |

**删除后同号重建的身份判定**：旧请求读取旧草稿后，另一个请求先把该行**删除**，
再用**相同配方号 + 版本号**创建一个全新草稿（新的行主键）。旧请求的条件写按行 id
匹配，命中 0 行；失败重查也**只追踪最初读取的那个行 id**，而不是按
`(配方, 版本号)` 重新查找——否则会把新行误判成旧行。于是：

- 原行**已删除**：旧 PUT / DELETE / publish 一律返回
  `404 RECIPE_VERSION_NOT_FOUND`，**新草稿保持 `draft`、内容原样**，可独立发布；
- 原行**确已发布**：旧 PUT / DELETE 返回 `409 RECIPE_VERSION_IMMUTABLE`，
  针对**同一原行**的重复 publish 保持幂等（200，回放冻结内容）。

下面是与实现逐字一致的真实请求 / 响应示例（交错由测试在端点的读窗口内注入）。

旧 PUT 在原行被删除并同号重建后得到 404，新草稿（7 g）原封不动：

```http
PUT /recipes/cake/versions/1
Content-Type: application/json

{"items":[{"kind":"ingredient","code":"sugar","quantity":99,"unit":"g"}]}
```

```json
404 Not Found
{"detail": {"code": "RECIPE_VERSION_NOT_FOUND",
            "message": "recipe 'cake' has no version 1"}}
```

```http
GET /recipes/cake/versions/1
```

```json
200 OK
{"version": 1, "status": "draft",
 "items": [{"kind": "ingredient", "code": "sugar", "version": null,
             "quantity": 7.0, "unit": "g"}],
 "yield_quantity": null, "yield_unit": null,
 "created_at": "2026-10-01T06:59:22.880689", "published_at": null}
```

旧 publish 在同样的删除—重建交错后**不再**谎称成功（修复前会返回 200 但响应体仍是
`draft`，等于一次什么都没冻结的“发布”）：

```http
POST /recipes/cake/versions/1/publish
```

```json
404 Not Found
{"detail": {"code": "RECIPE_VERSION_NOT_FOUND",
            "message": "recipe 'cake' has no version 1"}}
```

旧 DELETE 同理得到 `404` 且不会删掉新草稿：

```http
DELETE /recipes/cake/versions/1
```

```json
404 Not Found
{"detail": {"code": "RECIPE_VERSION_NOT_FOUND",
            "message": "recipe 'cake' has no version 1"}}
```

而当原行**确实被发布**时，旧 PUT 仍是 409（新草稿与此无关，不会被牵连）：

```json
409 Conflict
{"detail": {"code": "RECIPE_VERSION_IMMUTABLE",
            "message": "recipe 'cake' version 1 is published and immutable"}}
```

针对**同一原行**再次 `POST .../publish` 始终幂等：首次 200 冻结、后续重复发布都
返回同一个 `200 {"status": "published", ...}`。

一键复验（对运行中的服务并发发起真实竞速，逐轮断言上述不变量）：

```bash
docker compose up --build          # 启动后另开终端
bash examples/concurrency_check.sh # 默认 10 轮 × 3 组竞速，可用 ROUNDS=50 加量
```

脚本同时报告观察到的提交顺序（如 `A_publish_first` / `A_put_first`），
任一竞速组的不变量被破坏即以非零状态退出。**可控的删除—重建交错**无法用网络竞速
稳定复现，对应的确定性回归测试见
`tests/test_recipe_version_concurrency.py`（用 SQLAlchemy `load` 事件在
端点的读窗口内注入并发提交，精确复现「删除原行 → 同号新建草稿」等竞速交错）。

## 4. API 一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/ingredients` | 登记不可变原料（重复 code → 409） |
| GET  | `/ingredients`、`/ingredients/{code}` | 查询 |
| POST | `/rules` | 登记规则（草稿，旧接口） |
| PUT/DELETE | `/rules/{code}` | 仅草稿可改/删（已发布 → 409） |
| POST | `/rules/{code}/publish` | 发布规则（幂等，发布后不可变） |
| GET  | `/rules`、`/rules/{code}` | 查询（支持 `jurisdiction`、`status` 过滤） |
| POST | `/rule-sets` | 创建按法域的规则集草稿（`base_version=0`） |
| PUT  | `/rule-sets/{code}` | 整包提交草稿（ppm 归一化；带 `base_version`，不符 → 409 + `current_version`） |
| POST | `/rule-sets/{code}/publish` | 发布不可变版本（重叠冲突 → 409 稳定明细；带 `base_version`） |
| GET  | `/rule-sets`、`/rule-sets/{code}` | 查询草稿（支持 `jurisdiction` 过滤） |
| GET  | `/rule-sets/{code}/versions`、`/rule-sets/{code}/versions/{v}` | 版本摘要列表 / 不可变快照 |
| POST | `/recipes` | 创建配方 |
| POST | `/recipes/{code}/versions` | 新增草稿版本（版本号唯一） |
| PUT/DELETE | `/recipes/{code}/versions/{v}` | 仅草稿可改/删（按最初读取的行 id 与发布/删除竞速原子定胜负：原行已发布 → 409；原行被删除（含同号重建）→ 404） |
| POST | `/recipes/{code}/versions/{v}/publish` | 发布版本（幂等，发布后不可变） |
| GET  | `/recipes…` | 查询配方及版本 |
| POST | `/evaluations` | 评估（需 `Idempotency-Key` 头或 `idempotency_key`；可选 `rule_set_code`+`rule_set_version`） |
| GET  | `/evaluations`、`/evaluations/{id}` | 查询（可按 `rule_set_code`/`rule_set_version` 过滤，固定返回摘要、配方版本与结果） |
| GET  | `/units`、`/health`、`/` | 元信息 |

## 5. 评估语义

1. **递归展开**：从绑定的配方版本出发，按 `用量 / 子配方产出` 缩放每一片叶子
   原料的质量；所有质量统一换算为克。
2. **过敏原累积**：`过敏原质量 = Σ(叶子质量 × 含量)`；最终浓度
   `= 过敏原总质量 / 批次产出`，以 ppm 表示。
3. **交叉接触累积**：叶子原料的 `cross_contact` 标记沿路径向上累积为警告。
4. **规则匹配**：规则来自两个来源——未绑定时使用该法域**已发布单条规则**
   （旧接口 `/rules`）；绑定 `rule_set_code/version` 时使用该版本的**不可变
   ppm 快照**。`contains` 类在浓度 `≥ 阈值`（**边界即触发**，阈值 0 表示
   “存在即触发”）时触发，`cross_contact` 类在存在交叉接触路径时触发；
   按 `action` 分别归入 obligations / warnings / violations。
5. **未监管过敏原**：配方中存在但法域无任何已发布规则覆盖的过敏原，列入
   `unregulated_allergens`，不阻断评估。

## 6. 规则集语义（可追溯）

1. **按法域草稿**：每个规则集固定一个 `jurisdiction`；草稿是规则的**整包**视图，
   以 `base_version`（初始 0）做乐观并发控制。
2. **ppm 归一化提交**：提交时所有阈值换算为 ppm（保留 6 位小数），原始值/单位
   原样保留；归一化结果决定重叠判定、内容摘要（SHA-256）与评估阈值。
3. **重叠与冲突**：同法域、同过敏原、同 `applies_to` 且 ppm 阈值相等（含边界）
   即重叠；`declare`/`warning` 可并存，`prohibit` 与任何其他 action 冲突。
   冲突明细稳定排序（`allergen → applies_to → threshold_ppm → 规则码对`），
   存在冲突即**拒绝发布**。
4. **不可变发布**：发布生成单调递增的整数版本与单位无关的内容摘要（含规则数、
   action/applies_to 计数、过敏原清单）；发布后快照永不改变，可继续演进草稿
   发布下一版，同内容重发摘要不变。
5. **快照重放**：评估绑定规则集版本后，把 `code/version/digest` 与配方版本、
   结果一起持久化；按评估 ID 或规则集版本查询都重放同一规则快照，不受后续
   发布影响。未绑定的旧评估保持原有行为（`rule_set: null`）。

## 7. 本地开发与测试

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 运行测试（使用 SQLite 内存库，无需 PostgreSQL）
pytest

# 本地起服务（默认连 localhost:5432 的 PostgreSQL，可用 DATABASE_URL 覆盖）
uvicorn app.main:app --reload
```

## 8. 设计要点

- **不可变性**：原料登记即冻结；单条规则与配方版本经 `publish` 后冻结（改/删 → 409）；
  配方版本的草稿改/删与发布是**数据库层带状态守卫的条件写**（`status = 'draft'`），
  且失败判定**始终追踪最初读取的行主键**——竞速时按提交顺序原子定胜负：发布先提交则
  旧写 `409 RECIPE_VERSION_IMMUTABLE`，已发布行永不被改写或删除；若原行被删除后又以
  同配方号 + 版本号重建了新草稿，旧请求按原行 id 重查发现原行已消失，返回
  `404 RECIPE_VERSION_NOT_FOUND` 而**不会把新行误判成旧行**（修复前旧 publish 可能
  报 200 却仍返回 `draft`、旧 PUT/DELETE 可能误报 409）；针对同一原行的重复发布保持
  幂等。评估始终可依据稳定版本计算；
  规则集发布生成不可变版本快照与 SHA-256 摘要，保证任意历史评估可解释、可复现。
- **ppm 归一化与重叠检测**：规则集提交即归一化到 ppm（单位无关），同
  法域/过敏原/`applies_to`/等阈值（含边界）视为重叠；`declare`+`warning`
  并存，`prohibit` 与其他 action 冲突，冲突明细稳定排序并阻断发布。
- **乐观版本控制**：规则集每次提交/发布必须带 `base_version`（初始 0，成功
  后加 1）；版本不符返回 `409 VERSION_CONFLICT` + `current_version` 且不写入，
  并发安全且无需长事务锁。
- **版本绑定与规则快照**：评估必须指定 `recipe_version`；子配方可钉版本或解析为
  “最新已发布”（解析结果存于 `resolved_recipes`）；还可显式绑定
  `rule_set_code/version`，缺失或未发布返回稳定错误，评估在不可变规则快照上重放。
- **幂等重放**：评估结果按幂等键持久化；重放返回同一 `evaluation_id` 与
  `replayed: true`；同键不同请求体 → 409。数据库唯一约束兜底并发冲突。
- **旧接口兼容**：`/rules` 单条规则接口与未绑定规则集的评估行为完全保留；
  旧结果仅新增恒定为 `null` 的 `rule_set` 字段。升级时自动为已有库补齐新列。
- **最小缺失集合**：展开过程收集全部直接缺失引用（去重、稳定排序）后一次性
  返回 `422 INCOMPLETE_INPUT`；失败评估不占用幂等键。
- **稳定输出**：所有列表确定性排序，数值统一 `round(., 6)`，便于审计与 diff。

## 9. 目录结构

```
app/
  main.py                 # FastAPI 入口、建表/增量列迁移、统一错误格式
  config.py / db.py       # DATABASE_URL、引擎与会话
  models.py               # Ingredient / Rule / RuleSet(+Version) / Recipe(+Version) / Evaluation
  schemas.py              # 请求校验（单位、编码规范化、>100% 拦截、base_version/绑定）
  units.py                # 质量(g)与浓度(ppm)换算
  services/
    expansion.py          # 递归展开、循环检测、最小缺失集合
    evaluation.py         # 过敏原累积、规则/快照匹配、稳定排序输出
    rule_sets.py          # ppm 归一化、重叠冲突检测、摘要与摘要哈希
  routers/                # ingredients / rules / rule_sets / recipes / evaluations
tests/                    # pytest（SQLite 内存库，91 个用例，含并发竞速与删除—同号重建回归）
examples/quickstart.sh    # 端到端 curl 示例（含规则集发布、冲突与快照重放）
examples/concurrency_check.sh  # 并发复验：草稿写 vs 发布的原子胜负顺序
Dockerfile / docker-compose.yml
```
