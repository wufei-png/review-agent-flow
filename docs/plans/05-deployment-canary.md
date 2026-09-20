# 05 · 部署与真实 canary

状态：阶段 1 本地完成；阶段 2 的真实 OpenCode Luna 正向链路、MR 写后恢复和隔离备份演练已执行，但 commit 写后恢复停在安全阻断，完整验收未通过，试运行判定为 no-go。优先级：试运行门槛。前置条件：[01](01-notification-and-outcome.md)、[02](02-retention-worker-ownership.md)、[03](03-operator-diagnostics.md)、[04](04-backup-restore.md) 均完成；尚需 commit 写后完整恢复、TLS webhook 入口、专用可续期 Provider 身份及实际 ReviewFlow 通知的人工可见确认。

## 交付结果

值班人员能按运行手册启停服务、检查告警、轮换凭据、恢复备份，并在外部写结果不明时按已持久化的阶段作出安全决定。新的 exact-SHA canary 证明最终 OpenCode 配置下的 Note、Auto-fix commit/child MR、Relation、通知服务端接受、人工可见确认、幂等重放及提交/MR 写边界恢复。试运行 go/no-go 只依据完成的现场证据。

## 当前边界与资料

- [A1 历史证据](../evidence/jihulab-opencode-a1-2026-08-10.md)只完成 Program-owned Note；成功 Note 早于最终 target-frozen Skill 和 OpenCode 配置，Auto-fix 与通知闭环未完成。该文档保留为历史，不作为新 canary 的验收收据。
- `docs/quickstart-host-native.md` 给出本机试用入口，`docs/deployment-linux-systemd.md` 和 `deploy/` 给出 reference unit；`docs/spec/05-integrations.md`、`06-runtime-and-storage.md` 定义发布、通知及恢复边界。
- Agent 的任务输入与 RESULT_PATH v1 已有契约。canary 使用当前最终配置验证实际能力；仅在具体失败证据指向现有约束时另行处理 Agent 提示词或工具权限。

## 阶段

1. **部署与故障操作手册** — 更新 reference deployment，增加可执行的 runbook：单 Worker 及三循环存活检查、队列和异常状态、关闭 Auto-fix/通知外部写入、Note/commit/MR 不确定结果核查、notification resend 的人工判断、rearm/abandon、凭据轮换、备份验证与停止服务后的恢复。列出新 canary 所需资源、最小身份权限、日志/收据字段和退出条件。`/readyz` 保持本地 Store readiness；Worker/provider 由独立状态与告警观察。只在真实服务验证后调整 systemd sandbox 指令。**依赖：** 01–04 完成。**验收：** 操作步骤与当前 CLI 参数相符；`systemd-analyze verify` 在可用主机通过；操作手册对每个不确定边界给出明确核查与升级路径；文档检查与相关测试通过。一个本地提交。
2. **新 exact-SHA 现场验证** — 使用与 A1 不同的新 MR head，保存 ReviewKey、frozen policy/Plan digest、Note ID、Auto-fix commit SHA、child MR IID、Relation、notification request generation/outbox 状态与脱敏 correlation。核对服务端接受后由 canary 收件人单独确认可见；重复入队和 Worker replay 不新增 Note/Auto-fix Relation/请求 generation，通知传输仍按至少一次契约允许重复。分别在 Auto-fix commit、MR 外部写边界之后中断并恢复，对账不重复创建远端 commit/MR；演练备份包在隔离环境的完整恢复。把成功与失败及环境版本记录到新的 `docs/evidence/` 文档，更新 implementation status。**依赖：** 阶段 1 与真实资源可用。**验收：** 逐项证据可追溯到同一精确 SHA、配置与实际远端身份；所有要求的正向与崩溃演练完成；失败时按 runbook 关闭相应外部写并保留现场。证据文档一个本地提交。

## 证据与试运行判定

本地 `ruff check .`、`pytest`、`python -m compileall -q src tests` 和 `git diff --check` 必须通过；OpenCode runtime isolation 测试在 canary 主机确认实际执行，不能把 skip 记作通过。真实通知分成队列已落库、服务端已接受、收件人确认可见三项记录。前两项仍不能推出已读。

