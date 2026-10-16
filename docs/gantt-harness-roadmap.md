# mini-swe-agent 框架优化 · 甘特图

> **规划来源**：`harness/`（mini-swe-agent 改造层，现状见 `harness/change_log.md`）
> **两条主线**：递归生成（probe→plan→implement→integrate 流水线）、Agent 自进化（跑分→归因→变异→重跑闭环），先后衔接不并行
> **优化目标**：ProgramBench 任务分数、生成速度、token 成本
> **周期**：2026-09（W3 起）~ 2026-12，W1–W4 按自然月划分（W1=1–7 日，W2=8–14 日，W3=15–21 日，W4=月末）

```mermaid
gantt
    title mini-swe-agent 框架优化（2026-09 ~ 2026-12）
    dateFormat YYYY-MM-DD
    axisFormat %m月%d日
    tickInterval 1week
    section 递归生成·流水线验证
    implement 阶段验证（17 模块并行）   :a1, 2026-09-15, 16d
    integrate 阶段验证（编译+全回归）   :a2, 2026-09-22, 9d
    atlas 端到端首跑 + baseline 对照    :a3, 2026-09-24, 7d
    section 递归生成·扩展优化
    probe 多 CLI 风格适配（flag/urfave） :b1, 2026-10-01, 14d
    plan/implement 模板与步数调优       :b2, 2026-10-08, 14d
    批量任务扩展与失败模式收集          :b3, 2026-10-15, 17d
    生成速度与 token 成本优化           :b4, 2026-11-01, 14d
    section Agent 自进化
    自进化机制设计（跑分→归因→变异→重跑） :c1, 2026-11-15, 16d
    自进化 v1 闭环验证（atlas）          :c2, 2026-12-01, 14d
    多轮进化迭代（分数/速度/成本）       :c3, 2026-12-08, 14d
    section 评测与收尾
    全量 ProgramBench 评测              :d1, 2026-12-22, 10d
    结果分析与技术报告                  :d2, 2026-12-22, 10d
```

> Mermaid 的时间轴是单行日期刻度，不支持“月份跨列合并 + W1–W4”双行表头；
> 需要精确表头（工作内容 / 9月 10月 11月 12月 / W3–W4 + W1–W4）的版本见同目录
> [gantt-harness-roadmap.pptx](gantt-harness-roadmap.pptx)（由 [gantt-planner](https://github.com/cinderzhan/gantt-planner) 渲染，
> 数据源 [gantt-harness-roadmap.json](gantt-harness-roadmap.json)，改一处重新 `node render_gantt.js` 即可再生成）。
