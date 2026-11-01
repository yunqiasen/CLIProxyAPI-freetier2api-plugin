# 本地二开与自动加载

## 日常用法

源码只在 `/home/div/1_Project_dir/AI/CPA/freetier2api` 开发，分支 `freetier2api_fork`。
`main` 只同步上游。功能分支完成测试、审查后合回二开分支。

```bash
cd /home/div/1_Project_dir/AI/CPA/freetier2api
# 完成测试、审查，再提交自己的改动
# git add <本次文件>; git commit -m "..."
./scripts/dev-plugin status
```

启用监听服务后，**干净的二开分支出现新提交 → 固定提交快照 → 离线全测/编译 → CPA 切换插件 → 验证实际版本**。
不用 push，不用手动复制 `.so`。保存但未提交、脏工作区、`main`、普通功能分支均跳过。
编译期间出现新提交会丢弃过期结果，等待最新提交。
浏览器里已经打开的插件页面需要刷新。

| 命令（在上述目录） | 作用 |
| --- | --- |
| `./scripts/dev-plugin status` | 查看交付记录、源码状态和监听器；不是实时模型连通测试 |
| `./scripts/dev-plugin status --json` | 完整机器可读记录，包括产物、提交与备份位置 |
| `./scripts/dev-plugin pause` | 暂停后续发布；正在进行的发布会完成并验收 |
| `./scripts/dev-plugin resume` | 恢复监听；失败提交仍需显式重试 |
| `./scripts/dev-plugin publish` | 手动走同一发布流程；同一已加载提交只检查，不重新编译 |
| `./scripts/dev-plugin publish --retry` | 排查失败原因后，重试锁定的提交；先解除暂停 |
| `./scripts/dev-plugin rollback` | 恢复上次成功切换前的插件，暂停期间也可执行 |
| `./scripts/dev-plugin watch` | 前台监听，用于排查服务问题 |

只发生编译失败、尚未切版时，`rollback` 保持运行版本，不倒退到更早一次成功发布。
源码提交与服务启用是分开的：安装 CLI 本身不会启动服务。

## 安装与文件归属

```text
/home/div/1_Project_dir/AI/CPA/freetier2api/
├── scripts/dev-plugin              命令入口
├── scripts/local_delivery/         构建、发布、状态、服务控制
├── tests/local_delivery/           发布器回归测试
└── .local/                         忽略入库；仅当前用户可读
    ├── config.json                 目录、镜像摘要等配置，不存密钥
    └── delivery/                   状态、暂停标记、备份、构建日志与产物
```

CPA 仅接收最终产物，位于：
`/home/div/1_Project_dir/AI/CPA/CLIProxyAPI/logs/plugins/linux/amd64`。
所有发布入口共用该目录下 `.local-delivery.lock`，不按工作区分锁。
备份在插件的 `.local/delivery/backups`，不放进 CPA 扫描目录。
不要通过商店更新、删文件或另一个脚本同时切换此插件。

本机已验证的构建镜像为 Debian 12 bookworm / Go 1.26.4 / linux-amd64：
`sha256:0ba64f001aac341381b87e899320067012f5912a669f836773ee4042cbc545b1`。
这是本机已有镜像 ID，换机器需要准备兼容镜像并填写实际摘要，而不是直接依赖这个本地 ID。
构建强制无网络，不挂载账号、配置或 Docker socket；依赖变化需先维护兼容构建环境。

```bash
cd /home/div/1_Project_dir/AI/CPA/freetier2api
./scripts/dev-plugin init --builder-image sha256:0ba64f001aac341381b87e899320067012f5912a669f836773ee4042cbc545b1
./scripts/dev-plugin publish
# 手动发布实际验证成功后启用提交监听
./scripts/dev-plugin install-service --start
```

先使用提供 `active_path` 的本地 CPA 二开版本；旧版只有发现路径时，发布器在切版前提示先更新宿主。

