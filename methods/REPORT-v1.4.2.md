# 递归生成 harness（v1.4.2）vs v1.3 / baseline 对比报告

**任务**：`ariga__atlas.6d81150`（Atlas 数据库 schema 管理 CLI，Go）
**日期**：2026-10-10

| | baseline | v1.3 | v1.4.2 |
|---|---|---|---|
| **分数** | 72 | **83** | 80 |
| **成本** | $36.5 | $43.9 | $57.0 |
| **时长** | 2.6h | 3.0h | 3.35h |
| API 调用 | 381 | 1180 | 1224+ |
| 模块数 | — | 9 | 9 |

**一句话结论**：v1.4.2 放大了全部步数限制（验证预算天花板），分数 80 未超
v1.3 的 83，但**暴露了一个高价值 bug**（4/9 模块死于「收尾协议缺失」），
同时 probe 优化带来了覆盖率的可度量提升。

---

# 一、v1.4.2 前的框架优化（probe 阶段）

## 1.1 probe 模板重写

v1.4.2 之前对 probe 阶段做了一轮专项优化（在 `methods/probe-coverage-test`
验证），核心变化：

| 项 | 优化前 | 优化后 |
|---|---|---|
| 脚本预扫描 | runner 内置 cobra help 树解析器（脆弱，跨任务不通用） | **删除脚本预扫描**，agent 自己递归展开 help 树（"recursively expand every subcommand's --help (all depths)"） |
| 探测范围 | help 树 + 常规错误路径 | 增加**隐藏/内部命令发现**、`__complete` 补全协议、`login/whoami/push` 等非 help 列出命令 |
| data 块枚举 | 泛泛的「尝试 data 块」 | **逐 kind 枚举**（aws_rds_token / gcp_cloudsql_token / external / sql），每种记精确报错 |
| URL scheme | README 提到的 | 显式覆盖 `aws://` `awsrds://` `gcp://`（含 mysql/postgres 变体）等全部推断得出的 scheme |

## 1.2 覆盖率检查（用测试用例反向验证 probe 产物）

**方法**：对照 `tests.json` 的 **1719 个去重测试**（16 个 branch），逐维度核对
优化后 probe 的 517 条 findings——即「测试需要的，probe 有没有真实探测过」。

**① 命令覆盖：15/15 全覆盖**（findings 中真实执行次数，含参数变体）：

```
completion(12)   license(3)   version(4)
migrate: apply(31) diff(22) hash(9) import(12) lint(19) new(10) set(7) status(18) validate(10)
schema: clean(16) fmt(11) inspect(178)
```

测试最重的三个面——migrate apply（164 个测试引用）、schema inspect（76）、
migrate status（86）——恰好是探测执行最多的（31/178/18 次），分布合理。

**② 行为类别：全部有证据**

| 类别 | 证据数 | 类别 | 证据数 |
|---|---|---|---|
| help 输出 | 35 | URL/datasource | 309 |
| **错误路径（非零退出）** | **291** | config/env/var | 127 |
| completion（含 `__complete` 隐藏命令） | 16 | sqlite 真实执行 | 165 |
| version | 6 | file:// 输入 | 216 |
| license | 3 | stdin | 5 |

错误子类型齐全：`unknown command`×19、`required flag not set`×16、
`no such file`×47、URL scheme 错误、HCL/SQL parse 错误×6、community build 横幅×1。

**③ flag 覆盖：测试关注的全部探过**——29 个长 flag + `-u`×149 + `-c`×113，
测试点名的 `--dry-run`/`--auto-approve`/`--tx-mode`/`--lock-timeout` 等逐一对上。

**④ 横向对比**（同任务历史版本）：v1.2=343 条 → v1.4=415 条 → **本次 517 条**。
去掉脚本枚举层后覆盖率不降反升——agent 拿到全部步数预算（不再分给「对齐脚本
产物」）+ 提示词明确「自己递归建树」。

**结论**：「探测结果是完整且全面的——1719 个测试所需的命令面、flag 面、
错误路径面，findings 里都有结构化证据，没有发现测试需要而探测缺失的行为维度。」

