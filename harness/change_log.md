# Harness 改动日志

递归生成 harness 的迭代记录。按**版本**分类：每个版本的改动、结果、教训。

## 版本总览

| 版本 | 分数 | 成本 | 时长 | 一句话 |
|---|---|---|---|---|
| baseline | 72 | $36.5 | 2.6h | 官方 harness 一次成型（对照） |
| v1 | 42 | $60.7 | 6.5h | 四阶段框架搭通，被 $3 cost_limit 掐死 |
| v1.1 | 80 | $119.5 | 10.4h | 去 cost_limit，质量上来但无限打磨 |
| v1.2 | 74 | $23.4 | 5.3h | 降本成功，慢网关+时限太紧掉分 |
| **v1.3** | **83** | $41.5 | 3.0h | **职责后移 + data 块覆盖，全面最优** |
| v1.4 | 77 | $33.2 | **2.0h** | workers 8 提速成功，砍预算掉分 |
| v1.4.1 | 80 | $48.8 | 3.3h | 回退预算，未达 v1.3（模块划分随机性） |

---

# 零、初始搭建（2026-09-20）

## 框架

- `runners/pb_recursive.py`：复制官方 `programbench.py`（193 行）改造为四阶段流水线（~300 行）
  - probe（Flash）：脚本枚举 help 树 + LLM 补盲区 → `probe_matrix.json` + `probe_findings.json`
  - plan（主力）：`ARCHITECTURE.md` + `contracts.json`（冻结契约）+ 骨架
  - implement（主力，并行）：每模块独立会话，读契约实现 + 回归脚本
  - integrate（主力）：编译 + 全回归 + 修集成问题
  - 复用官方 `ProgramBenchAgent`/`copy_submission`（import，不复制）
- `pb-recursive`：启动脚本（PYTHONPATH 注入 + mini 的 venv），**site-packages 零改动**
- `configs/pb-atlas.yaml`：资源覆盖（8核/8G）+ GLM 价格注册

## 设计决策

- 不改 site-packages：PYTHONPATH 方案让自定义 runner 与官方并存，重装 mini 不受影响
- 上下文隔离：各阶段独立 agent 会话，只通过结构化产物传递（防屎山核心机制）
- probe 分两层：纯脚本枚举（零 LLM 成本）打底 + LLM 补盲区，覆盖率可度量

## 早期 bug 修复（分阶段验证期）

| # | 问题 | 修复 |
|---|---|---|
| 1 | `ProgressTrackingAgent` 需要必填的 `progress_manager`/`instance_id` | `run_phase` 加 `progress_manager=None`，None 时退回 `DefaultAgent` |
| 2 | cobra help 解析 bug（`line[0].startswith("  ")` 逻辑错，只抓到根命令） | 改显式状态机：找 `Available Commands:` 段 → 收两空格缩进行 → 遇空行结束 |
| 3 | `{{ module }}` 模板变量 undefined | extra_template_vars 传递改为 `_template_vars` 展开 |
| 4 | 模块名含 `/` 导致 output_path 变子目录 | 无害（文件仍按模块组织），后续 plan 自然产出无斜杠名 |
| 5 | 旧 submission.tar.gz 阻止新 run | 分段调试时手动删旧 submission |
| 6 | 容器 7h 超时风险 | container_timeout 调到 14h |

---

# 一、v1：框架验证（42 分 / $60.7 / 6.5h）

**目标**：验证四阶段框架能否端到端跑通。**结论：跑通了，但分数远低于 baseline。**

## 改动

- 四阶段流水线首次完整运行（分 3 轮调试：probe → probe+plan → implement+integrate）
- `--phases` 选项：支持只跑部分阶段，便于分阶段调试
- `container_pull`：plan 产出的 contracts.json 用 `docker cp` 拉到宿主机

## 结果

- 17 模块契约（划分贴近 atlas 真实结构），17/17 模块实现完成
- eval 首次 `compile_failed`（integrate 被 $3 掐死，compile.sh 没写出）→ 手动补 → **42 分**

## 问题与教训

