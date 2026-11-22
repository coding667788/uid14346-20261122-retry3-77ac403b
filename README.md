# 离线密文备份退役 / 留存校验器

一个**纯离线、纯标准库**的校验器，用抽象标识模拟密文、密钥与份额的保管状态，
证明指定密文备份在退役后无法通过任何登记的恢复路径重建，同时保证必须保留的
备份在无故障与任意单保管域故障下都仍能独立恢复。

* 运行环境：Windows 原生 Python 3.12，仅使用标准库，纯 CPU。
* 安全边界：密文、密钥、份额全部是抽象 ID。系统**只模拟保管状态**，
  初始状态及所有后续检查都不存在、也不可能从抽象图导出任何真实秘密。
* 规模上限：20 个授权动作、12 个步骤、30 个节点、6 个保管域。

## 1. 目录结构

```
solution/            可导入包，也是 `python -m solution` 的入口
  __main__.py        命令行入口（plan / check / evaluate）
  errors.py          结构化错误码与字段位置
  model.py           案例加载、结构校验、凭据状态机、动作应用
  recovery.py        DAG 恢复见证引擎、双重可恢复性缓存、退役报告
  planner.py         有限时域分支限界规划器、不可达状态剪枝
  checker.py         check：逐步复核既有方案
  evaluator.py       evaluate：初始状态只读评估
samples/             本地虚构备份样例（无真实数据）
  demo_case.json                 主样例：分期保留 + 完整退役（含迁移诱饵）
  demo_check_case.json           内嵌合法但非最优 schedule 的 check 样例
  demo_never_registered.json     从未登记凭据的退役分类样例
  demo_pending_case.json         待核验副本同时阻断保留与退役声明的样例
tests/               unittest 断言测试，含独立小规模穷举验证
```

## 2. 快速复验

```bash
# 规划全局最优退役方案
python -m solution plan samples/demo_case.json

# 逐步复核样例中内嵌的既有方案（该样例合法但非全局最优）
python -m solution check samples/demo_check_case.json

# 只读评估初始状态下各目标在每个故障域的最小见证
python -m solution evaluate samples/demo_case.json

# 全部断言测试（57 个）
python -m unittest discover -s tests -v
```

所有结果都从 stdout 以 **UTF-8、键有序（`sort_keys=True`）、无时间戳** 的 JSON
返回；同一案例重复运行字节级一致。结构错误以非零退出码返回：

```json
{
  "error_code": "MISSING_FIELD",
  "field": "horizon",
  "message": "missing required field 'horizon'"
}
```

## 3. 恢复机制（有向无环图）

* **叶节点**：代表有唯一当前副本的实物凭据（`kind: "leaf"`）。实际保管域由
  该副本登记的地点决定，而不是节点标签；同一凭据可被多条路径共享。
* **内部节点**：列出若干**备选配方**（AND/OR 语义）：
  * 一个配方的 `children` 是**必须同时满足**的子节点集合（AND）；
  * 节点上的多个配方是**任选其一**（OR）；
  * `distinct_domains: 0` 表示无域门槛；正整数 `t` 表示指定子节点的具体见证
    必须采用**彼此不共享保管域**的实现，且合并后的实际叶见证覆盖至少 `t` 个
    不同保管域。
* **域集合来自实际叶见证**：一个实现可含多个域，域集合由其消耗的实际叶副本
  登记地点递归取并集形成，绝不只看直接子节点的标签。叶迁移到新域后，任意
  深度的内部节点都能透过标签看到新域。
* **共享叶只计费一次**：多个子路径命中同一叶时取并集，不重复收费。
* **见证最少化**：输出恢复证据时先最小化实际使用的不同叶数，再以叶 ID 列表
  的字典序决胜。

### 两种可用性视图

| 视图 | usable 副本 | pending（待核验）副本 | 用途 |
|---|---|---|---|
| `strict` | 计入 | **不计入** | 保留目标的保全保证 |
| `potential` | 计入 | 计入 | 退役目标的不可恢复证明 |

