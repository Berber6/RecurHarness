# 递归生成 harness（v1.4.5-generic）vs v1.3 / baseline 对比报告

**任务**：`ariga__atlas.6d81150`（Atlas 数据库 schema 管理 CLI，Go）
**日期**：2026-10-16

| | baseline | v1.3 | **v1.4.5-generic** |
|---|---|---|---|
| **分数** | 72 | 83 | **84** 🎉 |
| **成本** | $36.5 | $45.3 | $65.2 |
| **时长** | 2.6h | 3.0h | 3.51h |
| API 调用 | 381 | 1180 | 1302 |
| 模块数 | — | 9 | 10 |
| 总通过 | 1180/1732 (68%) | 1385/1732 (80%) | **1419/1732 (82%)** |

**一句话结论**：v1.4.5-generic 取得**全系列最高分 84**（首超 v1.3 的 83、比
baseline 高 12 分），schema 块 81%（历史新高）是主要拉动力；但代价是成本升至
$65（系列最贵），且失败暴露了下一条瓶颈——**probe 观测的成功路径深度不足**
（192/310 失败集中在 migrate apply 的深层语义，这些行为需在真实 sqlite 上
跑通多步工作流才能观测，而 probe 未系统采集）。

---

# 一、v1.4.5-generic 做对了什么

## 1.1 模块划分从「按命令」转向「按功能域」

v1 是 17 个包（贴 atlas 真实目录结构，过细，固定开销 ×17）；v1.4.5-generic
收敛到 **10 个模块，且按功能域切分**（非按命令组）：

```
sqlurl  project  schemafmt  schemainspect  schemadiff
schemaapply  migdir  migrateapply  migrateplan  cli
```

关键：`schemaapply`/`migrateapply`/`migrateplan` 把「命令 × 共享引擎」拆开，
契约里明确 `depends_on` 拓扑（sqlurl/project 是所有 schema/migrate 模块的底座）。
这带来最直接的收益——**schema 块 71%（v1.3）→ 81%**。

## 1.2 端到端自然收敛（无早死）

| 阶段 | 步数 | 成本 | 退出方式 |
|---|---|---|---|
| probe | 84 | **$0.07** | Submitted（自然收尾） |
| plan | 103 | $4.79 | Submitted |
| implement ×10 | 16–120 | ~$44 | 8/10 Submitted，2 个 TimeExceeded |
| integrate | 300 | $16.13 | **LimitsExceeded**（撞上限） |

对照 v1.4.2 的核心 bug（4/9 模块死于 RepeatedFormatError，预算没用完就死），
v1.4.5-generic 的 **8/10 模块自然 Submitted**——收尾协议修复彻底生效。

## 1.3 probe 极低成本拿到高覆盖

probe 仅 $0.07（84 步，Flash），产出 `probe_findings.json`。plan 的契约里
自述 *"Reconstructed from black-box probing: 345 recorded invocations"*——
**plan 确实把 probe 的结构化观测吃进了契约**（这正是「痛点 1：探测盲区」
方案的预期效果）。

---

# 二、v1.4.5-generic 的改动与结果

## 2.1 功能块对比

| 块 | 测试数 | baseline | v1.3 | **v1.4.5-generic** | 最优 |
|---|---|---|---|---|---|
| basic/cli | 719 | 98.3% | 99.2% | 99.2% | 平 |
| migrate | 451 | 38.8% | 66.7% | **69.8%** | **v1.4.5-g** 🎉 |
| **schema** | 278 | 67.6% | 70.5% | **80.9%** | **v1.4.5-g** 🎉 |
| cmdlog | 86 | 4.7% | **53.5%** | 43.0% | v1.3 |
| config/env | 77 | 71.4% | **80.5%** | 74.0% | v1.3 |
| datasources | 31 | 0% | 25.8% | **38.7%** | **v1.4.5-g** 🎉 |
| other | 90 | 56.7% | 65.6% | **66.7%** | **v1.4.5-g** 🎉 |
| **总通过** | 1732 | 68.1% | 80.0% | **81.9%** | **v1.4.5-g** 🎉 |

**四个块创历史新高**（migrate / schema / datasources / other），总分数 84 系列第一。
datasources 从 0%（baseline）→ 38.7%，主要得益于 data 块逐 kind 枚举。

---

# 三、核心发现：下一条瓶颈在 probe 的「成功路径深度」

## 3.1 失败分布：表层几乎全过，深层是主战场

310 个失败里：

| 类别 | 失败数 | 性质 |
|---|---|---|
| help / usage / version / completion 等**表层** | **11** | 几乎全过 |
| `migrate apply` **深层语义** | ~192 | **主战场** |

深层失败细分：`template`(50)、`baseline`(10)、`txmode`(10)、`exec_order`(9)、
`index`(8)、`foreign_key`(7)、`data_preservation`(2)…

这些行为的共同特征：**不是「接口/报错」层面，而是「需要真实成功执行 + 多步
数据库状态累积」才能观察**。

## 3.2 排除两个假设

**假设 A：plan 阶段有问题？——否。**
`contracts.json` 质量高：10 模块 × 2.7–10KB 结构化契约（`role`/`behaviors`/
`errors`/`regression`/`required_flags`）+ 89 个 `golden_samples` + `command_tree`
+ 拓扑序，且明确消费了 probe 的 345 条观测。plan 不是病灶。