| 问题 | 教训 |
|---|---|
| **`cost_limit=$3` 默认值未覆盖**——每个会话刚进状态就被切，integrate 死前还在修 panic | 必须显式设置 cost_limit（0 = 不限） |
| 分段跑丢容器内状态——implement 在新容器重建骨架，probe_findings 没传过来 | 用单进程完整跑（四阶段同容器） |
| 模块切 17 个太细，固定开销 ×17 | 收敛到 8-10 个 |

---

# 二、v1.1：质量优先（80 分 / $119.5 / 10.4h）

**目标**：去掉成本限制，看模块化能到多少分。**结论：80 分（超 baseline 8 分），但贵 3 倍、慢 4 倍。**

## 改动

| # | 改动 | 理由 |
|---|---|---|
| 1 | **移除 cost_limit**（所有阶段 `cost_limit: 0`） | v1 被 $3 掐死是主因 |
| 2 | **compile.sh 提前到 plan**（骨架自带），integrate 只许调整不许重写 | v1 的 compile_failed 教训 |
| 3 | **模块粒度指导**：plan 模板要求 8-10 个模块 | v1 的 17 个太细 |
| 4 | **单进程完整跑**（四阶段同容器） | v1 分段跑丢状态 |

## 运行中问题与恢复

**网关挂死请求**（比 504 更阴）：2 个会话（hclx 66 分钟 / cmdlog 19 分钟）无响应。
特征：连接 ESTAB 但无数据、litellm 无 Retrying 日志（既不报错也不超时）。

恢复操作（保进度）：
1. `docker commit` 容器现场 → `pb-v11-progress:impl2of10` 镜像（工作零丢失）
2. runner 补丁：① 配置显式给 `environment.image` 时不覆盖（支持续跑）；
   ② implement 幂等——模块 traj 已有 exit_status 则跳过
3. 新配置 `pb-atlas-v11-resume.yaml`：image 指向进度镜像 + `model_kwargs.timeout: 300`
4. workers 3→4 重启：已完成模块正确跳过

**教训**：① 必须配 litellm `model_kwargs.timeout`（防网关挂死）；② docker commit + implement 幂等 = 无损断点续跑

## 结果

- **80 分**：migrate 63%（baseline 39%）、cmdlog 33%（5%）、schema 65%、basic/cli 100%
- datasources 仍 0%

## 问题

1. **成本 $119**（3.3× baseline）——模块全部跑满 200 步（无限打磨）
2. **时长 10.4h**——网关卡顿 + 模块打磨
3. integrate 以 RepeatedFormatError 收尾（连续无工具调用输出）

---

# 三、v1.2：降本优先（74 分 / $23.4 / 5.3h）

**目标**：降本提速。**结论：降本 80%、提速 49% 达成，但分数掉 6 分。**

## 改动

| # | 改动 | 目标 |
|---|---|---|
| 1 | 模块 step_limit 200 → 110 | 降本（v1.1 模块全跑满 200 步） |
| 2 | plan/implement 模板要求「精简回归脚本」（5-10 用例，禁止造大测试套件） | 降本（v1.1 输出 token 大半是测试代码） |
| 3 | 每会话 `wall_time_limit_seconds` | 提速（根治网关卡顿的时间黑洞） |
| 4 | workers 4 → 5 | 提速 |
| 5 | plan 模板强制产物顺序（contracts.json 优先） | 修 plan 跑偏 bug |
| 6 | integrate 模板加工具调用强制 | 修 RepeatedFormatError |

## 结果

- **74 分 / $23.4 / 5.3h**——降本提速达成，分数回退

## 两次运行

| 运行 | 分数 | 成本 | 说明 |
|---|---|---|---|
| 首次（存档 v1.2-fallback） | 73 | $17.7 / 2h | plan 砍到 90 步 → 没产出 contracts.json → **退化为单会话** |
| 正式 | 74 | $23.4 / 5.3h | plan 修复（130 步）→ 10 模块化跑通 |

## 教训

1. **plan 是瓶颈阶段，不能砍预算**——90 步不够写契约+骨架，会话跑偏去做实现
2. **wall_time_limit 与慢网关冲突**：45 分钟上限下，慢网关只能跑 15-25 次调用（正常 60-100+），模块被掐在半成品
3. **意外收获**：fallback 单会话 73 分 / $17.7 / 2h——说明降本参数对非模块化路径也有效

---

# 四、v1.3：职责后移（83 分 / $41.5 / 3.0h）—— 全面最优 🎉

