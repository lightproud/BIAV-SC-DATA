# 疑问清单

推进顺序与设计收口见 `docs/progress/roadmap.md`（2026-10-03）。

不阻塞施工的待定事项。每条附建议；守密人裁定后改写成结论，或移除。

## Q1 CLAUDE.md 第 10 条引用了已废除的 §1.1-HC（2026-10-03）
- 现状：守密人 2026-09-28 已整体废除 §1.1-HC 黑池防火墙（AGENTS.md）；本档按守密人指示「方案原文一字不改」照抄。
- 实际执行：第 10 条的落地要求（内网地址、密钥、运营数据、裁定内容不进仓库，只经环境变量与外部文件注入）
  与 AGENTS.md 现存的安全底线「凭据不入库」一致，照常执行；只是条款名指向的章节已不存在。
- 建议：之后把条款名改成「凭据与运营数据不入库」，内容不变。

## Q2 「每张工单一个提交」与本仓 squash 合并（2026-10-03）
- 现状：AGENTS.md §7.6 规定 PR squash 合并进 main，合并后一个阶段只剩一个提交，checklist 里写的工单提交号在 main 上查不到。
- 建议：分支上照方案逐工单提交；PR 合并后，checklist 的提交号改记 main 上的 squash 提交号，同时在 PR 里保留逐工单提交。
  也可以改用 rebase 合并，这就需要守密人对本子项目另行裁定。

## Q3 数据湖在哪跑普查（2026-10-03，已决）
- 结论：守密人 2026-10-03 裁定子项目整体迁入 BIAV-SC-DATA（理由：采集代码与数据都在这里）。
  `yuqing census` 不设 LAKE_ROOT 时默认读本仓 `Record/Community`；读取复用本仓 `archive_layout.open_archive_text`。
- 仍需：在有完整数据的检出上跑一次普查（DATA_ROOT 设在仓库外），报告交守密人过 G0。

## Q4 决策模型作候选判断后端（2026-10-03，守密人：加候选，未来再判）
- 内容：Jev（TypeSafe AI）/ OpenAI Decisions API 只答有限选项题，便宜约 30 倍、单次约 0.2 秒、格式零出错；准确率中档，中文、日文、韩文无公开数据，开箱置信度虚高。
- 建议：T20 留一个后端位；G2 时在金标集上比「单元有无反馈」的召回率（按中、日、英分开），比赢再在 T22 前加预筛。主标注不动。
- 依据与挂账：BIAV-SC-CODE `Public-Info-Pool/Resource/proposal/decision-model-candidates-20261003.md`、`memory/todo.md` T107。

## Q5 「临时分析」工单提案（2026-10-03，守密人：加候选，未来再判）
- 内容：一条命令输入题目、选项、范围 → 先出 50–100 条抽验页（校准阈值）→ 过关后对全量单元运行 → 单文件 HTML 报告（命中数、独立发言人、代表原文加 raw_ref）。
- 前提：第 1 阶段（T10–T12）完成；不依赖第 2 阶段。
- 建议：第 1 阶段做完后再提请；属方案范围外，需守密人批。挂账 BIAV-SC-CODE `memory/todo.md` T108。

## Q6 T20 / T21 施工中的取舍（2026-10-03，不阻塞）
- `runs` 台账落 `DATA_ROOT/runs/` 的 JSONL（逐次调用行 + 运行汇总行 + 收尾报告），未用 Parquet：台账小、只追加，JSONL 零依赖。建议：T30 查询层需要时由 DuckDB 直接读 JSONL。
- `yuqing llm ping` 同样受预算约束：人工验收时需临时设 `YUQING_BUDGET_TOKENS_PER_RUN` 与 `YUQING_BUDGET_TOKENS_PER_DAY`（各几百即可）。
- 单元内任一反馈点不合契约即整单元判无效、重发一次（不只丢那个点）。理由：半截结果混进库里更难查。建议 G2 时看 failed 率，过高再放宽。
- `self_identity`、`self_intent` 按词表枚举校验（样例里各 6、7 项）；真实词表随 `YUQING_CONFIG_DIR` 注入时由人定。