**假设 B：禁网导致深层测试过不了？——否。**
评测分阶段：**compile 阶段禁网，test 执行阶段有网**（`DOCKER_RUN_ARGS=[]`，
源码注释 *"Test-execution containers are never touched"*，实测
`getent hosts github.com` 解析成功）。且失败主力 `migrate apply` 用 **sqlite**
（纯离线），与网络无关。实测验证：在 `--network none` 容器里手工跑通
`migrate new → apply → status → schema inspect`，捕获到真实 DDL + 数据保留
行为——**成功路径完全可离线观测**。

## 3.3 真因：probe 未系统采集「成功路径的状态演化」

对照基线 probe 的 findings，缺的确切项：
- ✗ **data preservation**（插数据 → 变更 schema → 验证数据保留）
- ✗ **index / unique constraint** 的 DDL 形态

而 template / foreign-key / baseline / tx-mode 是**探过但仍失败**的——那属于
implement/integrate 的实现深度问题，非 probe 缺口。

**根因**：probe 的样例 schema 太简单（一两张简单表），成功输出只覆盖了
建表/主键，没覆盖索引/唯一约束/多表关系等**对象特性**——这些恰是深层测试要测的。

---

# 四、尝试过的优化与教训（probe 提示词强化）

针对 3.3 的缺口，做了一轮 probe 提示词强化实验（在本报告同期，见 `harness/pb_recursive.py`），
**三连失败，值得记录**：

| 尝试 | 改动 | 结果 | 结论 |
|---|---|---|---|
| 1 | 加「SUCCESS-PATH 维度置顶 + 硬约束落盘格式」 | 条数 517→263；首跑出现**格式漂移**（agent 写成研究报告 dict，0 条可消费记录） | 格式硬约束**有效**（修好了漂移）；但强化的成功路径**挤占广度** |
| 2 | 修格式 + 删「前 1/3 预算」硬约束 | 格式合规、前缀统一，但条数仍降 | 措辞微调不解决问题 |
| 3 | 加「OBJECT richness」明确要求 | agent **60 步就自判完成提交**，index/unique/INSERT **仍全缺** | **光靠改提示词措辞，无法稳定控制探测深度** |

**教训**：探测深度不能靠「劝说」模型，需要**结构化约束**（runner 代码校验
findings 覆盖度，缺则带缺口清单补跑一轮），而非自然语言指令。

---

# 五、三大痛点框架的检验

沿用 v1.4.2 报告的三痛点诊断，看 v1.4.5-generic 的位置：

| 痛点 | 方案 | v1.4.5-generic 的证据 |
|---|---|---|
| 探测盲区 | probe + 显式协议 | ✅ probe $0.07 产出 345 条观测被 plan 消费；datasources 0→38.7% |
| 注意力稀释 | 模块独立会话（契约切分） | ✅ 10 模块按功能域切分，schema 71→81%（历史新高） |
| 无全局视角 | 修复后移 integrate + module_status | ⚠️ integrate 撞 300 步上限（LimitsExceeded）——修复预算再次成为瓶颈 |

**新暴露的痛点 4：probe 的观测粒度停在「接口/报错」，未达「成功态语义」。**
这是探测盲区（痛点 1）的一个更深的子问题——不是「不知道有哪些命令」，而是
「知道命令但没观测它成功时的完整行为」。

---

# 六、版本演进路线图

```
baseline(72) → v1.1(80,$120,10.4h) → v1.3(83,$45,3.0h) → v1.4.2(80,$57,3.35h)
   痛点全面      去cost_limit          职责后移+data块       步数天花板实验
                暴露:打磨浪费         六块全面超baseline     暴露:收尾协议缺失
                                          ↓
                          v1.4.3(73) → v1.4.4(73) → v1.4.5(77) → v1.4.5-generic(84)
                              波动       波动        按命令划分    按功能域划分+schema专项
                                                                 ★ 系列最高分
```

- **v1.4.5-generic 是新的综合最优**（84 分 / $65.2 / 3.51h）
- 代价：成本系列最贵（integrate 撞 300 步 $16 + 模块预算放大）
- 下一步方向：① probe 结构化覆盖校验（痛点 4）；② integrate 预算再放宽

---

# 七、结论

1. **v1.4.5-generic 是全系列最高分 84**：模块按功能域划分 + schema 专项优化，
   使 schema(81%)/migrate(70%)/datasources(39%)/other(67%) 四块同创新高
2. **plan 阶段已验证无问题**：契约质量高、确实消费了 probe 观测——瓶颈不在 plan
3. **禁网不是瓶颈**：评测测试阶段有网，且失败主力是离线 sqlite 行为
4. **下一条瓶颈是 probe 的「成功态语义观测」**：310 失败中 192 个是
   migrate apply 深层语义（template/index/约束/数据保留），这些需多步成功
   工作流才能观测，当前 probe 未系统采集
5. **probe 提示词强化三连失败**：措辞调优无法稳定控制探测深度，下一步应转向
   **代码级覆盖校验 + 补跑**的结构化方案