**目标**：修复「模块盲目打磨」。**结论：83 分，六块全面超 baseline。**

## 核心改动：模块自测与 integrate 修复的职责错配

**问题**：v1.1/v1.2 的模块会话陷入「自测→修错→再测」无限循环（v1.1 全跑满 200 步）。
**根因**：模块**看不到全局**，却按「regression 全绿」死磕；真正需要全局视角的修复应该后移到 integrate。

| # | 改动 | 内容 |
|---|---|---|
| 1 | **implement 硬性修复预算** | 跑回归 1 次 → 最多修 2 轮 → 无论是否全绿都停 |
| 2 | **implement 必须写 `module_status.md`** | 记录已实现什么、哪些用例仍失败（精确输入输出） |
| 3 | **integrate 改为全局优先级修复** | 读 module_status.md → 建优先级列表（影响面大的先修）→ 先修集成断裂 → 再修高价值失败；明确「修不完正常，广度优先」 |
| 4 | 时限放宽 | 模块 45→90min、integrate 90→120min、probe 45→60min |
| 5 | 模块 step_limit 110 → 80 | 2 轮修复后应更早收工 |

## datasources 盲区诊断（重要发现）

**不是 URL scheme 问题，是 HCL `data` 块整个未实现**：
所有失败测试报错都是 `Error: config: expected "{" after "data"` —— HCL 解析器不支持
`data "<kind>" "<name>"` 块（data.aws_rds_token / data.gcp_cloudsql_token / data.external / data.sql）。
（v1.2 的 probe 枚举了 247 条 scheme 记录，但 scheme ≠ data 块，方向错了。）

**修复**：probe 模板加「枚举 HCL data 块类型」；plan 模板要求 HCL 模块契约覆盖 data 块。

## 结果

| 块 | baseline | v1.3 | 提升 |
|---|---|---|---|
| basic/cli | 98% | 99% | +1 |
| schema | 68% | 71% | +3 |
| migrate | 39% | 67% | **+28** |
| cmdlog | 5% | 53% | **+48** |
| config/env | 71% | 81% | +10 |
| datasources | 0% | 26% | **+26**（首次得分） |

**六块全部超过 baseline**。时长从 v1.1 的 10.4h 降到 3.0h。

---

# 五、v1.4 / v1.4.1：提速实验（77 / 80 分）

**目标**：在 v1.3 基础上继续降本提速，且要比 baseline（2.6h）更快。

## v1.4（77 分 / $33.2 / **1.98h**）—— 首次快过 baseline

| 改动 | 效果 |
|---|---|
| workers 5 → 8 | ✅ 时长 3.0h → 1.98h（10 模块从 2 波变 1 波） |
| 模块 step 80 → 70 | ❌ 掉分主因 |
| plan 130 → 115 | ❌ 略伤 |
| integrate 220 → 180 | ❌ 略伤 |

**关键发现：模块 step_limit 是质量杠杆，不能砍**

v1.4 里 **9/9 模块全部撞满 70 步**（v1.3 里 10/10 撞满 80 步）——预算用满说明模型
认为「还有活没干完」，**砍预算 = 砍完成度**：

| 块 | v1.3 | v1.4 |
|---|---|---|
| cmdlog | 53% | 14% (**-39**) |
| config/env | 81% | 62% (-19) |
| datasources | 26% | 19% (-7) |
| migrate | 67% | 62% (-5) |

## v1.4.1（80 分 / $48.8 / 3.3h）—— 回退预算，未达预期

- workers 8 保留（提速有效不伤质量）
- step_limit 全部回退（模块 90、plan 130、integrate 220）

**结果未达 v1.3 的 83**：schema 掉 10pp（71%→61%）是主因。

**关键认识：模块划分本身有随机性，是分数波动的主要来源**

同一套 prompt、同一任务，plan 每次分出的模块都不同：

```
v1.3 (10): root, config, sqlengine, sqlclient, schemafile, inspect, diff, apply, migratedir, migrateops
v1.4 (9):  sqlclient, sqlengine, datasrc, hclspec, project, migdir, schemacmd, migratecmd, root
v1.4.1(9): cli, cmd_migrate, cmd_schema, cmdapi, datasource, migratedir, schemahcl, sqlclient, sqlengine
```

