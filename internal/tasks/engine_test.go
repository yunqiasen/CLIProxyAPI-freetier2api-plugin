package tasks

import (
	"context"
	"testing"
	"time"

	"freetier2api-plugin/internal/core"
)

// TestBuildQueueItemsCollapsesAggregatedTasks 验证聚合任务（RunCode）每个账号只入队一次，
// 且队列里使用的是 RunCode 而不是明细任务码。
func TestBuildQueueItemsCollapsesAggregatedTasks(t *testing.T) {
	service := &Service{}
	scans := []accountScan{
		{
			UID: "u1", Label: "one",
			Tasks: []taskItem{
				{Kind: "growth", Code: "chat_5"},
				{Kind: "school", Code: "share_invite", RunCode: "school_daily"},
				{Kind: "school", Code: "chat_3_times", RunCode: "school_daily"},
			},
		},
		{
			UID: "u2", Label: "two",
			Tasks: []taskItem{
				{Kind: "school", Code: "expert_use", RunCode: "school_daily"},
			},
		},
	}

	items := service.buildQueueItems(scans, nil)
	if len(items) != 3 {
		t.Fatalf("buildQueueItems returned %d items, want 3: %#v", len(items), items)
	}
	want := map[string][]string{
		"u1": {"chat_5", "school_daily"},
		"u2": {"school_daily"},
	}
	for _, item := range items {
		found := false
		for _, code := range want[item.UID] {
			if item.Code == code {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("unexpected queue item for %s: %#v", item.UID, item)
		}
	}
}

// TestBuildQueueItemsFiltersByKind 验证旧控制台的 kind 筛选仍然生效。
func TestBuildQueueItemsFiltersByKind(t *testing.T) {
	service := &Service{}
	scans := []accountScan{{
		UID: "u1",
		Tasks: []taskItem{
			{Kind: "growth", Code: "chat_5"},
			{Kind: "school", Code: "share_invite", RunCode: "school_daily"},
		},
	}}
	items := service.buildQueueItems(scans, map[string]bool{"school": true})
	if len(items) != 1 || items[0].Code != "school_daily" {
		t.Fatalf("filtered queue = %#v, want only school_daily", items)
	}
}

// fakeScheduledVendor 声明若干任务族并记录实际被调用的那些。
type fakeScheduledVendor struct {
	fakeCheckinVendor
	kinds []core.TaskSpec
	calls []string
}

func (v *fakeScheduledVendor) TaskSpecs() []core.TaskSpec { return v.kinds }

func (v *fakeScheduledVendor) RunScheduled(_ context.Context, _ *core.Credential, kind string) (string, error) {
	v.calls = append(v.calls, kind)
	return kind + " done", nil
}

// TestRunScheduledForAccountSkipsUndeclaredKind 验证全局排程被供应商能力过滤。
//
// issue #58：task_schedules 里的 dailybonus 只对国际版有意义，国内账号执行时
// 抛 "unknown scheduled task kind" 并记 ERROR。调度器必须跳过未声明的族。
func TestRunScheduledForAccountSkipsUndeclaredKind(t *testing.T) {
	vendor := &fakeScheduledVendor{
		fakeCheckinVendor: fakeCheckinVendor{id: "workbuddycn"},
		kinds:             []core.TaskSpec{{Kind: "travel"}},
	}
	service := &Service{}
	account := &accountContext{vendor: vendor, credential: &core.Credential{FileID: "u1"}}

	if message, errRun := service.runScheduledForAccount(context.Background(), account, "dailybonus"); message != "" || errRun != nil {
		t.Fatalf("未声明的任务族应跳过，实际返回 %q / %v", message, errRun)
	}
	if len(vendor.calls) != 0 {
		t.Fatalf("未声明的任务族不该下发到供应商，实际调用 %v", vendor.calls)
	}

	if message, errRun := service.runScheduledForAccount(context.Background(), account, "travel"); message != "travel done" || errRun != nil {
		t.Fatalf("已声明的任务族应正常执行，返回 %q / %v", message, errRun)
	}
	if len(vendor.calls) != 1 || vendor.calls[0] != "travel" {
		t.Fatalf("调用记录 = %v, want [travel]", vendor.calls)
	}
}

// TestEffectiveScheduleGatesRaccoonDailyGrant 锁住小浣熊每日积分按开关门控：
// 开关关闭时从排程移除，开关开启（或未设置回落配置）时保留默认小时。
func TestEffectiveScheduleGatesRaccoonDailyGrant(t *testing.T) {
	registry := core.NewRegistry()
	_ = registry.Register(&fakeScheduledVendor{
		fakeCheckinVendor: fakeCheckinVendor{id: "raccoon"},
		kinds:             []core.TaskSpec{{Kind: core.TaskKindRaccoonDailyGrant, DefaultHours: []int{8}}},
	})
	deps := newFakeDeps(time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC))
	deps.registry = registry
	// 与真实组合根一致：配置默认开启小浣熊每日积分。
	deps.cfg.AutoRaccoonDailyGrant = true
	service := &Service{deps: deps, catalog: NewCatalog(registry)}

	// 默认（开关未设置）→ 配置默认开启 → 排程含该族。
	on := service.effectiveSchedule()
	if _, ok := on[core.TaskKindRaccoonDailyGrant]; !ok {
		t.Fatalf("默认开启时排程应含 raccoon_daily_grant，得到 %v", on)
	}

	// 页面设置关闭 → 排程移除。
	off := false
	deps.state.AutoRaccoonDailyGrant = &off
	offSchedule := service.effectiveSchedule()
	if _, ok := offSchedule[core.TaskKindRaccoonDailyGrant]; ok {
		t.Fatalf("开关关闭时排程应移除 raccoon_daily_grant，得到 %v", offSchedule)
	}

	// 页面设置开启 → 排程保留。
	onFlag := true
	deps.state.AutoRaccoonDailyGrant = &onFlag
	onAgain := service.effectiveSchedule()
	if _, ok := onAgain[core.TaskKindRaccoonDailyGrant]; !ok {
		t.Fatalf("开关开启时排程应含 raccoon_daily_grant，得到 %v", onAgain)
	}
}