**配套改动**（同轮落地）：
- `command` 字段统一字面量 `./executable <args>` 前缀（下游程序化匹配免归一化）
- probe 步数 110 → **200**、wall_time 3600 → **7200**（实测 ~19s/步，200 步
  ≈64 分钟，原墙钟 60 分钟会先于步数触发——预算等于白给）

---

# 二、v1.4.2 的改动与结果

## 2.1 改动：全面放大步数限制（无 token 预算）

| 阶段 | v1.3 | v1.4.2 |
|---|---|---|
| probe | 110 步 | 150 步（检查后提至 200） |
| plan | 130 步 | 160 步 |
| 模块 | 80 步 | **150 步** |
| integrate | 220 步 | 300 步 |
| cost_limit | 0 | 0（无 token 预算） |
| workers | 5 | 8 |

目的：验证「模块预算用满 = 还没做完」假设的上限。

## 2.2 结果：功能块对比

| 块 | 测试数 | baseline | v1.3 | v1.4.2 | 最优 |
|---|---|---|---|---|---|
| basic/cli | 719 | 98.3% | 99.2% | 99.0% | v1.3 |
| migrate | 451 | 38.8% | 66.7% | 65.9% | v1.3 |
| **schema** | 278 | 67.6% | 70.5% | **73.0%** | **v1.4.2** 🎉 |
| cmdlog | 86 | 4.7% | 53.5% | 44.2% | v1.3 |
| config/env | 77 | 71.4% | 80.5% | 67.5% | v1.3 |
| datasources | 31 | 0% | 25.8% | **0%** | v1.3 |
| other | 90 | 56.7% | 65.6% | **73.3%** | **v1.4.2** 🎉 |
| **总通过** | 1732 | 68.1% | 80.0% | 79.0% | v1.3 |

**schema 和 other 创历史新高**，但 datasources 归零、cmdlog/config 回退——
不是探测或设计问题，是下面这个 bug。

## 2.3 核心发现：收尾协议缺失导致模块早死

**4/9 模块死于 RepeatedFormatError**，预算没用完就被掐死：

| 模块 | 步数（上限150） | 退出方式 |
|---|---|---|
| migratecmd / migratedir / project / schemahcl | 150 | LimitsExceeded（正常用满） |
| sqlitedrv | 124 | RepeatedFormatError（早死） |
| cmdlog | 83 | RepeatedFormatError（早死） |
| cmdapi | 73 | RepeatedFormatError（早死） |
| schemacmd | 48 | TimeExceeded |
| **sqlclient** | **45** | **RepeatedFormatError（早死）** |

**死因序列**（以 sqlclient 为例，轨迹还原）：

```
模块跑完最终验证（"all tests pass"）
  → 想输出文本总结收工
  → harness 要求每条回复必须带 bash 工具调用
  → 连续 3 次纯文本回复
  → RepeatedFormatError，会话被杀
```

**根因**：integrate 模板有收尾指令（`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`），
但 **probe / plan / implement 三个模板都没有**——做完活的会话不知道正确退出方式。

**直接后果链**：sqlclient 45 步死 → data 块没做完 → **datasources 0%**；
cmdlog 83 步死 → 44%（v1.3 是 53.5%）。

