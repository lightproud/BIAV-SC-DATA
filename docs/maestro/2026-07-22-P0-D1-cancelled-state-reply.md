# 银芯答复：Maestro 用户主动取消的封闭终局语义（P0-D1）

- 答复方：银芯（silver-core-maestro-sdk 上游维护方）
- 接收方：BPT（Black Pool Terminal）
- 日期：2026-07-22
- 针对来文：《Maestro 需求说明书：用户主动取消的封闭终局语义（P0-D1）》（2026-07-22）
- 基线版本：`silver-core-maestro-sdk 0.75.0`
- 目标版本：**`silver-core-maestro-sdk 0.76.0`**（与 `silver-core-agent-sdk 0.76.0` 锁步发布）

---

## 1. 总体裁定：接受

需求整体接受，不反驳 API 形状。来文第 6.3 节邀请的等价提案（「取消建模为独立 `abort` 事件而非新终态」）经评估后**放弃**：abort-as-event 要满足「取消后永不自动重跑」终归需要一个终态标记来阻断 `claimDue()` / retry 调度，绕一圈仍会收敛到新终态；不如直接按来文 4.1–4.3 落地 `cancelled` 终态 + `cancel` 事件 + `cancelSession()`。

两条不可让渡的语义底线确认满足：

1. **用户取消与失败在台账层面严格可区分**——session 级 `cancelled` 与 `failed` 平级分立；query 级 `'cancelled'` outcome 与 `'error'` 分立（见 2.1）。
2. **取消后永不自动重跑**——`cancelled` 为无出边终态，`nextRunAt` 清 null，`claimDue()` / `sweepExpiredLeases()` / 宿主重启恢复均不触碰（见 2.2、第 4 节用例 1/2/6/7）。

## 2. 关键口径裁定

### 2.1 来文 4.4 二选一：**选方案 A —— 扩展 `QueryOutcome`**

```ts
export type QueryOutcome = 'ok' | 'error' | 'timeout' | 'cancelled';
```

理由：

- query 级历史保持完整、自描述，BPT Shadow 收敛不需要为「session cancelled 但最后一行 query 是 error/缺行」做特判口径；
- 与 session 级对称：`SessionState` 与 `QueryOutcome` 的终局分类一一对应，审计/统计逐层一致；
- 对旧读方是纯增量联合扩展，与 4.6 兼容承诺一致。

落账细则：

- 从 `running` 取消：为在飞 attempt 追加一行 `QueryRecord`，`attempt` 取当前在飞 attempt 号，`outcome: 'cancelled'`，`error` 字段承载 `opts.reason`（未提供则为 null）。**不递增 attempts 计数语义**——该行记录的是「这次 attempt 被中止」，不是一次新 attempt。
- 从 `pending` / `retrying` 取消：无在飞 attempt，**不追加 query 行**（不伪造 attempt 号污染计数）。
- 为使任意状态下取消的 reason / 时刻都有 session 级落点，`SessionRecord` 新增两个可选字段（纯增量，旧记录读出为 undefined，旧宿主忽略未知字段）：

```ts
interface SessionRecord {
  // ... 既有字段不变 ...
  /** 仅 state === 'cancelled' 时有值；Epoch ms */
  cancelledAt?: number | null;
  /** cancelSession(opts.reason) 原样透传 */
  cancelReason?: string | null;
}
```

`lastError` **不**挪用于承载取消原因（保持其「最近一次 error/timeout 摘要」的既有语义不被污染）。BPT Shadow 收敛请以 `cancelReason` / query 行 `error` 字段为口径。

### 2.2 来文 4.5：lease / sweep 交互

- `cancelSession()` 落 `cancelled` 时无条件置 `leaseUntil = null`、`nextRunAt = null`（三个来源状态一致处理）。
- `sweepExpiredLeases()` 的筛选谓词在实现中**显式**写为 `state === 'running' && leaseUntil !== null && leaseUntil <= now`——`cancelled` 天然不命中，且即使宿主持有取消前读出的陈旧快照，CAS 围栏（见 2.3）也会使 sweep 的写入失败。配专门测试（第 4 节用例 6）。

### 2.3 补充裁定：与在飞 attempt 的竞态（来文未列，实现必答）

`cancelSession()` 与在飞 attempt 的 `recordOutcome()` 可能并发到达。裁定如下：

- 两者同走既有并发模型：per-session 进程内互斥串行化，跨进程经 `putSessionIf` CAS 围栏，先获锁/先 CAS 成功者胜。
- **cancel 先落账**：后到的 `recordOutcome()` 面对终态，与既有闭包一致抛 `InvalidTransitionError`。台账层保持严格，不为迟到结果开静默口子；**driver 层**捕获该错误并丢弃迟到结果（降级为 debug 日志），不 crash、不重试。此行为随 0.76.0 一并合入 driver。
- **attempt 结果先落账**：session 已进入 `done` / `failed` / `retrying`。对 `done` / `failed` 的后续 `cancelSession()` 抛 `InvalidTransitionError`（来文 4.3 既定）；对 `retrying` 则正常取消。宿主对「取消按钮 vs 恰好完成」的这条竞态窗口无需特殊处理。
- `cancelSession()` 只写台账，**不主动 abort** driver 在飞 executor 的 `AbortSignal`。宿主（BPT `stopLoop` 路径）应自行中止运行时（既有 `controller.stop()`），两个动作顺序任意——上述竞态裁定保证任一交错都收敛到 `cancelled` 或合法终态。driver 侧「cancel 感知联动 abort」列为 0.77 候选，不阻塞本票。

## 3. 最终 API 契约（0.76.0）

### 3.1 类型