**这是 80-83 分波动的真正来源，而非 step_limit 微调。**

---

# 六、经验总结（跨版本）

## 参数杠杆

| 手段 | 提速 | 降本 | 伤质量？ |
|---|---|---|---|
| **加并发（workers）** | ✅✅ 显著 | 中性 | ❌ 不伤 |
| **砍 step_limit** | ✅ 显著 | ✅ 显著 | ✅✅ **重伤** |
| 放宽 wall_time_limit | ❌ 变慢 | ❌ 变贵 | ⚠️ 慢网关下必要（防掐死） |

**结论：要快就加并发，不要砍预算。**

## 设计杠杆（比参数更重要）

| 改动 | 效果 |
|---|---|
| 契约冻结 + 上下文隔离 | 防屎山（模块间零越界） |
| compile.sh 前置到 plan | 消除 compile_failed 归零风险 |
| 模块修复预算（2 轮上限） | 时长 10.4h → 3.0h |
| module_status.md + integrate 全局修复 | cmdlog 12%→53%，migrate 57%→67% |
| HCL data 块枚举 | datasources 0% → 26% |

## 环境问题（非框架，但影响结果）

| 问题 | 对策 |
|---|---|
| 网关挂死（收请求不返回） | litellm `model_kwargs.timeout: 300` |
| 网关慢（504/超时重试） | 放宽 wall_time_limit；重试期间不占预算 |
| 网关卡顿丢进度 | 定期快照 + docker commit + implement 幂等 |

## 下一步方向（v1.5）

**模块划分的随机性**是当前最大瓶颈（80-83 波动的主因）。方向：
在 plan 模板给出「必须覆盖的功能清单」（schema/migrate/cmdlog/datasource/config 各至少一个模块），
消除随机性 + 保留 workers 8 提速。

## v1.3：去脚本化 + 框架 vendor（2026-09-22）

### 改动

1. **删除 probe 脚本枚举层**：`probe_matrix.json` 及其 cobra-help 解析器整体移除。
   理由：解析器泛化性/鲁棒性太差（只认 "Available Commands:"、只爬两层、非 cobra
   CLI 直接抓瞎），而 v1.2 实测探测 agent 本来就会自己递归展开 help 树（322/343 条
   覆盖第三层及更深，还发现 `__complete` 等隐藏命令），脚本的"零成本骨架"价值
   低于维护成本。`samples` 预留字段一并删除。
2. **probe_findings.json 成为 plan 的唯一输入**：PLAN_SYSTEM 只引用 findings；
   probe 结束后 runner 把 findings 从容器拉到宿主机存档（原来只有 submission
   tar 里才有一份，出问题时不好取）。
3. **框架完整 vendor**：`harness/minisweagent/` = mini-swe-agent 2.4.6 完整副本
   （MIT，71 文件 484K）。`pb-recursive` 启动脚本改为 PYTHONPATH 指向 harness +
   `uv run --project`（项目 venv），`import minisweagent` 优先解析到 vendored
   副本，不再依赖 uv tool 的 site-packages。pyproject 补充 litellm/tenacity/
   python-dotenv/platformdirs/requests 运行时依赖。
4. 修复 v1.2 遗留三洞：matrix 路径不一致（已随枚举层删除一并消除）、PLAN_SYSTEM
   引用不存在文件（已修）、implement 假 fallback（改为 MONOLITHIC 单会话真兜底）。

### 验证

- vendored 副本 import 优先级确认（`harness/minisweagent` 排在 site-packages 前）
- 全流水线 import 链（DefaultAgent/DockerEnvironment/LitellmModel/ProgramBenchAgent/
  copy_submission/RunBatchProgressManager）从 vendored 副本 + 项目 venv 全通
- `harness/pb-recursive --help` 正常；probe_phase 源码无 matrix/samples 残留

## 2026-10-10 v1.4.2：放大步数限制（无 token 预算）

### 改动

| 阶段 | 原步数 | 新步数 | 时限 |
|---|---|---|---|
| probe | 110 | 150 | 60min |
| plan | 130 | 160 | 60min |
| 模块 | 90 | 150 | 90min |
| integrate | 220 | 300 | 120min |