待核验副本不能计入保留目标的保证，但在证明退役已完成时必须计入潜在恢复集合。

## 4. 凭据生命周期与授权动作

凭据副本状态：`usable → pending → destroyed`，另有 `unregistered`
（从未登记运行副本）。

| 动作 | 语义与约束 |
|---|---|
| `destroy` | 销毁现存副本。授权绑定**凭据 ID + 当前副本世代 + 保管机构 + 执行窗口**。执行后进入 `pending`，不立即断言不可恢复。 |
| `migrate` | 原子动作：在获准保管域制作同一凭据的新副本并使原副本失效，世代 +1。**仅 `usable` 当前副本可迁移**；目标域必须在授权中，新保管机构必须是目标域登记机构。 |
| `receipt` | 核对销毁回执。仅当回执与**当前唯一待核验销毁**匹配、且由**不同组织**确认时有效；随后才可断言该副本不可恢复。过期、旧世代、重复回执都不能消除仍可用的后继副本。 |

* 每个候选动作在一个方案中**至多发生一次**。
* 窗口作用于步骤编号，**左闭右开**：动作可在 `window_start <= step < window_end`
  时执行；窗口右端允许取 `horizon + 1`，因此可在最后一步 `horizon` 执行。
* 存在显式的单步等待 `{"type": "wait"}`，等待不消耗动作、不改变保管状态。

## 5. 保留策略与变更前缀校验

`policy_segments` 给出分期保留策略的边界（同样左闭右开）：

```json
{"target": "T_OLD_BACKUP", "retain": true,  "start": 0, "end": 3},
{"target": "T_OLD_BACKUP", "retain": false, "start": 3, "end": 10}
```

规划器与复核器对**每个变更前缀**（含 step 0 初始状态）检查：凡当前步骤落在
`retain: true` 段内的目标，必须同时存在

1. 无故障下的可用见证；
2. 对**每一个**保管域单独不可用情形下的可用见证（双重可恢复性）。

`retain` 目标若未给策略段，自动补一段覆盖整个时域 `[0, horizon+1)` 的保留段。

## 6. 退役结论

对每个退役目标，在终态用 `potential` 视图给出结论：

* **仍可恢复**：返回最小恢复见证（最少叶数、叶 ID 决胜）。
* **不可恢复且确有销毁凭证**：`irrecoverable_reason = "destroyed_evidence"`，
  逐叶列出销毁回执证据（销毁动作、回执动作、世代、机构、确认方、确认步骤）。
* **不可恢复且存在从未登记的叶**：`irrecoverable_reason = "never_registered"`，
  逐叶标注 `unregistered`。

## 7. plan：全局最优搜索

在有限时域上做深度优先分支限界：

1. **前缀保全剪枝**：任何使当前被保留目标丧失双重可恢复性的转移立即丢弃，
   不可行分支不再展开（统计在 `transitions_rejected_prefix`）。
2. **窗口剪枝**：窗口外动作不产生转移（`transitions_rejected_window`）。
3. **不可达 / 被支配状态排除**：状态键 = `(步骤, 物理保管状态, 已用动作集合)`，
   与历史无关。同一状态若已用不差于
   `(总成本, 动作数, 动作 ID 序列)` 的方案到达，则新分支被支配并剪枝
   （`states_pruned_dominated`）。
4. **双重可恢复性缓存**：`DoubleRecoveryCache` 以物理状态键缓存每个目标的
   无故障 + 单域故障裁决；经不同历史（含纯等待）到达的同一物理状态共享缓存。
5. **决胜**：总成本 → 动作数 → 动作 ID 序列（等待用空串参与排序，总序确定）。

输出中的 `stats` 暴露缓存命中与各类剪枝计数，便于审计搜索过程。

## 8. check：逐步复核

案例文件可内嵌 `"schedule": [...]`。`check` 从初始状态起逐步重放，返回**第一处**
不合规的动作或策略约束：

* 窗口、世代、机构、动作一次性、待核验匹配等违规 → 对应错误码；
* 保全丧失 → `PRESERVATION_LOST`，并给出受影响目标与故障域
  （`"none"` 表示无故障下即失败）；