管理密钥从本机 CPA 容器配置读取，只在内存里用于 loopback 请求；不写入配置、Git、命令行或发布记录。
服务名 `freetier2api-local-delivery.service`。`install-service` 只安装，`--start` 才启用并启动。
服务使用当前用户的 systemd、Docker 权限与本地环境。换目录或修改发布器自身脚本后，重新安装并重启监听服务。

```bash
systemctl --user status freetier2api-local-delivery.service
journalctl --user -u freetier2api-local-delivery.service -n 30 --no-pager
systemctl --user restart freetier2api-local-delivery.service
```

## 怎么证明已加载

成功同时要求：

1. 分支与固定提交仍一致，文件 SHA256 与构建记录一致。
2. CPA 报告的实际注册路径 `active_path`、进程身份和映射 inode 一致；此次切换不替换 CPA 进程。
3. 选择版本、注册版本、启用状态正确；插件页面哈希符合编译内容。
4. CPA 配置、凭证显示名/备注/禁用状态、API 模型列表保持本次发布前的值。

只写 `plugins.configs.freetier2api.store.version`；不覆盖整份配置。
验收读取 `/v1/models` 的本地注册表，不调用插件的模型探测、签到或 Chat/Messages/Responses 生成接口。
HTTP 200、文件已复制、页面可打开，单独都不是加载成功。
涉及模型清单或配置格式的有意变更会触发上述一致性门禁，应暂停自动发布、先调整兼容与验收方案。

## 回滚、并发和限制

- 版本带提交号与随机构建代号，每次重试也用新文件。
- 一旦尝试过候选切换，即使宿主自动保留原实例，回滚也用新副本重新注册，确保旧版后台任务恢复；只有候选尚未选中过才保留原实例。
- 切换前固定旧版本并备份。构建失败保留当前插件；切版失败验证回滚，锁定失败提交，避免反复重试。
- **回滚复制旧二进制到新路径、新 inode**，不直接复用曾卸载的 `.so`。正常构建时文件版本与内嵌版本相同；回滚文件加 `-rollback.<代号>`，内嵌版本仍是原版本，验收分别核对。
- 发现外部操作者切版、进程在未完成交付时改变、外部配置冲突，停止自动恢复，保留现场，状态标记 `recovery_failed`。先看状态与日志，再决定恢复；重复提交不绕过未处理的恢复记录。
- 任务被终止时持久化的切换意图保留；监听恢复后先处理它，再接新提交。
- Go 原生共享库换版后仍保留运行时映射。默认每个 CPA 进程最多 **8 个映射**，发布前预留新版本和回滚各一格；达到门槛显示 `paused_budget`，保留当前插件，**不自动重启 CPA**。
- 映射限制是保守保护，不是内存占用保证。达到门槛后安排一次 CPA 维护重载，确认旧进程退出，再恢复发布；不要只删除 `.so`、提高上限或杀子进程。
- 在途请求沿用 CPA 的旧实例排空机制；隔离流式夹具有覆盖，不承诺所有上游长连接永不受影响。
- 保留构建记录/备份供排查；磁盘清理在暂停后手工维护，避开当前、回滚、未完成交付及已映射产物。

## 上游维护

先暂停监听，确认工作区干净；`git fetch upstream` → `main` 快进上游 → 审查 → 合并到 `freetier2api_fork` → 解决冲突、测试、审查、提交 → 恢复监听。
涉及 SDK/ABI、配置或状态格式升级时先在隔离夹具验证。始终保留二开差异，不强行覆盖。
本流程不自动提交、不 push、不触碰 VPS。

```bash
cd /home/div/1_Project_dir/AI/CPA/freetier2api
python3 -m unittest discover -s tests/local_delivery -p 'test_*.py' -v
# 原生业务测试需使用兼容的 Go/CGO 工具链
CGO_ENABLED=1 go test ./... -count=1
```
