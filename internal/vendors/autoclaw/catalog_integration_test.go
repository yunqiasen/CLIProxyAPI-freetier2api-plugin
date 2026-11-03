package autoclaw_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"reflect"
	"sync/atomic"
	"testing"

	"freetier2api-plugin/internal/admin"
	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/hostbridge/hostbridgetest"
	"freetier2api-plugin/internal/state"
	"freetier2api-plugin/internal/vendors/autoclaw"
)

// Catalog 的公开边界验证目录刷新不改操作者的别名/禁用配置。
func TestCNCatalogRefreshKeepsOperatorSettings(t *testing.T) {
	hostbridgetest.UseLoopbackHost(t)
	var failed atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if failed.Load() {
			http.Error(w, "catalog outage", http.StatusBadGateway)
			return
		}
		_, _ = w.Write([]byte(`{"models":[{"id":"zai_auto","creditConsumptionLevel":"低"},{"id":"zai_glm-5.3-flashx-legal","creditConsumptionLevel":"中"}]}`))
	}))
	t.Cleanup(server.Close)
	registry := core.NewRegistry()
	vendor := autoclaw.Instances(core.Env{})[0]
	if errRegister := registry.Register(vendor); errRegister != nil {
		t.Fatal(errRegister)
	}
	vendor.(core.Configurable).Configure(core.Settings{}, map[string]string{"autoclawcn_base_url": server.URL})
	cred, errParse := vendor.Parse([]byte(`{"accessToken":"fixture-token","region":"cn"}`), "autoclawcn-test.json")
	if errParse != nil {
		t.Fatal(errParse)
	}
	store := state.NewStore()
	store.Configure(t.TempDir())
	if _, errLoad := store.Load(); errLoad != nil {
		t.Fatal(errLoad)
	}
	catalog := admin.NewCatalog(store, registry)
	catalog.SetAlias(vendor.ID(), "zai_auto", "my-auto")
	catalog.SetAlias(vendor.ID(), "removed-model", "old-alias")
	catalog.SetDisabled([]string{"autoclawcn:zai_glm-5.3-flashx-legal", "autoclawcn:removed-model"}, true)
	settingsBefore := store.Snapshot()
	provider := vendor.(core.ModelsProvider)
	for _, outage := range []bool{false, true} {
		failed.Store(outage)
		models, errFetch := provider.ModelsForAuth(context.Background(), cred)
		if errFetch != nil || len(models) != 2 {
			t.Fatalf("catalog during outage=%v: %+v, err=%v", outage, models, errFetch)
		}
		catalog.CacheProbedModels(vendor.ID(), models)
		visible := catalog.ApplyAliases(vendor.ID(), catalog.FilterForVendor(vendor.ID(), catalog.Registered(vendor.ID())))
		if len(visible) != 1 || visible[0].ID != "my-auto" {
			t.Fatalf("operator settings not applied: %+v", visible)
		}
		if catalog.ResolveOutboundModel(vendor.ID(), "my-auto") != "zai_auto" {
			t.Fatal("live model alias stopped resolving")
		}
		if catalog.ResolveOutboundModel(vendor.ID(), "old-alias") != "old-alias" || catalog.IsDisabled(vendor.ID(), "removed-model") {
			t.Fatal("stale model settings affected the live catalog")
		}
		snapshot := store.Snapshot()
		if !reflect.DeepEqual(snapshot.ModelAliases, settingsBefore.ModelAliases) || !reflect.DeepEqual(snapshot.DisabledModels, settingsBefore.DisabledModels) {
			t.Fatal("refresh changed saved operator settings")
		}
	}
	// 首次探测失败不产生缓存写入；重建控制台仍可读取已落盘的成功目录。
	catalog.CacheProbedModels(vendor.ID(), nil)
	snapshot := store.Snapshot()
	restored := admin.NewCatalog(store, registry)
	restored.ReloadFromState(snapshot)
	restored.LoadCachedState(snapshot.Models)
	visible := restored.ApplyAliases(vendor.ID(), restored.FilterForVendor(vendor.ID(), restored.Registered(vendor.ID())))
	if len(visible) != 1 || visible[0].ID != "my-auto" || restored.CreditsFor(vendor.ID(), "zai_glm-5.3-flashx-legal") != "中" {
		t.Fatalf("persisted successful catalog lost settings or credits: %+v", visible)
	}
}
