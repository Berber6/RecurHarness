# Methods

历次推理 run 的存档，按方法分目录，用于横向对比（原始 harness vs 改进 harness vs 递归生成）。

命名规范：`<方法>-<任务>-<分数>`，目录结构保持 mini-swe-agent 原样（可直接用
`programbench eval` / `info` 重新评测）。方法示例：`glm-atlas-baseline-72`（原始
harness）、`glm-atlas-golden-v1-XX`（模板改进 v1）、`glm-atlas-recursive-XX`（递归生成）。

## 当前存档

| 目录 | 任务 | 分数 | 日期 | 说明 |
|---|---|---|---|---|
| `glm-atlas-baseline-72/` | ariga__atlas.6d81150 | 72 | 2026-09-17 | GLM 5.3 原始 harness，首次本地复现 |
| `glm-atlas-recursive-v1/` | ariga__atlas.6d81150 | 42 | 2026-09-21 | 递归生成 v1（四阶段，$3 cost_limit 卡死会话，compile.sh 手动补） |
| `glm-atlas-recursive-v1.1/` | ariga__atlas.6d81150 | 80 | 2026-09-21 | 递归生成 v1.1（去 cost_limit、compile.sh 前置、10 模块、单进程+一次断点续跑） |
| `glm-atlas-recursive-v1.2-fallback/` | ariga__atlas.6d81150 | 73 | 2026-09-22 | v1.2 首次（plan 90 步不够→无契约→退化为单会话；$17.7/2h 降本参考） |
| `glm-atlas-recursive-v1.2/` | ariga__atlas.6d81150 | 74 | 2026-09-22 | v1.2 正式（降本 80% 达成，慢网关撞时限掉分） |
| `glm-atlas-recursive-v1.3/` | ariga__atlas.6d81150 | 83 | 2026-09-22 | **v1.3（修复模块盲目打磨 + data 块覆盖，最优）** |
| `glm-atlas-recursive-v1.4.2/` | ariga__atlas.6d81150 | 80 | 2026-10-10 | 步数天花板实验；发现收尾协议缺失 bug |
| `glm-atlas-recursive-v1.4.3/` | ariga__atlas.6d81150 | 73 | 2026-10-10 | 收尾修复生效（9/10 Submitted）但早收工致浅 |
| `glm-atlas-recursive-v1.4.4/` | ariga__atlas.6d81150 | 73 | 2026-10-10 | 组合完整方案；发现自测 fixture 代表性瓶颈 |
| `glm-atlas-recursive-v1.4.5/` | ariga__atlas.6d81150 | 77 | 2026-10-11 | fixture 多样化修复生效（cmdlog +43pp、datasources 32% 新高） |
| `glm-atlas-recursive-v1.4.5-generic/` | ariga__atlas.6d81150 | 84 | 2026-10-16 | prompt 泛化（通用四维度），可跨任务复用 |
| `glm-atlas-recursive-v1.4.6/` | ariga__atlas.6d81150 | **89** | 2026-10-16 | **checks 验收机制，历史最高** |
| `glm-atlas-recursive-v1.5.1/` | ariga__atlas.6d81150 | 86 | 2026-10-25 | 断言无上限（56→137 条）+ 隐藏命令/表达式语言探测 + 验证误杀修复 |

## 关键数字（glm-atlas-baseline-72）

- 有效测试 1318，官方分 72；全部测试 1732，通过 1180（68.1%）
- 耗时 2h36m，784 条消息（traj.json）
- 主要失败簇（修复优先级参考）：
  - `test_datasources` + `test_aws_gcp_datasources`：0/31，数据源模块整体缺失
  - `test_cmdlog_*`：约 4/95，cmdlog 功能基本未对上
  - `test_migrate_*` 系列：约 100/350，migrate 长尾
  - 基础命令（help/version/completion）：96%+，已对齐

## Run 画像

```bash
python3 methods/run_profile.py methods/<目录>/*/*.traj.json --scores 72,80   # 多 run 对比
```

单价（$/1M，GLM-5.3 默认渠道）写在脚本顶部：输入 1.1 / 输出 3.83 / 缓存输入 0.3。

## 对比方式

```bash
# 重新评测某个存档（分数可复现）
uv run programbench eval methods/<目录>
uv run programbench info methods/<目录>

# 失败聚类对比（按测试类名聚合）
python3 - <<'EOF'
import json, collections
from pathlib import Path
for run in ["methods/glm-atlas-baseline-72", "runs/<新run>"]:
    p = next(Path(run).glob("*.eval.json"))
    d = json.loads(p.read_text())
    c = collections.Counter()
    for t in d["test_results"]:
        if t["status"] != "passed":
            c[t["name"].split(".")[-2]] += 1
    print(f"== {run}: {sum(c.values())} failures")
    for k, v in c.most_common(15):
        print(f"{v:5d}  {k}")
EOF
```
