# ReviewFlow 试运行准备执行队列

状态：实施中；01–04 已完成本地实现与检查，05 的故障手册、真实 OpenCode Luna 正向 canary、MR 写后恢复、隔离备份演练和实际通知可见性已有证据；commit 写后完整恢复未通过，试运行判定 no-go。仍待 TLS webhook 和专用可续期 Provider 身份。规划基线：2026-09-20，`f407d0b25583a9553cbac24741183e87457d1523`。每次实施先核对当前提交、工作区和实际代码；本文的基线不是跳过核对的理由。

## 目标与契约

本队列收口单 Project、单 Worker 的有限生产试运行准备。完成代码和本地检查后，还需新的 JiHuLab/OpenCode、通知渠道及恢复演练证据才能作出试运行判定。既有 [A1 证据](../evidence/jihulab-opencode-a1-2026-08-10.md)只证明精确 SHA 评审、Program-owned Note、重复入队幂等及部分租约恢复。

Agent 仍从冻结的目标分支 Skill 与 `AgentTaskV1` 获取任务，只向 `RESULT_PATH` 写入现有 v1 结果；ReviewFlow 持有 GitLab 发布、通知和恢复权限。本队列不扩展 Agent 结果字段或增加提示词、工具限制。只有真实 canary 证明现有约束妨碍评审或修复时，才针对该证据另行调整。

通知沿用 [ADR-0010](../adr/0010-notification-outbox.md) 的 outbox、有限次自动重试和至少一次投递。持久状态 `delivered` 保持兼容，其含义是飞书或企微服务端以 HTTP 和业务响应接受了请求；收件人可见须另有人工证据，崩溃窗口可能重复发送。不新增发送不确定状态机或自动停止重试规则。

当前 SQLite schema 为 17；本项目现阶段没有旧数据库在线迁移契约。试运行后的升级必须先保存需保留的 canary 证据和一致性数据库快照，不把删库当成升级方法。不可变 Plan、精确 SHA、外部写阶段和显式恢复继续是发布契约。

## 建议取舍

| 评估项 | 决策 | 落点 |
| --- | --- | --- |
| Auto-fix 与 resend 通知不可领取 | 修复确定缺陷并验证实际 claim | 01 |
| 通知发送后崩溃可能重复 | 保留至少一次与有限自动重试；明确“服务端已接受，可能重复”的证据范围 | 01；不增加发送不确定状态机 |
| Run 租约过期、`superseded` 结果误判 | 修正 fencing 与结果投影 | 01 |
| 通知保留期、单 Worker 所有权 | 一次 schema 切换完成终态时间与运行时租约 | 02 |
| 状态、队列与 Worker 诊断 | 扩展只读 CLI，使用已有持久事实；暂不建立独立指标服务或固定流量限制 | 03 |
| DB 与 Plan 恢复集合 | 增加一致性快照、manifest、离线验证及恢复演练 | 04 |
| 运维手册与真实验收 | 核对当前部署契约并用新 exact SHA 取得正向及崩溃恢复证据 | 05 |

## 执行顺序

| 顺序 | 优先级 | 独立会话 | 前置条件 | 状态 |
| --- | --- | --- | --- | --- |
| 01 | P0 | [通知可领取与结果语义](01-notification-and-outcome.md) | 无 | 本地完成；现场证据待 05 |
| 02 | P1 | [终态保留与 Worker 所有权](02-retention-worker-ownership.md) | 01 完成；schema 切换前保存证据 | 本地完成；现场证据待 05 |
| 03 | P1 | [Operator 诊断](03-operator-diagnostics.md) | 02 完成 | 本地完成；现场证据待 05 |
| 04 | P1 | [一致性备份与恢复验证](04-backup-restore.md) | 02 完成；建议 03 完成以复用状态查询 | 本地完成；现场证据待 05 |
| 05 | 发布门槛 | [部署与真实 canary](05-deployment-canary.md) | 01–04 完成；取得独立 canary 资源 | 阶段 1 完成；阶段 2 正向 Run、MR 写后恢复和隔离备份已验证，commit 写后只能安全阻断；no-go，验收未完成 |

每项任务由一个新会话完成，并依照该任务的阶段顺序用 `implement-in-stages` 逐阶段验证、本地提交。每个阶段都须留下有效仓库状态；提交前只暂存该阶段文件并检查 staged diff。完成一个会话后更新本队列及对应计划的状态、提交与检查结果。若代码事实推翻计划，只调整未开始的阶段，并记录理由。不要把本地测试、模拟 GitLab 或配置检查记成真实平台验收。

