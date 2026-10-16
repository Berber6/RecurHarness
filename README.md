# programbench-recursive-harness

> A recursive multi-agent harness that reverse-engineers black-box CLI binaries into
> modular source code, splitting the task into **probe → plan → implement → integrate**
> phases to beat single-session baselines on [ProgramBench](https://programbench.com).

给定一个**黑盒可执行文件**（源码被删掉的真实开源 CLI 工具）和它的 README，
这套 harness 驱动一个 LLM agent 从零写出行为一致的新代码库。它把一个"单会话
从头干到尾"的任务，拆成四个相互隔离、通过结构化产物交接的阶段。

**当前最佳结果**（任务 `ariga__atlas.6d81150`）：**84 分**，超过官方单会话基线 **72 分**。

---

## 为什么拆成四阶段

单会话 agent（官方 baseline）有三个结构性弱点：

| 痛点 | 表现 |
|---|---|
| **探测盲区** | 单会话凭直觉决定探测什么，长尾命令和边角行为在实现前就已缺失 |
| **注意力稀释** | 上下文随步数线性膨胀（峰值 53 万 token），单个功能的实现深度由其在历史中的占比决定 |
| **修复无全局视角** | 修复只依据会话内可见的失败，无法评估影响面，也缺乏终止约束 |

四阶段 + 契约驱动，逐一对应解决：

| 痛点 | 解法 |
|---|---|
| 探测盲区 | **probe** 阶段做系统性的四维度枚举（见下），把"有哪些行为"变成显式清单 |
| 注意力稀释 | **implement** 给每个模块一个独立会话，上下文不膨胀，注意力全在本模块 |
| 修复无全局视角 | 模块只修 2 轮就停并交出状态文件；**integrate** 汇总全局按影响面修复 |

---

## 流水线

```
┌─ probe ──────┐  ┌─ plan ───────┐  ┌─ implement ───┐  ┌─ integrate ──┐
│ 探测黑盒行为  │→ │ 切模块 + 冻结 │→ │ N 个模块并行  │→ │ 编译 + 全回归 │
│ Flash 模型    │  │ 契约 + 骨架   │  │ 各写各的代码  │  │ + 全局优先级修 │
└──────────────┘  └──────────────┘  └───────────────┘  └──────────────┘
  probe_findings    contracts.json    每模块 + 回归脚本     submission.tar.gz
  (行为清单)        + compile.sh      + module_status.md
```

| 阶段 | 模型 | 并行 | 输出 |
|---|---|---|---|
| **probe** | GLM-5.3-Flash（便宜 1/20） | 否 | `probe_findings.json` — 结构化行为记录 |
| **plan** | GLM-5.3 | 否 | `contracts.json`（模块契约）+ 可离线编译的骨架 |
| **implement** | GLM-5.3 | **是**（线程池，8 并发） | 每模块代码 + 回归脚本 + `module_status.md` |
| **integrate** | GLM-5.3 | 否 | 最终 `submission.tar.gz` |

**上下文只通过结构化产物交接**（JSON 契约 / 代码文件），各会话不共享对话历史——
这是防止"代码越写越乱"的核心机制。

### probe 的四维度方法论

probe 不预设工具类型，而是给定四个通用维度，让 agent 从工具本身（README、自带样例、
help 输出、错误信息）推导具体探什么：

1. **命令面** — help 树递归全深度 + 隐藏/内部命令 + 每个 flag
2. **输入面** — 识别工具消费的所有输入种类 → 从其样例推导格式族 → 枚举变体，
   每种变体测「合法 / 畸形 / 空缺」三种情况，逐字记录报错
3. **输出面** — 正常/错误/空/边界值下的 stdout、stderr、退出码
4. **状态面** — 会持久化（写文件/建目录/改配置）的命令，在临时目录里跑并记录副作用

**这套方法论与具体工具无关**——换成 CSV 工具、图片工具都用同样的四维度，
agent 自己推出该探什么（数据库工具 → 连接串；CSV 工具 → 分隔符/编码变体）。

---

## 结果

任务 `ariga__atlas.6d81150`（Atlas 数据库 schema 管理 CLI，Go，1732 个行为测试）：

| 版本 | 分数 | 成本 | 时长 | 关键改动 |
|---|---|---|---|---|
| baseline（官方单会话） | 72 | $36.5 | 2.6h | — |
| v1.3 | 83 | $41.5 | 3.0h | 修复职责后移 + data 块覆盖 |
| **v1.4.5-generic** | **84** | $65.2 | 3.5h | **prompt 泛化为通用四维度** |

### 分功能块（v1.4.5-generic vs baseline）

| 功能块 | baseline | v1.4.5-generic |
|---|---|---|
| basic/cli | 98% | 99% |
| **schema** | 68% | **81%** |
| **migrate** | 39% | **70%** |
| **cmdlog** | 5% | 43% |
| **datasources** | 0% | **39%** |
| config/env | 71% | 74% |
| other | 57% | 67% |
| **总** | **68%** | **82%** |

长尾功能（baseline 拿不到分的部分）提升最显著。

### 值得注意的发现

- **泛化 ≠ 牺牲性能**：把 prompt 里 atlas 专属的硬编码提示（`mysql://`、HCL data 块）
  换成通用四维度后，分数不降反升（77 → 84）——更通用的方法论反而挖得更全，
  说明"补丁式 prompt"是次优的。
- **每代定位一个瓶颈**：v1.4.2 发现收尾协议缺失（模块早死）、v1.4.3 修复后暴露
  "早收工致浅"、v1.4.4 发现自测 fixture 代表性不足、v1.4.5 修复。迭代记录见
  [`harness/change_log.md`](harness/change_log.md)。

---

## 仓库结构

```
harness/                     ← 递归生成框架（核心）
├── pb-recursive             #   启动脚本（注入 PYTHONPATH，用项目 venv）
├── runners/pb_recursive.py  #   四阶段流水线 runner
├── configs/pb-atlas.yaml    #   资源配置（CPU/内存/超时/价格）
├── minisweagent/            #   vendored 的 mini-swe-agent 框架
├── README.md                #   框架用法
└── change_log.md            #   逐版本改动与教训

methods/                     ← 各版本运行结果与总结
├── README.md                #   版本索引
├── PROFILES.md              #   分数/成本/时长汇总表
├── REPORT-v1.3-vs-baseline.md
├── REPORT-v1.4.2.md
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

# 3. 跑一次递归生成（见 harness/README.md 了解参数）
harness/pb-recursive \
  --model openai/GLM-5.3 \
  --probe-model openai/GLM-5.3-Flash \
  --filter ariga__atlas --workers 8 \
  -c harness/configs/pb-atlas.yaml \
  -o methods/glm-atlas-recursive-vN

# 4. 评测
uv run programbench eval methods/glm-atlas-recursive-vN
uv run programbench info methods/glm-atlas-recursive-vN
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