cost_limit 保持 0（无 token 预算限制）。workers=8。目的：验证「模块预算用满
= 还没做完」假设的上限——看给足预算后分数天花板在哪。

### 启动信息

- 2026-10-10 11:33 启动，容器 minisweagent-8e6f493d
- 输出 methods/glm-atlas-recursive-v1.4.2

## 2026-10-10 probe 覆盖实测 + 两项调整

### 实测（probe-coverage-test run）

当前代码单跑 probe（110 步实际生效）：$0.126、517 条 findings（v1.2=343 / v1.4=415）。
与 tests.json 1719 个测试对比：15/15 子命令全覆盖、29 个长 flag + -u×149 + -c×113、
291 条非零退出错误路径（unknown command/required flag not set/no such file/parse
error/community 横幅均有证据）。结论：探测完整全面，无需脚本枚举层。

### 调整

1. PROBE_SYSTEM 补 command 字段格式约束：一律用 `./executable <args>` 前缀
   （实测混出三种写法：./executable / /workspace/executable / atlas，程序化分析需归一化）
2. probe step_limit 110→200（110 步时被切断而非自然收尾，预算偏紧）；
   wall_time 3600→7200（实测 ~19s/步，200 步约 64 分钟，3600 会先撞墙钟使 200 步失效）

## 2026-10-10 v1.4.2 结果：80 分 / $56.98 / 3.35h

### 改动（本轮）

步数全部放大：probe 110→150、plan 130→160、模块 90→150、integrate 220→300。
无 token 预算限制，workers=8。

### 结果

| | baseline | v1.3 | v1.4.1 | v1.4.2 |
|---|---|---|---|---|
| 分数 | 72 | 83 | 80 | 80 |
| 成本 | $35.7 | $43.9 | $47.4 | $57.0 |
| 时长 | 2.6h | 3.0h | 3.3h | 3.35h |

### 关键发现：implement 缺收尾指令导致 RepeatedFormatError 早死（4/9 模块）

模块步数分布（步数上限 150）：

| 模块 | 步数 | 退出方式 |
|---|---|---|
| migratecmd/migratedir/project/schemahcl | 150 | LimitsExceeded（正常用满） |
| **sqlitedrv/cmdlog/cmdapi** | 124/83/73 | **RepeatedFormatError（早死）** |
| schemacmd | 48 | TimeExceeded |
| **sqlclient** | **45** | **RepeatedFormatError（早死，data 块没做完）** |

**死因序列**（sqlclient 为例）：模块跑完最终验证（"all tests pass"）→ 想输出
总结收工 → harness 要求每条回复必须带工具调用 → 连续 3 次纯文本 → 被杀。

**根因**：INTEGRATE 模板有收尾指令（`echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`）
但 IMPLEMENT 模板没有——模块做完活后不知道怎么正确退出，只能反复输出文本总结
直到被 RepeatedFormatError 杀死。**预算没用完就被收尾 bug 掐死**。

直接后果：datasources 0%（sqlclient 45 步死，data 块未完成）、cmdlog 44%
（83 步死）。

### v1.4.3 改动方向

