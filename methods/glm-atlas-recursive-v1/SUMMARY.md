# glm-atlas-recursive-v1 总结

递归生成 v1：四阶段流水线在 `ariga__atlas.6d81150` 上的首次端到端运行。
对照基线：`methods/glm-atlas-baseline-72`（官方 harness 一次成型）。

## 一、框架总览（是什么）

**官方 baseline**：一个 agent 会话从头干到尾——单条 500 行级 system prompt
（"这是逆向 benchmark，写出完整代码库"），模型自主决定先探测后写码，上下文
一路膨胀到 53 万 token。

**递归生成**：拆成 4 个独立 agent 会话的阶段流水线，上下文只通过**结构化产物**
传递（JSON 契约 / 代码文件），不共享对话历史。每个会话的上下文都只有几万
token，不膨胀。

```
probe ──probe_matrix/findings.json──→ plan ──contracts.json+骨架──→ implement×N ──→ integrate
(Flash)                              (5.3)        (5.3, 并行)              (5.3)
```

**改动位置**：不改官方 yaml/site-packages，全部在 `harness/runners/pb_recursive.py`
里定义 4 组新模板（PROBE/PLAN/IMPLEMENT/INTEGRATE 的 system+instance），
官方的 agent 循环/容器/模型层直接复用。

## 二、四阶段详解（含 prompt 改动）

### 1）probe 探测

| 维度 | 内容 |
|---|---|
| 模型 | **GLM-5.3-Flash**（便宜 1/20，探测是机械活） |
| 并行 | 否——先脚本枚举（零 LLM 成本），后 1 个 LLM 会话补盲区 |
| 步数 | 150 |
| prompt 改动 | **全新角色设定**：baseline 让模型"写实现"，probe 明确 **"You do NOT write an implementation"**——角色从工程师变成实验员 |
| 关键指令 | ① 以脚本产物 `probe_matrix.json`（help 树）为工作清单逐项过；② 每个子命令试缺参/错参/非法 flag；③ 结果**结构化落盘**到 `probe_findings.json`（`{command, stdout, stderr, exit_code}`），不是自由文本；④ 优先补脚本够不到的盲区：错误信息、环境变量、assets/ 配置、stdin |
| 产物 | `probe_matrix.json` + `probe_findings.json`（v1: 70 条发现） |

### 2）plan 规划