若 provider 凭据、仅吴非可收的私信通道、独立 Project 或隔离恢复环境缺失，完成可独立验证的部分，保留阶段 2 为待验证并列出缺项；不要制造 live 结论或复用旧 Note。只在完成恢复演练、外部写对账和安全退出路径后记录试运行 go/no-go。此 canary 不向其他人私聊，也不向群聊发送消息。

完成记录写入[执行队列](pilot-readiness-queue.md)，并在本文记录提交、现场收据与未验证项。

## 完成记录

- 2026-09-21，阶段 1：`fc6d1a0` 新增 [单 Project 运维手册](../operations-runbook.md)，核对 reference 部署的 Worker 配置文件身份与三循环、状态、外部写和恢复路径；`30f52df` 按独立只读 review 修正 rearm/abandon 的活动 Store 路径，以及 resend/recover 的 Worker 身份、root-owned 环境文件与活动 Store 调用。两项 P2 均接受并修复，无拒绝项。`9added1` 修正既有 Worker 失租测试的链式异常竞态断言，审查者复核后无新增发现。
- 本地检查：最终 `.venv/bin/python -m pytest` 417 passed，1 条 Starlette/httpx 依赖弃用警告；此前一次复检为 416 passed、1 failed，失败的既有断言经修正后定向连跑 5 次及全量复跑通过。`.venv/bin/python -m pytest tests/test_opencode_runtime_isolation.py -rs` 1 passed（OpenCode `1.18.11`，仅本机）；`.venv/bin/python -m pytest tests/test_cli.py tests/test_deployment.py` 19 passed；`.venv/bin/ruff check .`、`.venv/bin/python -m compileall -q src tests`、相关 CLI `--help`、相对 Markdown 链接和 staged `git diff --check` 通过。
- 现场收据：无新收据。本机确认 JiHuLab CLI 已登录且存在模型凭据；未确认独立 canary Project、新通知 webhook 与人工收件人、Linux systemd 主机和隔离恢复环境。当前主机是 macOS，`systemd-analyze verify`、服务身份/sandbox 验证和 canary 主机上的 OpenCode isolation 尚未执行。
- 阶段 2 保持待现场验证：新的 exact-SHA ReviewKey、frozen Policy/Plan digest、Note ID、Auto-fix commit/child MR/Relation、通知服务端接受与人工可见、重复入队及 Worker replay、commit/MR 写边界中断恢复、完整备份包隔离恢复均未取得。未作试运行 go/no-go 判定；不复用 A1 Note。
- 2026-09-21，阶段 2 部分预检与阻塞记录见[新 canary 证据](../evidence/jihulab-opencode-canary-2026-09-21.md)：新私有 Project `373614`、MR `!1`、精确 head `8610a97e552468a78787aaba0d31f8817b54ca48` 和专用 Review Bot 已就绪；Linux systemd unit 验证、Worker 身份 OpenCode isolation、精确 SHA fetch、API `/readyz`、跨 Unix 身份 Store 写入与重启后访问已在主机执行。`3c68159` 增加仅吴非的飞书私信通道，`394579b` 修复通道类型重绑外发风险，独立只读 review 又发现同类型改绑其他 open ID 的 P1 并由 `f8c99f9` 修复，复审无新增发现；`6d56bb8` 的运行时 SQLite 权限修补在并发探测出现 Bus error 后由 `ce20858` 撤回并改为预创建共享数据库；`647a475` 修正启动顺序。单次吴非私信仅验证 lark-cli 服务端接受，不是 ReviewFlow 通知，人工可见待确认。
- 专用 Worker 模型 key 现场返回 `Incorrect API key`，飞书 app ID/secret 未配置；TLS webhook 入口、真实 Run/Note/Auto-fix/Relation/outbox、重复与崩溃恢复及完整隔离恢复均未验证。API/Worker 均停止且未启用，未作试运行 go/no-go 判定。
- 本轮本地全量 `.venv/bin/python -m pytest` 424 passed、1 条 Starlette/httpx 弃用警告；通知定向测试 20 passed；`.venv/bin/ruff check .`、`.venv/bin/python -m compileall -q src tests`、`git diff --check` 通过。真实主机 systemd 245 的 `systemd-analyze verify` 退出码为 0，但提示 `ProtectProc` 和 `ProcSubset` 未生效；这些检查不能代替真实 MR 发布与恢复验收。
- 2026-09-21，按吴非确认继续使用 OpenCode、指定 `openai/gpt-5.6-luna`：`52ad655` 加入逐 Run 私有 OAuth 启动器及入口；`c1b8b8a` 在现场隔离探针发现内置 OpenAI OAuth 插件被总开关禁用后，只在该启动器恢复内置插件。Worker 身份的实际 Luna `run --pure` 探针返回 `OK`，源码与主机安装文件 SHA-256 一致。现用从已有主机配置提取的单一 OpenAI OAuth 记录；这不是专用 Provider 账号，Run 内刷新不会回写稳定来源。现有飞书应用凭据已安全接入唯一 `feishu-owner-dm` 通道；ReviewFlow 适配器给吴非的单次测试私信返回服务端接受。此前 lark-cli 私信由吴非确认可见；实际 outbox 通知可见仍待单独确认。
- 新[现场证据](../evidence/jihulab-opencode-canary-2026-09-21.md)：Run `1` 的 exact head `8610a97e552468a78787aaba0d31f8817b54ca48` 产生 Note `9207699`、Plan digest `a58faebde9fb730b437af64a03ea2aeadfb89ef05243ff8867bcf3f32fd39add`、修复 commit `85fcd9c9640de3e4dc90a0a0d68eb4b4eb49d327`、子 MR `!2`、Relation `1` 和 generation `1` 的飞书服务端接受收据。重复入队返回 `existing`，Worker 重启后没有第二个 Note、Relation、请求 generation 或子 MR。父 head 的单测失败 `20 != 15`，修复分支单测通过。
- 两个独立故障窗口：Run `3` 在 GitLab 返回 commit `7a3fee17e22f86f6ef6462688c18c23761831551` 后、Store 写入 SHA 前退出 `137`；重启没有重发 commit，Plan `2` 因身份未持久化而 `blocked/operator_check`。核对唯一远端分支、父 SHA、变更路径和 Plan 文件内容摘要后，先备份再以审计 action `a72bffe4a76c4e94b1c788d0ae98ed62` 放弃 Plan；该项只证明安全退出，不满足完整恢复。Run `4` 在 GitLab 返回子 MR `!3` 后、本地写 IID 前退出 `137`；正常 Worker 对账已有 MR，落库 Relation `2` 和一次 generation `1` 私信请求，未创建第二个 MR。
- 真实 Store 的一致性 bundle 经验证并在私有 Linux mount namespace 内恢复到原绝对路径；schema 17、Run/Note/Relation/outbox 收据与摘要一致。真实 Plan 尚未到保留期，正式 bundle 的 Plan 数为 0；仅在隔离副本上提前物化一个 Plan 备份后，含一个 canonical Plan 文件的第二 bundle、绝对收据路径、权限及摘要均通过恢复检查。原 Store 的 API `/readyz` 与 Worker 心跳在演练后通过；未在独立恢复主机或恢复副本上启动 systemd 服务。
- 本轮最终本地全量 `.venv/bin/python -m pytest` 427 passed、1 条 Starlette/httpx 弃用警告；OpenCode 启动器与隔离定向 4 passed；Ruff、compileall、`git diff --check` 通过。`delegated-change-review` 的全新只读审查对 `0df06cc..1c45d02` 无 P0–P3 发现；审查者未独立重放现场收据。最终 `ops-status` 无 pending/blocked/uncertain 队列，API/Worker 均停止且 disabled。TLS webhook、实际 outbox 的人工可见性、专用可续期 Provider 身份和 commit 写后继续完成发布尚缺证据；不得把安全 abandon 或飞书服务端接受记作通过，试运行 no-go。
