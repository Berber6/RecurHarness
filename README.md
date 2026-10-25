# RecurHarness

> A recursive multi-agent harness that reverse-engineers black-box CLI binaries into
> modular source code. The task is split into **probe → plan → implement → integrate**,
> so that no single agent session has to hold the entire program at once.
> Built on [ProgramBench](https://programbench.com).

给定一个**黑盒可执行文件**（源码被删掉的真实开源 CLI 工具）和它的 README，
RecurHarness 驱动 LLM agent 从零写出行为一致的新代码库。它把一个"单会话从头干到尾"
的任务，拆成四个相互隔离、只通过磁盘上的结构化产物交接的阶段。

---

## 为什么拆阶段

单会话 agent（官方 baseline）有三个结构性弱点：

| 痛点 | 表现 |
|---|---|
| **探测盲区** | 单会话凭直觉决定探测什么，长尾命令和边角行为在实现前就已缺失 |
| **注意力稀释** | 上下文随步数线性膨胀（峰值 53 万 token），单个功能的实现深度由其在历史中的占比决定 |
| **修复无全局视角** | 修复只依据会话内可见的失败，无法评估影响面，也缺乏终止约束 |

拆阶段后逐一对应解决：

| 痛点 | 解法 |
|---|---|
| 探测盲区 | **probe** 阶段对命令面/输入面/输出面/状态面做系统枚举，把"有哪些行为"变成显式清单 |
| 注意力稀释 | **implement** 给每个模块一个独立会话，上下文只装本模块该做的事 |
| 修复无全局视角 | 模块只修 2 轮就停并交出状态文件；**integrate** 汇总全局按影响面修复 |

代价是信息在阶段之间**有损传递**：plan 写 spec 时丢掉的东西，implement 拿不回来。
spec 的质量因此直接决定最终分数——这是 RecurHarness 最需要盯住的变量。

---

## 流水线

```
                    一个 Docker 容器（--network none）
                    /workspace/{executable, README.md, assets/}

 ① probe                                  ② plan            ③ implement       ④ integrate
┌──────────────────────────────┐   ┌───────────────┐   ┌────────────┐   ┌────────────┐
│ Flash agent 自枚举命令树       │──>│ 架构师模型     │──>│ N 个模块会话 │──>│ 全局模型    │
│ 错误路径 / 输入族 / 表达式语言  │   │切模块 + 写 spec│   │各读自己的 spec│   │修编译/修回归 │
│ 状态副作用                     │   │写骨架 + 编译   │   │+ checks 跑通 │   │压测不变量   │
│ ────────────────────────      │   │分派全部 checks │   │写 module_status│  │            │
│ 收尾：纯程序实跑每条 check，    │   └───────────────┘   └────────────┘   └────────────┘
│       剔除跑不通的、分离弱检查   │           │                  │                │
└──────────────────────────────┘           ▼                  ▼                ▼
     │                                specs.json       implement_<m>.traj  integrate.traj
     ▼                                ARCHITECTURE.md  module_status.json  submission.tar.gz
probe_findings.json                   骨架 + compile.sh
probe_checks.json → validated_checks.json
```

| 阶段 | 模型 | 并行 | 步数上限 | 产物 |
|---|---|---|---|---|
| **probe** | 探测模型（便宜档） | 否 | 200 | `probe_findings.json` + `probe_checks.json` + `validated_checks.json` |
| **plan** | 主力 | 否 | 160 | `specs.json` + `ARCHITECTURE.md` + 骨架 + `compile.sh` |
| **implement** | 主力 | 是（线程池） | 150/模块 | 模块代码 + 回归脚本 + `module_status.md` |
| **integrate** | 主力 | 否 | 300 | `submission.tar.gz` |

### probe 的四维度方法论

probe 不预设工具类型，给定四个通用维度，让 agent 从工具本身（README、自带样例、
help 输出、错误信息）推导具体探什么：

1. **命令面** — help 树递归全深度 + 隐藏/内部命令 + 每个 flag
2. **输入面** — 识别工具消费的所有输入种类 → 从样例推导格式族 → 枚举变体，
   每种测「合法 / 畸形 / 空缺」三态，逐字记录报错
3. **输出面** — 正常/错误/空/边界值下的 stdout、stderr、退出码
4. **状态面** — 会持久化的命令，在临时目录里跑并记录副作用

这套方法与具体工具无关——换成 CSV 工具、图片工具都用同样的四维度。

### probe 的收尾：把发现变成可信断言

probe agent 会同时产出 `probe_checks.json`，但模型写下的期望值可能是编的。
所以 probe 会话结束后、plan 开始前，有一段**纯程序**的收尾：在容器内实跑每一条
check（干净临时目录 → setup → command → 比对结果），分成三类：

- `valid` — 断言通过，且不止看 exit_code → 交给 implement 当验收标准
- `weak` — 断言通过，但只有 exit_code → 仅作参考，不阻碍模块完成
- `invalid` — 实跑结果和期望不符 → 直接丢弃

没有模型参与，是保证 implement 拿到的东西可信的门禁。它与 probe 共享同一个容器和
同一份产物，所以算在 probe 阶段内，不单列。

### plan 的模块切分与校验

plan 必须**按用户可见命令切模块**，而不是按目标程序的内部包结构切。后者是原作者的
内部分层，用户调不到，切出来会得到一堆零检查的模块，而命令层模块被压垮。

切完由 `slicing_problems()` 程序校验，不过则带反馈重试一次：

| 规则 | 判据 |
|---|---|
| 模块必须可被用户调用 | 零检查模块超过总数一半 → 打回 |
| 不能单点过载 | 单模块独占超过一半检查 → 打回 |
| spec 必须结构化 | 某个模块的 spec 是自由文本而非 JSON 对象 → 打回 |
| spec 必须点名命令 | 没有任何字段列出该模块负责的命令 → 打回 |

字段名刻意不写死（`commands` / `owns_commands` / 任何含 "command" 的键都接受）——
具体字段清单是工具自身的偶然产物，写死会过拟合到 atlas。

---

## module 与 spec 的结构

`specs.json` 顶层只有一个 `modules` 数组，每个模块：

```jsonc
{
  "name": "schema_diff",
  "spec": { ... },              // 工具相关的自由 JSON 对象
  "depends_on": ["cli"],
  "golden_samples": [           // 实测记录
    {"command": "...", "context": "...", "stdout": "...", "stderr": "...", "exit_code": 0}
  ],
  "checks": [                   // 从 validated_checks 分派来的验收断言
    {"command": "...", "setup": "...", "expect": {"exit_code": 0, "stdout_contains": ["..."]}}
  ],
  "reference_checks": [ ... ]   // exit_code-only，参考用
}
```

module 层的六个字段是固定的；`spec` 内部由 plan 自由设计。实测中稳定出现的是
`commands`、`summary`、`shared_packages`、`regression_script`，其余字段
（`flags`、`cobra_registration`、`hcl_error_texts`、`type_matrix_sqlite` 等）
取决于该模块实际承载的行为，不同模块之间并不相同——这是预期的。

> **已知问题**：`golden_samples` 和 `reference_checks` 虽然写进了 specs.json，
> 但 `IMPLEMENT_INSTANCE` 模板目前只注入 `module.spec` 和 `module.checks`，
> 这两个字段的实测数据没有到达 implement agent。

---

## 结果

任务 `ariga__atlas.6d81150`（Atlas 数据库 schema 管理 CLI，Go，1732 个行为测试）：

| 版本 | 通过 | 分数 | 关键改动 |
|---|---|---|---|
| baseline（官方单会话） | 1180 | 68% | — |
| v1.3 | 1385 | 80% | 修复职责后移 + data 块覆盖 |
| v1.4.5-generic | 1419 | 82% | prompt 泛化为通用四维度 |
| **v1.4.6** | **1548** | **89%** | **probe checks 验收机制 + 程序化校验** |
| v1.4.7 | 1317（3 次均值） | 76% | 按用户可见命令切模块 + 程序校验 |
| **v1.4.8** | **1358**（3 次均值） | **78%** | spec 结构校验，方差从 12 分收窄到 3 分 |

### 方差是这条路上最大的陷阱

同一份 probe、同一套模型，plan 产出的 spec 每次都不一样，导致下游分数大幅漂移：

| 版本 | 三次同样输入的通过数 | 极差 |
|---|---|---|
| v1.4.7 | 1429 / 1219 / 1302 | **210** |
| v1.4.8 | 1360 / 1382 / 1331 | **51** |

**任何"改完跑一次看分数"的对比都不可信。** v1.4.8 加的结构校验把方差压到 3 分以内，
但均值没有追上 v1.4.6 的 89 分——差距在 spec 的信息密度，不在 probe。

### 值得注意的发现

- **这不是随机的**：跨 7 次运行统计，spec 字节数与分数相关系数 r=0.04（无关），
  而 `golden_samples` 条数与分数的 r=0.61。写得多不等于写得好，**实测样本才相关**。
- **泛化 ≠ 牺牲性能**：把 prompt 里 atlas 专属的硬编码提示（`mysql://`、HCL data 块）
  换成通用四维度后，分数不降反升——更通用的方法论反而挖得更全。
- **每代定位一个瓶颈**：v1.4.2 发现收尾协议缺失（模块早死）、v1.4.3 修复后暴露
  "早收工致浅"、v1.4.4 发现自测 fixture 代表性不足、v1.4.5 修复。迭代记录见
  [`harness/change_log.md`](harness/change_log.md)。

### 成本

单任务约 155M 输入 token（其中 98.4% 命中缓存）和 1.4M 输出 token，约 $54。
输入输出比约 111:1 是 agent 密集任务的常态——每次工具调用的结果都要塞回上下文。

---

## 仓库结构

```
harness/                     ← RecurHarness 框架（核心）
├── pb-recursive             #   启动脚本（注入 PYTHONPATH，用项目 venv）
├── runners/pb_recursive.py  #   四阶段流水线：调度 + 全部提示词 + 校验逻辑
├── configs/pb-atlas.yaml    #   资源配置（8 核/8G）+ 模型价格注册
├── minisweagent/            #   vendored 的 mini-swe-agent 2.4.6（MIT）
├── README.md                #   框架用法
└── change_log.md            #   逐版本改动与教训

methods/                     ← 各版本运行结果与总结
├── README.md                #   版本索引
├── PROFILES.md              #   分数/成本/时长汇总表
├── REPORT-v1.4.2.md
├── REPORT-v1.4.6.md
├── run_profile.py           #   从 trajectory 生成画像的脚本
└── glm-atlas-recursive-*/   #   各版本产物（大文件已被 .gitignore 排除）

src/programbench/            ← ProgramBench 官方包（评测、任务数据）
```

> **注意**：`methods/*/` 下的产物（trajectory、tarball、二进制）体积可达数十 MB，
> 已在 `.gitignore` 中排除，只保留人写的总结文档和脚本。

---

## 运行

需要 Docker（评测容器）、`programbench` 包和 mini-swe-agent。

```bash
# 1. 安装依赖（在 ProgramBench 项目根）
uv sync

# 2. 拉取任务镜像（首次）
docker pull programbench/ariga_1776_atlas.6d81150:task_cleanroom_v6

# 3. 跑一次递归生成（完整四阶段，见 harness/README.md 了解参数）
harness/pb-recursive \
  --model openai/GLM-5.3 \
  --probe-model openai/GLM-5.3-Flash \
  --filter ariga__atlas --workers 1 \
  -c harness/configs/pb-atlas.yaml \
  -o methods/glm-atlas-recursive-vN

# 4. 评测
uv run programbench eval methods/glm-atlas-recursive-vN
uv run programbench info methods/glm-atlas-recursive-vN
```

**只跑下游（复用固定 probe，用于测方差）**：

```bash
harness/pb-recursive -c harness/configs/pb-atlas.yaml \
  --model openai/GLM-5.3 --filter ariga__atlas --workers 1 \
  --reuse-probe methods/glm-atlas-recursive-v1.4.7 \
  --phases plan,implement,integrate \
  -o methods/reuse-runN
```

模型配置（API key / endpoint）通过 `~/.config/mini-swe-agent/.env` 提供。

---

## 引用

本工作建立在 [ProgramBench](https://programbench.com) 之上：

```bibtex
@misc{yang2026programbenchlanguagemodelsrebuild,
    title={ProgramBench: Can Language Models Rebuild Programs From Scratch?},
    author={John Yang and Kilian Lieret and Jeffrey Ma and Parth Thakkar and
            Dmitrii Pedchenko and Sten Sootla and Emily McMilin and Pengcheng Yin and
            Rui Hou and Gabriel Synnaeve and Diyi Yang and Ofir Press},
    year={2026},
    eprint={2605.03546},
    archivePrefix={arXiv},
    primaryClass={cs.SE},
    url={https://arxiv.org/abs/2605.03546},
}
```
