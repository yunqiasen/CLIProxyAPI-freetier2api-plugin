# 2026-11-03 渠道能力与测速验收

本轮范围全部保留：7899清理/7891托管、Qoder特殊分流、MiniMax国内签到、AutoClaw实时目录、插件模型行与账号行测速、原始报错、全渠道签到审计。唯一排除的是CPA认证文件卡片改造，管理台仓库无本轮改动。

## lab 真实结果

隔离CPA端口18317，宿主为本轮新代码，插件实际加载 `0.1.23-lab.20261103final2`。

| 中文渠道 | 模型 | 结果 |
|---|---|---|
| MiniMax Code 国内版 | `lab-mm-flash` → `minimax-m3.1-flash` | HTTP200，正文 `ok`；本轮最后一次14.2秒，此前约5秒 |
| AutoClaw 国内版 | `zai_auto-fast` | HTTP200，正文 `ok`；插件模型行与账号行浏览器均真实通过，约2秒 |
| Qoder 国际版 | `efficient` | 414ms取得上游HTTP403/code112/pricingUrl；链路已到上游，账号业务限制，不记成功 |
| WorkBuddy 国际版 | `kimi-k3.1-pro` | 实测HTTP400/code11158原文 `is not allowed for user type [personal_free_user]`；其它轮次出现TLS握手超时，明确列为网络失败 |

AutoClaw当前实时目录：`zai_auto`、`zai_auto-fast`、`zai_glm-5.3-flash`、`zai_glm-5.3-flashx-legal`、`zaicoding_glm-5.3`。条目数不写死；旧用户禁用和别名不重置。目录出现不等于账号对该模型有权限。

## 本轮找到并补齐的缺陷

1. 宿主单文件bind指向旧inode，宿主配置与容器/API出口不同。经管理API保存7891，保留线上用户配置，再同步宿主字节；V8实际字段为 `requests.proxy-url`。
2. RPC丢了凭证HTTPClient。普通、流式、异步、签到/额度/目录回调均按固定凭证出口继承。
3. MiniMax国内签到能力错误；看板模糊时仍claim。现在仅明确今日status2可领执行，窗口/已领/空活动/异常分别返回。
4. 模型错误被分类器截断或改写，部分adapter二次转换又丢原始链。保留分类前状态/正文及私有cause，展示端只脱敏、明确截断；生产分类字段不变。
5. 测速可能被宿主和Qoder内层重试。可选request_stop沿RPC透传，借现有停止重试接口，只测一次且不附加凭证冷却。
6. “90秒”只在插件本地context中，未跨原生RPC。页面现在真实取消fetch并解锁按钮，晚到结果不覆盖取消。
7. lab的JSON-as-YAML被插件行解析器误读。准备脚本输出块状YAML，先核对自动任务均false；10:00历史日志确认国内MiniMax自动新领400点、AutoClaw自动新领200点，手动/checkin只验证已领幂等，不冒称手动新领取。
8. 目录过渡发布器可能把旧active/skipped当成功。锁内固定批准完整commit，结果要求无skipped且active.commit匹配。

## 检查与审查

- 插件完整Go tests/vet、相关race、Go1.26.4 bookworm c-shared构建通过；真实DOM事件测试通过。
- CPA隔离完整Go tests、pluginhost vet/race、server build通过。离线builder缺失模块缓存的问题通过只读挂载完整模块缓存解决，未修改依赖。
- 常规发布器75项通过；一次性目录发布门禁2项、签到审计11项通过。首次并行繁忙时一项watcher测试超时，单项与再次完整75项均通过。
- 两轴审查及有效发现的修复复核完成，无剩余代码阻断。
- 430px浏览器：页面宽430、弹窗390，无横向撑破、无页面JS错误；模型/账号两入口共用弹窗，正文为实际 `ok`。

## 全渠道签到与正式交付

16个已安装vendor按国内/国际分别列项；11个首凭证样本，5渠道无凭证。原08:56审计保留，不覆盖历史结果；MiniMax国际已实测新领1000点。小浣熊定时发放、WorkBuddy国际活跃奖励、Qoder国际VIEW_DETAILS不冒充每日新领取。

本机最终加载、出口真值与部署后只读矩阵以CPA根目录 `lab/results/capability-delivery-20261103.md` 为准。正式发布继续执行配置、凭证、进程、共享库hash、模型库存和回滚门禁，保留原用户设置。
