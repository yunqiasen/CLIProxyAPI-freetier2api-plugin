package tasks

import (
	"testing"

	"freetier2api-plugin/internal/core"
)

// TestSpecsTagVendorOwnership 锁定任务族的归属标注。
//
// 排程是全局的，而夜猫子 / 开学季这类活动只有单个客户端实现。控制台必须能
// 直接看出归属，否则用户填了时间也不知道会不会白跑。签到与保活由引擎对所有
// 账号统一执行，标「全部渠道」。
func TestSpecsTagVendorOwnership(t *testing.T) {
	registry := core.NewRegistry()
	cn := &fakeScheduledVendor{
		fakeCheckinVendor: fakeCheckinVendor{id: "workbuddycn"},
		kinds: []core.TaskSpec{
			{Kind: "blackcat", Label: "夜猫子"},
			{Kind: core.TaskKindKeepalive, Label: "Token 保活"},
		},
	}
	global := &fakeScheduledVendor{
		fakeCheckinVendor: fakeCheckinVendor{id: "workbuddyglobal"},
		kinds:             []core.TaskSpec{{Kind: "dailybonus", Label: "每日活跃奖励"}},
	}
	if err := registry.Register(cn); err != nil {
		t.Fatal(err)
	}
	if err := registry.Register(global); err != nil {
		t.Fatal(err)
	}

	got := map[string]string{}
	for _, spec := range NewCatalog(registry).Specs() {
		got[spec.Kind] = spec.Vendor
	}
	want := map[string]string{
		core.TaskKindCheckin:   "全部渠道",
		core.TaskKindKeepalive: "全部渠道",
		"blackcat":             cn.Name(),
		"dailybonus":           global.Name(),
	}
	for kind, vendor := range want {
		if got[kind] != vendor {
			t.Errorf("%s vendor = %q, want %q", kind, got[kind], vendor)
		}
	}
}
