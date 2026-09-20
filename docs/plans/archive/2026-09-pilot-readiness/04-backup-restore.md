# 04 · 一致性备份与恢复验证

> 历史归档：本地实施已完成；现场验收由[05 当前计划](../../05-deployment-canary.md)跟踪。本文保留当时的任务与检查记录，当前状态见[执行队列](../../pilot-readiness-queue.md)。

状态：本地完成；现场恢复证据待 05。优先级：P1。前置条件：[02 · 终态保留与 Worker 所有权](02-retention-worker-ownership.md) 完成；[03 · Operator 诊断](03-operator-diagnostics.md) 已完成。

## 交付结果

Operator 可生成一份包含一致性 SQLite 快照、收据引用的不可变 Plan 文件和校验清单的私有备份包；可离线验证缺失、篡改及权限错误。恢复流程先在空目录验证完整性，再启动服务。备份与恢复演练留下可复核的版本、时间及检查结果，不把凭据或 Review 内容写进公开证据。

## 实施前契约与修改入口

- `retention.py::RetentionManager` 已把可清理的规范 Plan JSON 保存成 `<plan_sha256>.json`，验证 0700 目录、0600 文件、owner、digest、fsync，再由 `store.py::record_publication_plan_backup()` 记不可变收据。收据中的 `backup_path` 是绝对路径。
- `docs/adr/0024-retention-and-systemd-runtime.md` 和 `docs/deployment-linux-systemd.md` 把 SQLite 与 Plan backup 目录定义为同一 recovery set。直接复制运行中 WAL 模式的主 DB 文件不提供一致性快照。
- `cli.py` 现有恢复命令只处理 Store 丢失后的单条 Auto-fix Relation，不承担整个 Store 备份。`tests/test_retention.py` 证明 Plan 文件备份 primitive；尚无 DB+Plan bundle 的完整验证。

## 阶段

1. **生成一致性备份包** — 新增独立备份模块和 `backup-store` CLI。使用 SQLite backup API 取得静态 DB 快照，再从该快照读取 backup receipts，只复制其引用的规范 Plan 文件；验证路径归属、普通文件、权限、长度、SHA-256 与 Plan digest。manifest 使用相对包内路径记录应用/schema 版本、快照时间、文件 digest/bytes 及收据关系；临时目录完成校验与 fsync 后原子发布备份包。Plan payload 仍在 DB 的记录由快照覆盖；源 Store 与源备份文件不修改。**依赖：** 02 完成。**验收：** 并发 WAL 写入下可读取快照；缺失、篡改、符号链接或不安全权限使备份失败且不留下伪完整包；重名目标不覆盖；针对性测试通过。一个本地提交。
2. **离线验证与恢复演练** — 新 `verify-backup` CLI 检查 manifest、SQLite integrity/schema、收据到 Plan 文件的完整映射及 digest；不访问 GitLab。给出停止 API/Worker、验证包、恢复 SQLite 和 Plan 文件、重新验证 owner/mode、只读 `status`/`ops-status`、再启动服务的明确操作手册。现有绝对 `backup_path` 收据在真正恢复后仍须指向有效的部署路径；在隔离主机或等价环境重建该路径，不悄悄重写不可变收据。用空目录及故意缺文件、改 digest、错误 mode 的案例演练，记录可恢复的 ReviewKey/Policy Snapshot/Plan/Relation/notification/audit 抽样。**依赖：** 阶段 1。**验收：** 完整包校验成功，损坏包 fail closed；空目录恢复后只读查询与收据核对成功；当前主 Store 不被演练改写；全套检查通过。一个本地提交。

## 验证与交接

逐阶段跑 `pytest -q tests/test_retention.py tests/test_store.py tests/test_cli.py tests/test_backup.py`（新文件依实施命名）；结束跑 `ruff check .`、`pytest`、`python -m compileall -q src tests`、`git diff --check`。本机临时目录演练证明工具契约，不代替新服务身份、真实备份介质与 systemd 停启演练。完整试运行恢复证据在任务 05 记录。

完成记录写入[执行队列](../../pilot-readiness-queue.md)，并在本文记录提交、校验结果和仍待现场验证的条件。

## 完成记录

- 2026-09-21：`39f643b` 增加 `backup-store`、SQLite backup API 快照、快照收据对应的私有 Plan 包、manifest 和原子发布；`8f5680e` 增加 `verify-backup`、[恢复手册](../../../backup-restore.md)、规格说明和空目录演练；`cad3ac2` 修复独立只读审查提出的首次无收据时 Plan 目录尚不存在、并发同名目标覆盖、发布后 fsync 失败掩盖原错误三项问题（两项 P2、一项 P3；无拒绝项）。
- 阶段针对性检查 `.venv/bin/python -m pytest -q tests/test_retention.py tests/test_store.py tests/test_cli.py tests/test_backup.py` 通过；最终 `.venv/bin/python -m pytest` 为 417 passed、1 条现有 Starlette/httpx 依赖弃用警告，`.venv/bin/ruff check .`、`.venv/bin/python -m compileall -q src tests`、CLI `--help` 和 `git diff --check` 通过。
- 本地临时目录演练：WAL 写事务期间捕获一致性快照；无收据时仍可备份；缺失、篡改、符号链接、不安全 mode 和更改收据映射均拒绝；恢复到已腾空的原绝对路径后，按只读接口抽样核对 ReviewKey/Run 状态、Policy Snapshot、Plan/Relation、通知记录、Operator audit 与备份收据，当前实际 Store 未被演练修改。演练只使用测试构造的数据，不是 JiHuLab/OpenCode 或通知服务的现场证据。
- 仍待 05：在目标 Linux/systemd 身份与真实备份介质上执行停启和异机或等价隔离恢复，核对 owner/group/mode、持续运行时的备份转移、实际服务读写、`/readyz`、新 exact SHA canary、GitLab 与通知结果。macOS 本地测试未执行 Linux 的 `renameat2` 路径。