1. **implement 模板加收尾指令**（核心修复）：完成标准达成后，最后一条命令
   `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
2. 保留步数 150（自然收敛的模块 45-83 步就完了，给足空间无害）

## 2026-10-10 v1.4.3：修复收尾 bug

### 改动（核心）

**所有阶段模板（probe/plan/implement）加「HOW TO FINISH」收尾指令**：
完成标准达成后，最后一条命令必须是 `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`，
禁止纯文本总结收尾（连续 3 次文本回复会被 RepeatedFormatError 杀死）。

v1.4.2 里 4/9 模块死于这个 bug（sqlclient 45 步、cmdapi 73、cmdlog 83、sqlitedrv 124），
预算没用完就被掐死，datasources 直接归零。

### 其他

步数限制保持 v1.4.2 水平（150/160/150/300），无 token 预算，workers=8。

## 2026-10-10 v1.4.3 结果：73 分 / $23.10 / 3.42h

### 收尾修复全面生效（机制层面 100% 成功）

| 退出方式 | v1.4.2 | v1.4.3 |
|---|---|---|
| Submitted（正常收工） | 4/9 | **9/10** |
| RepeatedFormatError（早死） | 4/9 | **0** |
| TimeExceeded（网关兜底） | 1 | 1 |

模块步数分布（各不同，自然收敛）：11/13/16/37/42/43/50/66/70/104。
plan 53 步、probe 74 步、integrate 168 步——**全部 Submitted 正常收尾**，
v1.2 以来的「会话被上限切死」问题彻底解决。成本 $23.10（回到 v1.2 水平）。

### 但分数 73 < v1.4.2 的 80：早收工的副作用

| 块 | v1.4.2 | v1.4.3 |
|---|---|---|
| migrate | 65.9% | **53.9%** |
| schema | 73.0% | 61.5% |
| datasources | 0% | 3% |
| cmdlog | 44.2% | 39.5% |

**根因：模块「做完就交」导致实现深度不足**。
- 模块步数大幅下降（如 cmdlog 50 步 vs v1.4.2 的 83 步+150 上限）
- 「HOW TO FINISH」指令让会话顺利退出，但也让模块**在浅层完成度下提前满足
  于完成标准**（回归 5-10 用例绿了就收工，但那只是模块自选的代表用例）
- migrate 失败模式：`migrate new/apply` 直接退出码 1（基础路径坏了）——
  模块自测用例没覆盖到的路径，之前靠更多步数的对拍自然发现

### 对照实验结论（v1.4.2 vs v1.4.3 互为镜像）

| | v1.4.2 | v1.4.3 |
|---|---|---|
| 收尾 | 4/9 早死（bug） | 9/10 正常（修复） |
| 模块深度 | 用满 150 步（深） | 11-104 步早收工（浅） |
| 分数 | 80 | 73 |

**两者合起来指向同一结论：分数 ∝ 模块有效工作步数**。
v1.4.2 深但 4 模块早死 → 80；v1.4.3 全活但浅 → 73。最优解是「收尾修复（v1.4.3）
+ 步数上限保留（v1.4.2）+ 完成标准收紧（要求模块对拍覆盖契约全部行为后才许收工）」。

### v1.4.4 方向（如有）

1. 保留 HOW TO FINISH（已验证必要）
2. implement 完成标准收紧：回归用例必须覆盖契约的**全部** frozen_behaviors，
   而非自选 5-10 个代表用例
3. 步数上限维持 150（v1.4.2 证明用满是常态且产出质量高）

## 2026-10-10 v1.4.4：组合完整方案（收工标准收紧）

### 改动（组合三个已验证组件）

1. HOW TO FINISH 收尾协议（v1.4.3 验证：9/10 Submitted，早死归零）
2. 模块步数上限 150（v1.4.2 验证：用满是常态且质量高）
3. **完成标准收紧（新增，堵 v1.4.3 的洞）**：
   - 回归用例规模改为「按契约缩放」：每个 frozen behavior 至少一个用例
     （每个子命令/每个承诺的 flag/每条错误路径），禁止自选几个简单用例交卷
   - DONE 判据改为严格版：回归覆盖契约**全部** frozen behaviors 才算完成，
     「只覆盖部分契约就收工 = 失败的模块」——要求把剩余步数花在未覆盖行为上

### 预期

v1.4.2（深但 4 模块早死，80 分）与 v1.4.3（全活但浅，73 分）互为镜像，
组合后应达 84+。

## 2026-10-10 v1.4.4 结果：73 分 / $47.73 / 2.58h —— 预期未达，发现新根因

### 结果

| | v1.4.2 | v1.4.3 | v1.4.4 |
|---|---|---|---|
| 分数 | 80 | 73 | **73** |
| 成本 | $57.0 | $23.1 | $47.7 |
| 时长 | 3.35h | 3.42h | **2.58h** |
| 模块步数 | 45-150（4 早死） | 11-104（浅） | 38-127（全部 Submitted） |

组合生效的部分：10/10 模块 Submitted + 步数深度回升 + 时长最优。
但分数与 v1.4.3 持平，未达 84+ 预期。

### 新根因：**模块自测的「全绿」与官方测试考的东西错位**

module_status.md 显示 cmdlog 模块自测 18/18 全过、integrate 复核「全部 73 个
frozen regression 条目通过」——但官方 eval 里 cmdlog 只有 13%。

失败样例（`Error: checksum mismatch`、migrate apply 非零退出）：官方测试用的
是 **eval 镜像自带的 test_resources 目录**（真实的迁移文件集
`/workspace/eval/test_resources/test_cmdlog_formatting/migrations_basic`），
模块自测用的是**自己造的 fixture**。两者在 atlas.sum 校验、真实迁移文件解析
等路径上行为不同。

### 三层归因总结（v1.4.2/3/4 对照实验的完整结论）

| 层 | v1.4.2 发现 | v1.4.3 发现 | v1.4.4 发现 |
|---|---|---|---|
| 会话生命周期 | 缺收尾协议→早死 | 修复（9/10 Submitted） | 验证稳定（10/10） |
| 模块深度 | 150 步用满=深 | 早收工=浅 | 步数回升（38-127） |
| **自测代表性** | — | — | **新瓶颈：自测 fixture ≠ 官方考法** |

**当前瓶颈已从「探测盲区/会话管理」转移到「模块自测的代表性」**：
模块按契约自造的测试数据（fixture）与官方测试的输入分布不一致，
导致「自测全绿」无法预测「官方测试通过」。

### 下一步方向（v1.4.5+，待讨论）

1. **fixture 多样化**：implement 模板要求自测数据覆盖多种目录结构
   （带/不带 atlas.sum、不同迁移文件命名），不能只用一种自造样例
2. **integrate 用真实数据压测**：把 probe 阶段观测到的真实输入分布
   （尤其文件类输入）纳入 integrate 的复核
3. 或者接受当前 80 分级别（v1.4.2），把精力转向成本/时长优化

## 2026-10-10 v1.4.5：fixture 代表性修复

### 改动（针对 v1.4.4 发现的第三层瓶颈）

1. **implement 模板加 FIXTURE DIVERSITY 硬要求**：自测数据必须多样化——
   迁移目录带/不带 atlas.sum、不同命名风格（20240101000000_a.sql / V1__x.sql /
   001_x.sql）、空目录、畸形条目；文件类输入至少 3 种不同 fixture 形态。
   明示失败模式：「checksum mismatch 类失败来自你的 fixture 与真实输入不同」
2. **integrate 模板加 STRESS WITH DIVERSE DATA**：收尾前用模块可能没测过的
   fixture 形态压测核心命令，专抓「模块 fixture 不具代表性」的缺口

其余保持 v1.4.4（150/160/200/300 步、workers 8、HOW TO FINISH、严格 DONE 判据）。

## 2026-10-11 v1.4.5 结果：77 分 / $61.82 / 2.79h

### 结果对比（fixture 代表性修复）

| 块 | v1.4.4 | v1.4.5 | 变化 |
|---|---|---|---|
| **cmdlog** | 13% | **56%** | **+43pp** ✅（修复生效的铁证） |
| **datasources** | 3% | **32%** | **+29pp** ✅（历代最高，超 v1.3 的 25.8%） |
| other | 59% | 69% | +10pp |
| schema | 62% | 70% | +8pp |
| migrate | 53% | 55% | +2pp |
| config/env | 74% | 71% | -3pp |

**总分 73 → 77**。v1.4.4 诊断的 fixture 代表性瓶颈被证实且有效修复：
cmdlog（checksum mismatch 重灾区）+43pp、datasources（官方测试用真实数据）+29pp。

### 剩余差距分析（vs v1.3 的 83）

migrate 仍 55%（v1.3 是 66.7%）是主要缺口。cmdlog/datasources 已超 v1.3，
schema 基本追平（70% vs 70.5%）。

migrate 的 11pp 差距可能来自：
1. 模块划分随机性（本次 11 模块，migrate 相关切了 cmdmigrate/migrdir/diffplan 三个）
2. cmdmigrate 模块 105 步接近 150 上限被打断的可能

### 五代连续实验总结（v1.4.2 → v1.4.5）

| 版本 | 改动 | 分数 | 发现 |
|---|---|---|---|
| v1.4.2 | 步数放大 | 80 | 收尾协议缺失（4/9 早死） |
| v1.4.3 | 收尾修复 | 73 | 早收工致浅（自选用例交卷） |
| v1.4.4 | 完成标准收紧 | 73 | fixture 代表性瓶颈 |
| v1.4.5 | fixture 多样化 | 77 | 修复生效（cmdlog +43pp）；剩 migrate 划分随机性 |

**每代发现一个瓶颈、下一代修复它**——分数路径 80→73→73→77 尚未收敛，
但每个机制问题都已定位且有对策。下一步要么针对 migrate（合并为单模块），
要么接受波动进入总结阶段。

## 2026-10-15 v1.4.5-generic：probe/plan/fixture prompt 泛化

### 改动（去 atlas 过拟合，提升跨任务泛化性）

早前 v1.2→v1.4.5 的 prompt 是「补丁式」演进的：每遇一个 atlas 失分点就把
该功能的探测方法硬写进 PROBE_SYSTEM，导致 prompt 含大量 atlas 专属知识。

**PROBE_SYSTEM**：删除 atlas 硬编码（URL scheme 清单 mysql://、HCL data 块
aws_rds_token/gcp_cloudsql_token、./assets/ 路径），换成通用四维度方法论：
1. 命令面（help 树递归 + 隐藏命令 + 每 flag）
2. **输入面**（识别输入种类 → 从 README/自带样例推导格式族 → 枚举变体；
   每变体测合法/畸形/空三种，记录逐字报错）
3. 输出面（正常/错误/空/边界 × stdout/stderr/退出码）
4. 状态面（持久化命令的副作用）
明确原则：「METHOD 固定；探什么由 agent 从工具本身推导，不预设工具类型」

**PLAN_SYSTEM 规则 5**：删除「必须覆盖 HCL data 块」的 atlas 专属要求，
改为通用规则「从 probe 实际发现推导模块清单，每种输入族必须有模块负责，
probe 观察到但无模块覆盖 = 失分缺口」

**implement/integrate fixture 多样性**：把写死的 atlas 文件名示例
（atlas.sum、V1__x.sql）改为通用维度描述（有无辅助文件/备用命名约定/空vs有内容/
畸形条目）

### 目的

验证泛化后 atlas 分数不掉（换来的能力：这套 prompt 能用于全部 201 个任务，
而非只对 atlas 调好）。若 atlas 掉分明显，说明泛化代价过高，需权衡。

### 启动

2026-10-15 22:20，输出 methods/glm-atlas-recursive-v1.4.5-generic

## 2026-10-16 v1.4.5-generic 结果：**84 分**（历史最高）/ $63.15 / 3.51h 🎉

### 泛化 prompt 不仅没掉分，还创了新高

| 块 | v1.3 | v1.4.5(专用) | **v1.4.5-generic** |
|---|---|---|---|
| basic/cli | 99.2% | 98% | 99% |
| **schema** | 70.5% | 70% | **81%** 🎉 历史最高 |
| **migrate** | 66.7% | 55% | **70%** 🎉 历史最高 |
| cmdlog | 53.5% | 56% | 43% |
| **datasources** | 25.8% | 32% | **39%** 🎉 历史最高 |
| config/env | 80.5% | 71% | 74% |
| other | 65.6% | 69% | 67% |
| **总分** | **83** | 77 | **84** 🎉

### 为什么泛化反而更好

通用四维度方法论（命令面/输入面/输出面/状态面）比 atlas 专属补丁**更系统地
覆盖了输入面**：
- 「识别输入种类 → 从 README/样例推导格式族 → 枚举变体 → 每变体测合法/畸形/空」
  这条通用规则，比「枚举 URL scheme」更全面——它逼 agent 想清楚 atlas
  到底吃哪些输入（不只 URL，还有 schema 文件、迁移目录、配置文件…）
- 结果：schema 81%、migrate 70%、datasources 39% 三块同时创新高

### 强有力的证据：泛化 ≠ 牺牲性能

之前担心「去掉 atlas 专属提示会掉分」，实测相反——**更通用的方法论
反而挖得更全**。这说明「补丁式 prompt」其实是次优的：它把注意力引向
已知失分点，却漏掉了没被点名的输入种类。

### 模块退出分布

8 Submitted + 2 TimeExceeded（网关卡兜底），无早死。

### 当前最佳

v1.4.5-generic 84 分 > v1.3 83 分 > baseline 72 分。泛化版同时是
分数最高 + 可跨任务复用。
