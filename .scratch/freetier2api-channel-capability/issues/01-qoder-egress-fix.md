# 01: 修复 Qoder 正式出口链路

**What to build:** 让 CPA 正式出口使用受 systemd 托管的 7891，独立登记 Qoder 国际版相关域名与子域直连分流。验收轮询、带身份接口与批准的真实调用；通过后清理临时 7899。历史部分节点 TLS 失败仅描述对应样本，不外推全部节点。

**Blocked by:** None (can start immediately)

**Status:** 已交付；实测与已知限制见 `lab/results/capability-delivery-20261103.md`。

- [x] 服务定义与运行配置有可回滚的私有备份，保留线上已有用户配置
- [x] 受托管出口包含 `qoder.sh`、`qoder.com`、`qoder.com.cn` 及子域直连，其它域名继续既定上游
- [x] CPA `proxy-url` 经管理 API 保存为 7891，并核对 API、容器文件、宿主文件三处
- [x] 核查单文件 bind mount 是否仍指向旧 inode；维护不以替换宿主文件 inode 代替运行配置更新
- [x] 连通性记录精确 URL/出口/状态/时间；404 只算该路径响应，不算授权通过
- [x] 带身份接口业务码、签到结果和批准的真实调用分别验收，HTTP 200 不代替业务成功
- [x] 临时 7899 在正式链路通过后终止，端口释放，无第二份同功能监听
- [ ] 正式服务与配置的回滚演练未执行；备份与恢复路径已保留，发布器离线回滚门禁已验证。

2026-11-03 执行者交接：管理 API 已保存并回读 7891；此前宿主原子替换 inode 导致容器单文件 bind 仍是 7899。已保留线上 `codex` 额外一项 `excluded-models` 并同步宿主字节，容器 bind 尚未重建。私有证据在 `/home/div/1_Project_dir/AI/CPA/freetier2api/.local/evidence/20261103`；此进展不是整票验收完成。

当前核验：7891 systemd active/enabled，运行参数含Qoder三域，7899无监听；API与两侧文件均为7891且字节一致。普通CPA进程重载保持容器ID。未把未执行的正式服务回滚演练勾为完成。