```ts
// ledger/types.d.ts
export type SessionState =
  | 'pending' | 'running' | 'retrying'
  | 'failed' | 'done'
  | 'cancelled';                       // 0.76.0 新增：用户主动取消，终态，无出边

export type QueryOutcome = 'ok' | 'error' | 'timeout' | 'cancelled';  // 0.76.0 新增 'cancelled'

// 生命周期序追加，cancelled 排终态区末尾，不扰动既有索引序
export declare const SESSION_STATES: readonly ['pending', 'running', 'retrying', 'failed', 'done', 'cancelled'];
export declare const TERMINAL_STATES: ReadonlySet<SessionState>;      // {'failed', 'done', 'cancelled'}
```

### 3.2 状态机

```ts
// ledger/state.d.ts
export type SessionEvent =
  | 'claim' | 'attempt:ok' | 'attempt:error' | 'attempt:timeout'
  | 'cancel';                          // 0.76.0 新增
```

转移闭包（既有五条不变，新增三条）：

```
pending  --claim-->            running
retrying --claim-->            running
running  --attempt:ok-->       done
running  --attempt:error-->    retrying | failed   (attempts vs maxAttempts)
running  --attempt:timeout-->  retrying | failed   (attempts vs maxAttempts)
pending  --cancel-->           cancelled            // 0.76.0
running  --cancel-->           cancelled            // 0.76.0
retrying --cancel-->           cancelled            // 0.76.0
```

`cancelled` 无出边；对终态（含 `cancelled` 自身）投递任何事件均为 `InvalidTransitionError`，幂等豁免仅限 3.3 所述 `cancelSession()` 重复调用。

### 3.3 `TaskLedger.cancelSession()`

签名照单全收（来文 4.3 原样）：

```ts
cancelSession(sessionId: string, opts?: {
  /** 取消原因，宿主可填 'user' | 'operator' | 'superseded' 等，原样入台账 */
  reason?: string;
  /** Epoch ms，取消生效时刻；缺省取注入 clock 的 now() */
  cancelledAt?: number;
}): Promise<SessionRecord>;
```

行为表：

| 调用时状态 | 结果 |
|---|---|
| `pending` | → `cancelled`；`nextRunAt = null`；不追加 query 行 |
| `retrying` | → `cancelled`；`nextRunAt = null`；不追加 query 行 |
| `running` | → `cancelled`；`nextRunAt = null`、`leaseUntil = null`；追加一行 query（`outcome: 'cancelled'`，attempt 取在飞号，`error = reason ?? null`） |
| `cancelled` | 幂等：返回现状记录；不抛错、不追加 query 行、不覆写首次的 `cancelledAt` / `cancelReason` |
| `done` / `failed` | 抛 `InvalidTransitionError` |
| 不存在的 sessionId | 抛与既有 `claimSession` 同款的 not-found 错误（保持错误族一致） |

并发：per-session 进程内互斥 + `putSessionIf` 跨进程 CAS，CAS 失败按既有 audit r4 模型重读-重验-重试；重读后若已是 `cancelled` 则走幂等分支，若已是 `done`/`failed` 则抛 `InvalidTransitionError`。

## 4. 验收标准 → 上游测试映射

BPT 验收用例 1–8 全部转为上游测试，随 0.76.0 合入：

| # | BPT 验收用例 | 上游测试归属 |
|---|---|---|
| 1 | `pending` 取消 → `cancelled`、`nextRunAt=null`、`claimDue()` 不再列出 | ledger 单测 + store 契约 |
| 2 | `retrying` 取消 → 到点不重跑 | ledger 单测（注入 clock 快进） |
| 3 | `running` 取消 → query 行落 `'cancelled'`、`leaseUntil=null` | ledger 单测 |
| 4 | 重复取消幂等、不增 query 行 | ledger 单测 + store 契约 |
| 5 | `done`/`failed` 取消 → `InvalidTransitionError` | 状态机单测 |
| 6 | 取消后 `sweepExpiredLeases()` 不受影响 | ledger 单测（构造已过期 lease 快照） |
| 7 | 重启重载后仍 `cancelled`、不复活 | store 契约（持久化往返） |
| 8 | `runLedgerStoreContractSuite` 全量回归（含既有 15 项 CAS 契约） | 契约套件，零改动通过 |

契约套件另**新增**用例：cancel 的 CAS 冲突重试路径、cancel 与 recordOutcome 交错的双向竞态（2.3 两分支）、`TERMINAL_STATES` 含 `cancelled` 的枚举完备性。

## 5. 兼容性与发布

- 纯增量：既有五态转移、`recordOutcome` / `claimDue` / `claimSession` 签名与行为零改动（driver 对迟到结果的 catch 是 driver 内部行为收敛，不改公开签名）。
- `SESSION_STATES` 按生命周期序追加于末尾（3.1），依赖索引序的下游不受扰动。
- 旧宿主读到 `cancelled` 台账按未知终态处理，与来文 4.6 认知一致。
- changelog 与类型注释将显式载明：2.1 的 QueryOutcome 扩展口径、2.2 的 lease 交互、2.3 的竞态裁定（本答复第 2 节全文进 changelog 附录）。

## 6. 排期

工作量评估约 3 人日：类型与状态机 0.5d、`cancelSession()` 与并发路径 1d、测试（含契约套件扩充）1d、changelog 与锁步发布 0.5d。

- **2026-07-24**：`0.76.0-rc.1` 可供 BPT Shadow 环境预验收（验收用例 1–8）。
- **2026-07-28**：`silver-core-maestro-sdk 0.76.0` + `silver-core-agent-sdk 0.76.0` 正式锁步发布。

若 BPT 对 2.1 的 `SessionRecord` 增量字段（`cancelledAt` / `cancelReason`）或 2.3 的竞态口径有异议，请在 rc 窗口内回文；语义底线两条不受任何后续调整影响。