新会话只需读本队列、所选任务计划、仓库 `AGENTS.md`、`CONTEXT.md` 及计划点名的当前源码和规格；后续任务计划不要求预读。当前代码与规格决定具体实施方式。

## 留待后续证据的事项

独立指标服务、固定 admission 阈值、分布式 tracing、通知静默与升级策略、凭据热加载及额外 systemd sandbox 指令，需要容量或运行证据再设计。队列中的只读队列年龄和状态汇总可供初期告警使用；试运行目标值由真实负载和恢复演练校准。

## 完成记录

- 2026-09-20，01 本地完成：`f85aaa8`（通知领取）、`b3e4381`（Run 租约 fencing）、`bb67f5c`（结果投影和措辞）、`7e5e6ac`（review 后修正 Worker 返回状态）。独立只读 review 的一项 P2 已核实并修复；无其他发现。
- `.venv/bin/python -m pytest`：380 passed，1 条 Starlette/httpx 测试依赖弃用警告；`.venv/bin/ruff check .`、`.venv/bin/python -m compileall -q src tests`、`git diff --check` 通过。测试中的 GitLab 与通知服务为模拟；新 JiHuLab/OpenCode、飞书/企微服务端接受、收件人可见及崩溃恢复现场证据仍由 05 收集。
- 2026-09-20，02 本地完成：`b449419`（schema 17 与通知终态保留）、`dc442dc`（单 Worker 运行时所有权）、`77aa9e2`（审查后修复 `--once` 通知批次失租）、`8f54e04`（审查后修复退出时失租）。新的只读 review 提出的 P1、P2 均核实并逐项修复，无拒绝项。
- `.venv/bin/python -m pytest`：392 passed，1 条 Starlette/httpx 测试依赖弃用警告；`.venv/bin/ruff check .`、`.venv/bin/python -m compileall -q src tests`、`git diff --check` 通过。测试验证新建库为 schema 17、旧 schema 16 拒绝，以及租约并发、过期接管、fencing、释放和长任务 heartbeat。未在真实部署上执行 schema 切换或取得一致性数据库快照；旧 canary 证据保留在 `docs/evidence/`，切换前的现场快照和新 canary、通知及恢复证据仍由 05 收集。
- 2026-09-20，03 本地完成：`cb42a79`（单 Run 效果诊断）、`fd4ca73`（只读 `ops-status` 汇总）、`0c6484a`（审查后修正已发布 Note 和进行中 Auto-fix 的行动提示）。独立只读 review 的两项 P2 均核实并修复，无拒绝项。
- `.venv/bin/python -m pytest`：398 passed，1 条 Starlette/httpx 测试依赖弃用警告；相关四个测试文件、`.venv/bin/ruff check .`、`.venv/bin/python -m compileall -q src tests`、CLI `--help`、`git diff --check` 通过。诊断仅读取 Store 的已知事实；未核查远端 Note/MR、收件人可见性、真实 Worker 部署或告警阈值。
- 2026-09-21，04 本地完成：`39f643b`（一致性 Store/Plan 备份包）、`8f5680e`（离线验包和恢复手册）、`cad3ac2`（独立只读审查后修正首次无 Plan 收据、并发目标覆盖及发布后 fsync 失败）。审查的两项 P2、一项 P3 均核实并修复，无拒绝项。
- `.venv/bin/python -m pytest`：417 passed，1 条 Starlette/httpx 测试依赖弃用警告；备份相关针对性测试、`.venv/bin/ruff check .`、`.venv/bin/python -m compileall -q src tests`、CLI `--help`、`git diff --check` 通过。临时 Store 的 WAL 并发快照、损坏拒绝和空路径恢复演练验证了只读查询及绝对收据路径；未在 Linux systemd 部署、独立备份介质或真实服务身份下演练，05 仍需现场恢复和新 canary 证据。
- 2026-09-21，05 阶段 1 本地完成：`fc6d1a0`（部署故障手册及 reference 权限核对）、`30f52df`（独立只读审查后补齐 Operator 命令的活动 Store 与安全凭据环境）、`9added1`（修正既有 Worker 失租测试的链式异常竞态断言）。审查的两项 P2 均核实并修复，无拒绝项；审查者复核测试修正无新增发现。阶段 2 未实施，试运行 go/no-go 尚无依据。
- 本地最终 `.venv/bin/python -m pytest` 417 passed，1 条 Starlette/httpx 弃用警告；期间一次复检为 416 passed、1 failed，定位为已有 Worker 失租测试只接受顶层异常文本，改为检查顶层及链式根因后定向连跑 5 次和全量复跑通过。OpenCode `1.18.11` 在本机的 runtime-isolation 定向测试为 1 passed；CLI/部署定向测试 19 passed，`.venv/bin/ruff check .`、compileall、CLI `--help`、相对 Markdown 链接检查、staged `git diff --check` 通过。当前主机为 macOS，未运行 `systemd-analyze verify` 或 Linux 服务演练。虽本机有 JiHuLab CLI 登录与模型凭据，独立 canary Project、新通知 webhook/人工收件人、Linux systemd 主机和隔离恢复环境尚未确认；没有新的 exact-SHA 远端 Note、commit/MR、Relation、通知或恢复收据。旧 A1 证据不计入 05 验收。
- 2026-09-21，05 阶段 2 部分预检：新增仅吴非的 `feishu_dm`、通道重绑防外发、私有 Project/MR 与专用 Review Bot；主机完成 systemd unit 验证、OpenCode isolation、精确 SHA fetch、API readiness 与跨 Unix 身份 Store 写入。提交 `3c68159`、`394579b`、`6d56bb8`、`647a475`、`ce20858`、`f8c99f9`；其中 `6d56bb8` 在 Bus error 现场探测后由 `ce20858` 撤回。独立只读 review 的另一项 P1（同 ID 私信改绑其他 open ID）由 `f8c99f9` 修复并定向复测，复审无新增发现。单次 owner-only lark-cli 私信仅有服务端接受，未确认可见。当前 Worker 模型 key 无效，飞书 app 凭据缺失；两服务已停止且未启用。逐项收据、现场失败与未验证项见[新 canary 证据](../evidence/jihulab-opencode-canary-2026-09-21.md)。真实 ReviewFlow Run、外部写和完整恢复仍未执行，未作 go/no-go 判定。
- 本轮本地全量 `.venv/bin/python -m pytest` 424 passed、1 条 Starlette/httpx 弃用警告；通知定向测试 20 passed；`.venv/bin/ruff check .`、`.venv/bin/python -m compileall -q src tests`、`git diff --check` 通过。`systemd-analyze verify` 在主机退出码为 0，systemd 245 提示两项 sandbox 指令未生效；仅属 unit 语法检查，未计为完整运行或发布验收。
- 2026-09-21，05 阶段 2 现场推进：`52ad655`（Worker 私有 OpenCode OAuth 启动器）、`c1b8b8a`（保留内置 OAuth 插件）；主机 Worker 身份 OpenCode `openai/gpt-5.6-luna` 探针返回 `OK`。Run `1` 完成 Note `9207699`、Auto-fix commit `85fcd9c9640de3e4dc90a0a0d68eb4b4eb49d327`、子 MR `!2`、Relation `1`、飞书 owner-only outbox 服务端接受；重复入队和 Worker 重启无重复效果。Run `4` 在子 MR 写后退出 `137`，正常 Worker 对账 `!3` 并只生成一次 Relation/通知请求。Run `3` 在 commit 写后退出 `137`，远端唯一 commit 与 Plan 一致且未重写，但因 SHA 未持久化被安全阻断；备份后审计式 abandon，未完成发布。真实 Store bundle 和隔离副本上的含 canonical Plan bundle 均通过隔离恢复；原 Store 的 API/Worker 重启健康。详见[本计划](05-deployment-canary.md)与[现场收据](../evidence/jihulab-opencode-canary-2026-09-21.md)。
- 最终本地全量 `.venv/bin/python -m pytest` 427 passed、1 条 Starlette/httpx 弃用警告；OpenCode 定向 4 passed；Ruff、compileall、`git diff --check` 通过。`delegated-change-review` 的全新只读审查对 `0df06cc..1c45d02` 无 P0–P3 发现，未独立重放现场收据。当前使用可用的个人 OpenCode OAuth 副本，不是专用可续期 Provider 身份；TLS webhook 和 commit 写后继续完成发布亦未通过。05 保持未完成，试运行 no-go，两个 systemd 服务已停止并 disabled。
- 吴非提供的个人飞书会话截图确认 ReviewFlow outbox 中子 MR `!2`、`!3` 的两条通知实际可见，另显示此前的通道测试和适配器探针。原图只保留在私有本机证据目录，摘要及证据边界见[现场收据](../evidence/jihulab-opencode-canary-2026-09-21.md)。实际通知人工可见性待验项已关闭；截图不证明阅读时间，也不改变 05 未完成及试运行 no-go 判定。