* 终态退役未达成 → `RETIREMENT_NOT_ACHIEVED`；
* 全部合法时再与规划器的全局最优解比对，给出 `valid` / `valid_not_optimal`
  与 `globally_optimal`。

## 9. CASE.json 接口定义

顶层字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `case_id` | string | 案例标识，允许非 ASCII |
| `horizon` | int 1..12 | 有限时域步数 |
| `domains` | string[1..6] | 保管域 ID，唯一 |
| `organizations` | string[1..6] | 保管机构 ID，唯一 |
| `domain_organization` | object | 域 → 登记机构 |
| `nodes` | object[1..30] | DAG 节点（叶或内部），引用必须可解析、禁止环 |
| `credentials` | object[] | 叶凭据的初始世代、域、机构（机构须与域登记一致） |
| `actions` | object[0..20] | destroy / migrate / receipt 授权 |
| `policy_segments` | object[] | 可选；同目标段不得重叠 |
| `objectives` | object[] | 每目标 `retain` 或 `retire`，至少一个 |
| `schedule` | object[] | 可选；仅 `check` 使用 |

节点：

```json
{"id": "L1", "kind": "leaf", "credential": "C1"}
{"id": "N1", "kind": "internal",
 "recipes": [{"id": "R1", "children": ["L1", "L2"], "distinct_domains": 2}]}
```

动作：

```json
{"id": "A1", "kind": "destroy", "credential": "C1", "generation": 1,
 "custodian": "O1", "window_start": 0, "window_end": 5, "cost": 2}
{"id": "A2", "kind": "migrate", "credential": "C1", "from_domain": "D1",
 "to_domain": "D2", "custodian": "O2", "window_start": 0, "window_end": 5,
 "cost": 1}
{"id": "A3", "kind": "receipt", "destroy_action": "A1", "confirmer": "O2",
 "window_start": 1, "window_end": 6, "cost": 1}
```

主要错误码：`MISSING_FIELD`、`TYPE_MISMATCH`、`DUPLICATE_ID`、
`UNKNOWN_REFERENCE`、`CYCLE_DETECTED`、`VALUE_OUT_OF_RANGE`、
`CREDENTIAL_UNREGISTERED`、`THRESHOLD_UNREACHABLE`、`CUSTODIAN_MISMATCH`、
`CONFIRMER_SAME_ORG`、`POLICY_OVERLAP`、`OUTSIDE_WINDOW`、
`ACTION_ALREADY_USED`、`GENERATION_MISMATCH`、`NOT_USABLE`、
`RECEIPT_OLD_GENERATION`、`RECEIPT_DUPLICATE`、`RECEIPT_MISMATCH`、
`NO_PENDING_DESTRUCTION`、`ENUMERATION_LIMIT`。

## 10. 独立小规模穷举验证

`tests/test_recovery.py` 内置**不共享生产枚举代码**的暴力见证枚举
（直接展开原始案例文档、`itertools.product` 遍历全部配方组合），逐节点比对
叶集合；`tests/test_planner.py` 的 `exhaustive_best_plan` 独立枚举全部动作
子集、排序与等待插入，验证规划器返回解的全局最优性。

覆盖的关键场景：共享密钥导致直接销毁破坏留存、未确认时保留与退役均无法声明、
换代后旧回执失效、子项单独可恢复但无法独立拼组、最终可行但过程曾丧失保全、
可行路径的全局最优性。

## 11. 复杂度与防护

* 见证枚举带记忆化并按 `(叶数, 叶 ID, 域)` 排序；单节点实现数超过
  `IMPL_CAP`（默认 200000）时以 `ENUMERATION_LIMIT` 显式失败，不静默截断。
* 规划器的状态记忆与双重可恢复性缓存均以物理状态为键，等待不产生新物理状态，
  因此等待分支天然被支配剪枝覆盖。
* 所有迭代均按固定 ID 顺序进行，输出集合全部排序，保证可复现。
