# Harness

自定义 mini-swe-agent 改造层。**框架已完整 vendor**：`harness/minisweagent/` 是
mini-swe-agent 2.4.6 的完整副本（agents/environments/models/config，MIT 协议见
`harness/minisweagent/LICENSE.md`），启动脚本让 `import minisweagent` 优先解析到这里，
不再依赖 uv tool 安装的 site-packages。后续测试一律用本 harness。

## 结构

```
harness/
├── pb-recursive          # 启动脚本（PYTHONPATH 指向 harness，用项目 venv 跑）
├── minisweagent/         # vendor 的 mini-swe-agent 2.4.6（唯一事实源，可自由改）
├── runners/
│   └── pb_recursive.py   # 递归生成 runner（四阶段流水线）
└── configs/
    └── pb-atlas.yaml     # 资源覆盖（8核/8G）+ GLM 价格注册
```

## 用法

```bash
# 递归生成 runner（自包含：vendored 框架 + 项目 venv）
harness/pb-recursive \
  --model openai/GLM-5.3 \
  --probe-model openai/GLM-5.3-Flash \
  --filter ariga__atlas --workers 1 \
  -c harness/configs/pb-atlas.yaml \
  -o methods/glm-atlas-recursive-v1.3

# 对照：官方 runner（原样保留，独立于本 harness）
mini-extra programbench --model openai/GLM-5.3 --filter ariga__atlas \
  -c "$HOME/.local/share/uv/tools/mini-swe-agent/lib/python3.13/site-packages/minisweagent/config/benchmarks/programbench.yaml" \
  -c harness/configs/pb-atlas.yaml \
  -o methods/glm-atlas-baseline-v2
```

## 流水线（pb_recursive.py）

| 阶段 | 模型 | 产物 | 步数上限 |
|---|---|---|---|
| probe | 探测模型（--probe-model） | probe_findings.json（命令树全深度 + 错误路径 + 边界行为 + 内嵌表达式语言，agent 全权枚举）；收尾由纯程序实跑每条 check 得到 validated_checks.json | 200 |
| plan | 主力 | ARCHITECTURE.md + specs.json（模块规格，冻结）+ 骨架 + compile.sh | 160 |
| implement（并行） | 主力 | 各模块代码 + module_status.md | 150/模块 |
| integrate | 主力 | compile.sh 产出 executable + 全回归通过 | 300 |

上下文隔离：各阶段独立会话，只通过结构化产物（JSON/代码）传递。
plan 失败时 implement 回退为单会话全量实现（MONOLITHIC 模板，吃 probe_findings.json）。

## 设计决策

- **probe 不用脚本枚举**（v1.3 起）：cobra-help 解析器泛化性/鲁棒性太差（只认
  "Available Commands:" 两层、遇到非 cobra CLI 就抓瞎），全部交给探测 agent——
  它自己从 `--help` 递归展开命令树（实测可到第三层并发现隐藏命令），并补齐
  错误路径/环境变量/配置文件/stdin 等脚本够不到的行为。
- **plan 输入 = probe_findings.json（唯一）**：脚本产物 probe_matrix.json 已废除。
- **probe 自带验收收尾**：探测 agent 写的 check 期望值可能是编的，所以 probe 会话一结束
  就用纯程序在容器里实跑每条 check，只留真通过的。它与 probe 共用容器和产物，不单列
  成阶段。
- **vendor 而非 import site-packages**：重装/升级 mini 不影响 harness；后续对
  框架层（如 agent 自进化）的修改直接改 harness/minisweagent。

## 状态

- [x] 骨架：四阶段调度 + 启动脚本（--help 可跑）
- [x] v1/v1.2 已跑通端到端（ariga__atlas，见 methods/）
- [x] v1.3：脚本枚举层移除，probe 全权交给 agent；框架 vendor 进 harness