**修复**：三个模板全部加「HOW TO FINISH」指令——完成标准
达成后必须用 `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 收尾。

## 2.4 成本归因

$57.0（历代最贵）：步数放大后 4 个模块用满 150 步 + integrate 208 步自然收敛。
缓存输入仍占大头（~77%），成本 ∝ 步数 × 上下文，符合此前分析。

---

# 三、三大痛点与对应解决方案（方法论总结）

## 痛点 1：探测盲区

> 单会话 agent 依赖自身直觉决定探测什么、探测多深，没有系统性的行为枚举，
> 导致长尾命令和边角行为在实现之前就已缺失。

**表现**：baseline 的 datasources 0%、cmdlog 4.7%——不是实现错了，是不知道要做。

**解决方案：probe 阶段 + 显式探测协议**

- 独立 probe 会话（Flash 模型，成本 ~$0.13）：递归展开 help 树全深度 +
  隐藏命令 + URL scheme 全变体 + HCL data 块逐 kind 枚举
- 产出结构化 `probe_findings.json`（每条含 command/stdout/stderr/exit_code），
  把「有哪些功能」从模型直觉变成**显式清单**
- 覆盖率经 1719 个测试反查验证：**命令 15/15、行为类别、flag 全覆盖**（第二节 1.2）

**效果**：datasources 0% → 25.8%；
probe 本身只花 $0.13-0.2（Flash 单价 1/20）。

## 痛点 2：注意力稀释

> 上下文随步数线性膨胀，单个功能的实现深度由其在历史中的占比决定，
> 而非功能本身的复杂度。

**表现**：baseline 单会话峰值 53 万 token，migrate（最复杂功能）只做对 38.8%。

**解决方案：模块独立会话（上下文隔离）**

- plan 阶段产出契约（contracts.json），把任务切成 8-10 个模块
- 每模块一个独立会话，上下文只有几万 token，**全部注意力在本模块**
- 模块间通过冻结契约协作，不共享对话历史

**效果**：migrate 38.8% → 66.7%（+28pp）；v1.4.2 的 schema 73.0%（历史最高）。
同样模型、同样预算量级，只是换了组织方式。

## 痛点 3：无全局视角

> 修复决策只依据会话内可见的失败，既无从评估影响面，也缺乏无界消耗的终止约束。

**表现**：单会话里修错的顺序由「发现的先后」而非「价值大小」决定；模块会话
看不到别的模块，容易在测试不考的边角上无限打磨（v1.1 撞满 200 步 × 10 模块）。

**解决方案：修复职责后移到 integrate（全局优先级修复）**

- 模块硬性修复预算：跑回归 1 次 → 最多修 2 轮 → 无论是否全绿都停
- 模块交出 `module_status.md`（精确记录哪些失败、期望 vs 实际）
- integrate 读全部模块的状态 → 按影响面排优先级 → 先修集成断裂，再修
  高价值失败；明确「修不完是正常的，广度优先于深度」

**效果**：时长 10.4h（v1.1 无限打磨）→ 3.0h（v1.3），分数反而 80 → 83；
cmdlog 4.7%（baseline）→ 53.5%（v1.3）。

## 痛点 → 方案的完整映射

| 痛点 | 方案 | 落点 | 数据证据 |
|---|---|---|---|
| 探测盲区 | probe + 显式协议 | 结构化清单替代直觉 | datasources 0→25.8%；探测命令 15/15 全覆盖 |
| 注意力稀释 | 模块独立会话 | 契约切分 + 上下文隔离 | migrate +28pp；峰值 53 万→几万 |
| 无全局视角 | 修复后移 integrate | 状态文件 + 优先级 | 时长 -71% 且分数 +3 |

---

# 四、版本演进路线图

```
baseline(72)  →  v1.1(80,$120,10.4h)  →  v1.3(83,$44,3.0h)  →  v1.4.2(80,$57,3.35h)
   痛点全面        去cost_limit             职责后移+data块        步数天花板实验
                 暴露:打磨浪费            六块全面超baseline      暴露:收尾协议缺失
```

- **v1.3 仍是综合最优**（83 分 / $43.9 / 3.0h）
- v1.4.2 的价值：① schema/other 创新高（大预算对复杂块有效）；② 发现收尾
  bug
- 下一步：收尾协议修复 + 保持大步数

---

# 五、结论

1. **probe 优化已经过完备验证**：1719 个测试反查确认命令/行为/flag 三维
   全覆盖，探测从「碰运气」变成「有指标的工程」
2. **大预算对复杂块有效但不均匀**：schema 73%（新高）vs config 67.5%（回退）——
   预算放大不解决早死和划分随机性问题
3. **收尾协议是 v1.4.2 最大发现**：4/9 模块未用完预算即死，直接抹掉
   datasources 全部分数
4. **三痛点框架经受住了迭代检验**：每一代的分数变化都能归因到痛点-方案的
   对应关系上，说明这套诊断是有效的分析框架