| 维度 | 内容 |
|---|---|
| 模型 | GLM-5.3（全局设计，错误代价最大） |
| 并行 | 否 |
| 步数 | 100 |
| prompt 改动 | **角色从"实现者"变"架构师"**：明确基于 probe 产物和 README 做设计而非写码 |
| 关键指令 | ① 产出 `ARCHITECTURE.md`（命令树/模块清单/依赖图/**拓扑序**）；② 产出 `contracts.json`：每模块 `{name, cli_contract, depends_on, golden_samples}`，**接口冻结**（"contracts are frozen once written"）；③ 产出可编译骨架 + **自带可离线构建的 compile.sh**（v1.1 新增，v1 死在这）；④ **"Do not implement module internals yet"**——只搭架子不许实现行为；⑤ 模块数收敛到 8-10 个（v1.1 新增，v1 切了 17 个太细） |
| 产物 | `contracts.json` + 骨架 + `ARCHITECTURE.md`（v1: 17 模块，划分贴近 atlas 真实结构） |

### 3）implement 实现

| 维度 | 内容 |
|---|---|
| 模型 | GLM-5.3 |
| 并行 | **是**（线程池，workers=3，每模块独立会话） |
| 步数 | 200/模块 |
| prompt 改动 | **每会话只看见一个模块**：instance 模板注入 `{{ module.name }}`/`{{ module.cli_contract }}`/`{{ module.depends_on }}`，其余模块对它不可见——上下文隔离防屎山的核心机制 |
| 关键指令 | ① "implement ONLY the module named in the task"；② 只许改本模块目录及其测试（权限纪律）；③ 对着黑盒 `./executable` 抓 golden 输出，固化成模块级回归脚本；④ 完成标准 = 编译通过 + 自己的回归脚本全绿 |
| 产物 | 各模块代码 + 模块回归脚本（v1: 17/17 会话完成，全部撞 $3 上限被切） |

### 4）integrate 集成

| 维度 | 内容 |
|---|---|
| 模型 | GLM-5.3 |
| 并行 | 否（必须等全部模块完成） |
| 步数 | 300 |
| prompt 改动 | **角色变"集成者"**：不再写新功能，只消解组合不确定性 |
| 关键指令 | ① compile.sh 已由 plan 阶段写好——**"never rewrite it from scratch"**，只调模块源码直到离线构建成功（v1.1 新增；v1 里 integrate 自己写 compile.sh 被 $3 上限掐死，eval 报 compile_failed 0 分）；② 跑**全部**模块回归脚本，修集成问题；③ 契约纪律延续：契约错→集中修+记 `CHANGELOG.md`，不许模块内私改；④ 完成标准 = compile.sh 产出 executable + 全回归通过 |
| 产物 | 最终 `submission.tar.gz` |

### 三条贯穿性纪律（所有阶段共享）

1. **接口冻结**：契约一旦写出，后续阶段不许私改；发现契约错走集中变更（CHANGELOG）
2. **权限边界**：implement 只许动自己模块；integrate 只许调模块源码不许重写 compile.sh
3. **结构化交接**：阶段间只传文件（JSON 契约/代码/回归脚本），不传对话历史

## 三、成本对比

| | baseline-72 | recursive-v1 |
|---|---|---|
| 总成本 | **$36.46** | **$60.71** |
| probe | —（无此阶段） | $0.16（Flash） |
| plan | —（含在单会话里） | $3.00（撞上限） |
| implement ×17 | —（单会话） | ~$54.6（**17 × $3 全撞上限**） |
| integrate | —（含在单会话里） | $3.00（撞上限） |
| 输入 token | 107.5M（97.8% 缓存） | 159.1M（97.7% 缓存） |
| 输出 token | 409K | **2.35M**（5.7 倍：更多代码+对拍脚本） |
| api_calls | 381 | 2023 |
| 最大单会话上下文 | **534K**（一路膨胀） | ~220K（每模块独立，不膨胀） |

费用高的根因不是 token 结构，是 **cost_limit=$3 把每个会话都切在半成品状态**。

## 四、得分对比：42 vs 72

| 功能块 | baseline | recursive-v1 |
|---|---|---|
| basic/cli | 98% | 82% |
| schema | 68% | 30% |
| migrate | 39% | 8% |
| config/env | 71% | 21% |
| datasources | 0% | 0% |
| cmdlog | 5% | 0% |
| **总** | **72** | **42** |

原因（按权重）：① $3 cost_limit 切会话（主因，integrate 死时 compile.sh 都没写出来，
手动补后才得 42）；② 分段调试丢失容器内状态（plan 骨架/probe findings 没传到
implement）；③ 模块粒度 17 个偏细，固定开销 ×17。

## 五、速度对比

| | baseline-72 | recursive-v1 |
|---|---|---|
| 总时长 | 2.6h | 6.5h（跨两轮，含间隙） |
| 会话数 | 1 | 20 |

变慢原因：阶段串行依赖 + 每会话冷启动（读环境/摸上下文）×20 + implement 内部
3 并发下 17 模块要排 6 批。

## 六、v1 的真实价值（42 分不算失败的理由）

1. **框架验证达成**：四阶段、契约衔接、并行实现、出分闭环全通；20 个会话无一
   越界改代码——契约纪律成立
2. **架构优势初现**：17 个包按契约解耦，后续修复环可单模块重写不动全局
3. **成本结构可控**：Flash 探测 $0.16 证明「探测下放便宜模型」可行；问题集中在
   cost_limit 一个人为参数，非架构缺陷

## 七、v1.1 改动清单（已在跑）

1. 移除 cost_limit（所有阶段 0，靠 step_limit 控预算）
2. compile.sh 前置到 plan 骨架；integrate 只许调整不许重写
3. 单进程完整跑（四阶段同容器，probe_findings/骨架自然传递）
4. plan 模板要求模块数收敛到 8-10
5. 容器超时 14h
