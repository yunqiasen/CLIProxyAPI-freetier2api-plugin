# 06: 全部 vendor 国内/国际签到实测矩阵

**What to build:** 以运行时已安装 `/vendors` 与 `/status` 为边界，逐 vendor 固定第一把凭证；国内/国际分别列项，不把七个品牌当七个渠道。默认只查资格/已领状态，在已批准且明确可领时实际领取每日奖励一次，保存可横向比较的真实结果。

**Blocked by:** 01: 修复 Qoder 正式出口链路

**Status:** 已交付；实测与已知限制见 `lab/results/capability-delivery-20261103.md`。

- [x] 覆盖 `autoclawcn`、`autoclawglobal`、`codearts`、`minimaxcodecn`、`minimaxcodeglobal`、`qodercn`、`qoderglobal`、`raccoon`、`traecn`、`traeglobal`、`workbuddycn`、`workbuddyglobal`、`cline`、`opencodezen`、`zcodecn`、`zcodeglobal` 共16个当前实例；以完整实际 ID 落表，新增安装实例补列
- [x] 每实例按 `file_id`、`auth_id` 排序固定首凭证；停用首凭证不换号，无凭证标记未测试
- [x] 未声明能力逐项标注，不记失败；MiniMax 国内预修复能力错误与上游可领状态并列
- [x] 分清本次新领、已领无新增、窗口未开、当前无活动、凭证失效、网络失败、上游异常及插件能力错误
- [x] 窗口开启时刻仅在上游明确时显示；HTTP 200、VIEW_DETAILS、活跃事件均不冒充奖励到账
- [x] 新领有回执/状态变化，并尽量记录前后数值；没有前值不编造增量，重复运行有领取防重
- [x] 小浣熊定时/一次奖励、WorkBuddy 国际活跃次日奖励、ZCode 体验套餐与每日签到分开，不运行无关任务、重登、模型或付费动作
- [x] TSV/结构化结果包含中文渠道名、vendor、区域、脱敏凭证、实际出口、endpoint、HTTP码、业务码、上游基本消息、结果、时间；不落盘 token
- [x] 脚本支持只读与显式可领取模式，每请求最多30秒，无无限循环；不修改凭证或全局代理
- [x] 报告区分插件缺陷、凭证生命周期、上游限制与网络归因，结论只覆盖当前抽样
- [x] 保留08:56原审计时间与7891/7899差异；正式出口修复后另留附记与复验证据，不覆盖历史结果
