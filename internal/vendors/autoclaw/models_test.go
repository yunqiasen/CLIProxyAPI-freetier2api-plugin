package autoclaw

// 本文件锁定两个区域模型目录（autoclaw-model-config）的响应形态解析。
//
// 上游实测返回的是**顶层 models 数组**（没有 code/msg/data 信封）：
//
//	{"models":[{"id":"zai_auto",...,"input":["image","text"]}, ...]}
//
// 旧实现只认 data.models：JSON 解析成功、模型却是 0 条，于是宿主把该凭证
// 当作「没有可用模型」淘汰，控制台模型列表整片为空，而日志里只有一行
// "probe returned 0 model(s)"。这里同时锁住两种形态与「空清单要报错」。

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"freetier2api-plugin/internal/core"
	"freetier2api-plugin/internal/hostbridge/hostbridgetest"
)

// modelConfigTopLevelFixture 是上游实测响应的结构（数值按真实值）。
const modelConfigTopLevelFixture = `{"models":[
 {"id":"zai_auto","name":"Auto","reasoning":true,"input":["image","text"],
  "contextWindow":1048576,"maxTokens":131072,
  "tooltip":"Automatically picks the best model for your task."},
 {"id":"zai_auto-fast","name":"Auto Fast","reasoning":true,"input":["text"],
  "contextWindow":1048576,"maxTokens":131072},
 {"id":"zaicoding_glm-5.3","name":"GLM-5.3","reasoning":true,"input":["text"],
  "contextWindow":1048576,"maxTokens":307200,"tooltip":"GLM-5.3 coding"}]}`

// modelConfigCNFixture 复刻 2026-11-02 lab 的国内版顶层结构、ID、档位与输入模态。
// future-upstream-model 是新增条目哨兵：五条实测结果不是永久白名单。
const modelConfigCNFixture = `{"models":[
 {"id":"zai_auto","name":"Auto","creditConsumptionLevel":"低","input":["text"]},
 {"id":"zai_auto-fast","name":"Auto-Fast","creditConsumptionLevel":"中","input":["text","image"]},
 {"id":"zaicoding_glm-5.3","name":"GLM-5.3","creditConsumptionLevel":"高","input":["text"]},
 {"id":"zai_glm-5.3-flash","name":"GLM-5.3-Flash","creditConsumptionLevel":"低","input":["text","image"]},
 {"id":"zai_glm-5.3-flashx-legal","name":"GLM-5.3-FlashX-Legal","creditConsumptionLevel":"中","input":["text","image"]},
 {"id":"future-upstream-model","name":"New upstream model","creditConsumptionLevel":"高","input":["text"]}]}`

// modelConfigEnvelopeFixture 是旧信封形态。
const modelConfigEnvelopeFixture = `{"code":0,"msg":"SUCCESS","data":{"models":[
 {"id":"legacy-model","name":"Legacy","contextWindow":200000,"maxTokens":8192}]}}`

// serveModels 起一个只服务 ModelsConfigPath 的桩，并断言带了鉴权头。
//
// 出站桥也在这一层装上：用例只要拿到桩地址就必须能真的出站，避免依赖
// 同包其它用例留下的全局配置。
func serveModels(t *testing.T, body string) string {
	t.Helper()
	hostbridgetest.UseLoopbackHost(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != ModelsConfigPath {
			t.Errorf("path = %q, want %q", r.URL.Path, ModelsConfigPath)
		}
		if got := r.Header.Get("Authorization"); !strings.Contains(got, "tok-abc") {
			t.Errorf("Authorization = %q, want token", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(server.Close)
	return server.URL
}

func TestFetchModelsCNUsesLiveCatalog(t *testing.T) {
	baseURL := serveModels(t, modelConfigCNFixture)
	models, errFetch := FetchModels(context.Background(), baseURL, "tok-abc", RegionCN)
	if errFetch != nil {
		t.Fatalf("fetch CN models: %v", errFetch)
	}
	ids := make([]string, 0, len(models))
	for _, model := range models {
		ids = append(ids, model.ID)
	}
	want := []string{"zai_auto", "zai_auto-fast", "zaicoding_glm-5.3", "zai_glm-5.3-flash", "zai_glm-5.3-flashx-legal", "future-upstream-model"}
	if !reflect.DeepEqual(ids, want) {
		t.Fatalf("CN models = %v, want the live catalog %v", ids, want)
	}
	if models[0].SupportsImages || !models[4].SupportsImages || models[4].Credits != "中" || models[4].Name != "GLM-5.3-FlashX-Legal" {
		t.Fatalf("CN metadata not preserved: %+v", models)
	}
}

func TestFetchModelsParsesTopLevelModels(t *testing.T) {
	baseURL := serveModels(t, modelConfigTopLevelFixture)

	models, errFetch := FetchModels(context.Background(), baseURL, "tok-abc", RegionGlobal)
	if errFetch != nil {
		t.Fatalf("fetch models: %v", errFetch)
	}
	if len(models) != 3 {
		t.Fatalf("models = %d, want 3（顶层 models 未被识别）", len(models))
	}
	byID := make(map[string]Model, len(models))
	for _, model := range models {
		byID[model.ID] = model
	}
	auto, okAuto := byID["zai_auto"]
	if !okAuto {
		t.Fatalf("missing zai_auto, got %v", byID)
	}
	if auto.Name != "Auto" || auto.ContextWindow != 1048576 || auto.MaxOutputTokens != 131072 || !auto.IsReasoning {
		t.Fatalf("zai_auto metadata = %+v", auto)
	}
	if !auto.SupportsImages || !auto.SupportsTools {
		t.Fatalf("zai_auto capabilities = %+v, want images+tools", auto)
	}
	if glm := byID["zaicoding_glm-5.3"]; glm.SupportsImages {
		t.Fatalf("zaicoding_glm-5.3 (input=text) must not claim image support")
	}
}

func TestFetchModelsStillParsesEnvelope(t *testing.T) {
	baseURL := serveModels(t, modelConfigEnvelopeFixture)

	models, errFetch := FetchModels(context.Background(), baseURL, "tok-abc", RegionGlobal)
	if errFetch != nil {
		t.Fatalf("fetch models: %v", errFetch)
	}
	if len(models) != 1 || models[0].ID != "legacy-model" {
		t.Fatalf("models = %+v, want legacy-model", models)
	}
}

func TestFetchModelsErrorsOnUnknownShape(t *testing.T) {
	// 解析成功但没有任何模型：必须报错，而不是静默返回空清单。
	baseURL := serveModels(t, `{"code":0,"msg":"SUCCESS","data":{}}`)

	models, errFetch := FetchModels(context.Background(), baseURL, "tok-abc", RegionGlobal)
	if errFetch == nil {
		t.Fatalf("expected error for empty model list, got %+v", models)
	}
	if !strings.Contains(errFetch.Error(), "0 model") {
		t.Fatalf("error = %v, want it to mention 0 model", errFetch)
	}
}

// TestFetchModelsParsesCreditLevel 锁定计费档位的透传。
//
// 上游 creditConsumptionLevel 是文字档位（低/中/高），不是折扣倍数；原样
// 透传到倍率列，整数则标成「档位 N」，缺失则留空（控制台显示「—」，不能
// 写成免费，也不能把 3 格式化成 3.00X）。
func TestFetchModelsParsesCreditLevel(t *testing.T) {
	baseURL := serveModels(t, `{"models":[
	 {"id":"zai_auto","creditConsumptionLevel":"中"},
	 {"id":"zai_auto-fast","creditConsumptionLevel":"低"},
	 {"id":"numeric","creditConsumptionLevel":3},
	 {"id":"none"}]}`)

	models, errModels := FetchModels(context.Background(), baseURL, "tok-abc", RegionGlobal)
	if errModels != nil {
		t.Fatalf("fetch: %v", errModels)
	}
	want := map[string]string{"zai_auto": "中", "zai_auto-fast": "低", "numeric": "档位 3", "none": ""}
	for _, model := range models {
		if model.Credits != want[model.ID] {
			t.Fatalf("%s: Credits = %q, want %q", model.ID, model.Credits, want[model.ID])
		}
	}

}

// TestLevelTextToleratesShapes 守住未知形态：宁可留空，也不把结构塞进倍率列。
func TestLevelTextToleratesShapes(t *testing.T) {
	cases := map[string]string{
		`"高"`:           "高",
		`3`:             "档位 3", // 整数是档位编号，不能被格式化成伪倍率
		`null`:          "",
		``:              "",
		`{"level":"低"}`: "",
	}
	for raw, want := range cases {
		if got := levelText(json.RawMessage(raw)); got != want {
			t.Fatalf("levelText(%s) = %q, want %q", raw, got, want)
		}
	}
}

// TestAdapterModelCredits 锁定倍率表与清单同源。
func TestAdapterModelCredits(t *testing.T) {
	v := &adapter{}
	v.modelsToPluginAPI([]Model{
		{ID: "zai_auto", Credits: "中"},
		{ID: "tier_3", Credits: "档位 3"},
		{ID: "plain"},
	})
	credits := v.ModelCredits()
	if got := credits["zai_auto"]; got != "中" {
		t.Fatalf("倍率 = %q, want 中", got)
	}
	if got := credits["tier_3"]; got != "档位 3" {
		t.Fatalf("整数档位 = %q, want 档位 3（不能变成 3.00X）", got)
	}
	if _, exists := credits["plain"]; exists {
		t.Fatal("没有档位的模型不应进表")
	}
}

// 通过供应商 ModelsProvider 边界验证失败回落，不读取内部缓存。
func TestCNModelsForAuthKeepsLastSuccessfulCatalog(t *testing.T) {
	hostbridgetest.UseLoopbackHost(t)
	var status atomic.Int64
	status.Store(http.StatusOK)
	var requests atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.Method != http.MethodGet || r.URL.Path != ModelsConfigPath {
			t.Errorf("request = %s %s, want GET %s", r.Method, r.URL.Path, ModelsConfigPath)
		}
		if r.Header.Get("Authorization") != "Bearer tok-abc" {
			t.Errorf("Authorization = %q", r.Header.Get("Authorization"))
		}
		w.WriteHeader(int(status.Load()))
		if status.Load() == http.StatusOK {
			_, _ = w.Write([]byte(modelConfigCNFixture))
		} else {
			_, _ = w.Write([]byte("catalog temporarily unavailable"))
		}
	}))
	t.Cleanup(server.Close)
	vendor := Instances(core.Env{})[0]
	vendor.(core.Configurable).Configure(core.Settings{}, map[string]string{"autoclawcn_base_url": server.URL})
	cred, errParse := vendor.Parse([]byte(`{"accessToken":"tok-abc","region":"cn"}`), "autoclawcn-test.json")
	if errParse != nil {
		t.Fatal(errParse)
	}
	provider := vendor.(core.ModelsProvider)
	first, errFirst := provider.ModelsForAuth(context.Background(), cred)
	if errFirst != nil || len(first) != 6 {
		t.Fatalf("first catalog = %+v, err = %v", first, errFirst)
	}
	status.Store(http.StatusBadGateway)
	fallback, errFallback := provider.ModelsForAuth(context.Background(), cred)
	if errFallback != nil || !reflect.DeepEqual(fallback, first) {
		t.Fatalf("failed refresh catalog = %+v, err = %v; want previous successful catalog", fallback, errFallback)
	}
	if requests.Load() != 2 {
		t.Fatalf("upstream requests = %d, want a live request on each refresh", requests.Load())
	}
	if got := vendor.(core.CreditsProvider).ModelCredits()["zai_glm-5.3-flashx-legal"]; got != "中" {
		t.Fatalf("cached catalog credits = %q, want 中", got)
	}
}

func TestCNModelsForAuthDoesNotBorrowAnotherCredentialCatalog(t *testing.T) {
	hostbridgetest.UseLoopbackHost(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") == "Bearer good-token" {
			_, _ = w.Write([]byte(modelConfigCNFixture))
			return
		}
		http.Error(w, "credential rejected", http.StatusUnauthorized)
	}))
	t.Cleanup(server.Close)
	vendor := Instances(core.Env{})[0]
	vendor.(core.Configurable).Configure(core.Settings{}, map[string]string{"autoclawcn_base_url": server.URL})
	good, errGood := vendor.Parse([]byte(`{"accessToken":"good-token","region":"cn"}`), "autoclawcn-good.json")
	if errGood != nil {
		t.Fatal(errGood)
	}
	bad, errBad := vendor.Parse([]byte(`{"accessToken":"bad-token","region":"cn"}`), "autoclawcn-bad.json")
	if errBad != nil {
		t.Fatal(errBad)
	}
	provider := vendor.(core.ModelsProvider)
	if models, errFetch := provider.ModelsForAuth(context.Background(), good); errFetch != nil || len(models) != 6 {
		t.Fatalf("good credential catalog = %+v, err = %v", models, errFetch)
	}
	models, errFetch := provider.ModelsForAuth(context.Background(), bad)
	if len(models) != 0 || errFetch == nil || !strings.Contains(errFetch.Error(), "credential rejected") {
		t.Fatalf("first failed credential probe = %+v, err = %v; want empty catalog and original reason", models, errFetch)
	}
}

func TestFetchModelsCNRejectsEntriesWithoutIDs(t *testing.T) {
	baseURL := serveModels(t, `{"models":[{"id":" "},{"name":"missing id"}]}`)
	models, errFetch := FetchModels(context.Background(), baseURL, "tok-abc", RegionCN)
	if len(models) != 0 || errFetch == nil || !strings.Contains(errFetch.Error(), "0 valid model") {
		t.Fatalf("invalid catalog = %+v, err = %v; want an explicit empty catalog error", models, errFetch)
	}
}

func TestCNModelsForAuthFirstFailureReturnsReason(t *testing.T) {
	for name, fixture := range map[string]string{
		"upstream business error": `{"code":410000,"msg":"session expired"}`,
		"malformed JSON":          `{"models":`,
		"empty catalog":           `{"models":[]}`,
		"unknown shape":           `{"data":{"updated":true}}`,
		"entries without IDs":     `{"models":[{"id":" "}]}`,
	} {
		t.Run(name, func(t *testing.T) {
			baseURL := serveModels(t, fixture)
			vendor := Instances(core.Env{})[0]
			vendor.(core.Configurable).Configure(core.Settings{}, map[string]string{"autoclawcn_base_url": baseURL})
			cred, errParse := vendor.Parse([]byte(`{"accessToken":"tok-abc","region":"cn"}`), "autoclawcn-test.json")
			if errParse != nil {
				t.Fatal(errParse)
			}
			models, errFetch := vendor.(core.ModelsProvider).ModelsForAuth(context.Background(), cred)
			if len(models) != 0 || errFetch == nil || strings.TrimSpace(errFetch.Error()) == "" {
				t.Fatalf("first catalog = %+v, err = %v; want empty catalog and readable reason", models, errFetch)
			}
		})
	}
}

func TestCNModelsForAuthRefreshReplacesLastSuccessfulCatalog(t *testing.T) {
	hostbridgetest.UseLoopbackHost(t)
	var body atomic.Value
	body.Store(modelConfigCNFixture)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(body.Load().(string)))
	}))
	t.Cleanup(server.Close)
	vendor := Instances(core.Env{})[0]
	vendor.(core.Configurable).Configure(core.Settings{}, map[string]string{"autoclawcn_base_url": server.URL})
	cred, errParse := vendor.Parse([]byte(`{"accessToken":"tok-abc","region":"cn"}`), "autoclawcn-test.json")
	if errParse != nil {
		t.Fatal(errParse)
	}
	provider := vendor.(core.ModelsProvider)
	first, errFirst := provider.ModelsForAuth(context.Background(), cred)
	if errFirst != nil || len(first) != 6 {
		t.Fatalf("first catalog = %+v, err = %v", first, errFirst)
	}
	for _, invalid := range []string{`{"models":`, `{"models":[]}`, `{"models":[{"id":" "}]}`} {
		body.Store(invalid)
		fallback, errFetch := provider.ModelsForAuth(context.Background(), cred)
		if errFetch != nil || !reflect.DeepEqual(fallback, first) {
			t.Fatalf("invalid response %s replaced catalog: %+v, err = %v", invalid, fallback, errFetch)
		}
	}
	body.Store(`{"models":[{"id":"next-model","name":"Next","creditConsumptionLevel":"低"}]}`)
	next, errNext := provider.ModelsForAuth(context.Background(), cred)
	if errNext != nil || len(next) != 1 || next[0].ID != "next-model" {
		t.Fatalf("changed upstream catalog = %+v, err = %v", next, errNext)
	}
	body.Store(`{"models":[]}`)
	fallback, errFallback := provider.ModelsForAuth(context.Background(), cred)
	if errFallback != nil || !reflect.DeepEqual(fallback, next) {
		t.Fatalf("recovered catalog was not cached: %+v, err = %v", fallback, errFallback)
	}
	if got := vendor.(core.CreditsProvider).ModelCredits(); !reflect.DeepEqual(got, map[string]string{"next-model": "低"}) {
		t.Fatalf("credits after catalog replacement = %+v", got)
	}
}

// cnCatalogHost 只替代宿主凭证端口，真实 adapter 与解析器照常执行。
type cnCatalogHost struct{}

func (cnCatalogHost) ListAuths(context.Context) ([]core.HostAuth, error) {
	return []core.HostAuth{{AuthIndex: "test", Name: "autoclawcn-test.json"}}, nil
}

func (cnCatalogHost) AuthJSON(context.Context, string) ([]byte, bool) {
	return []byte(`{"accessToken":"tok-abc","region":"cn"}`), true
}

func (cnCatalogHost) Settings() core.Settings { return core.Settings{} }

func TestCNStaticModelsKeepsLastSuccessfulCatalog(t *testing.T) {
	hostbridgetest.UseLoopbackHost(t)
	var failed atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if failed.Load() {
			http.Error(w, "catalog outage", http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte(modelConfigCNFixture))
	}))
	t.Cleanup(server.Close)
	registry := core.NewRegistry()
	vendor := Instances(core.Env{Host: cnCatalogHost{}, Registry: registry})[0]
	if errRegister := registry.Register(vendor); errRegister != nil {
		t.Fatal(errRegister)
	}
	vendor.(core.Configurable).Configure(core.Settings{}, map[string]string{"autoclawcn_base_url": server.URL})
	first, errFirst := vendor.StaticModels(context.Background())
	if errFirst != nil || len(first) != 6 {
		t.Fatalf("first static catalog = %+v, err = %v", first, errFirst)
	}
	failed.Store(true)
	fallback, errFallback := vendor.StaticModels(context.Background())
	if errFallback != nil || !reflect.DeepEqual(fallback, first) {
		t.Fatalf("static catalog fallback = %+v, err = %v", fallback, errFallback)
	}

	// 新实例与切换后的基地址都没有已验证的目录，首次失败要如实报错。
	other := Instances(core.Env{})[0]
	other.(core.Configurable).Configure(core.Settings{}, map[string]string{"autoclawcn_base_url": server.URL})
	cred, errParse := other.Parse([]byte(`{"accessToken":"tok-abc","region":"cn"}`), "autoclawcn-test.json")
	if errParse != nil {
		t.Fatal(errParse)
	}
	if models, errFetch := other.(core.ModelsProvider).ModelsForAuth(context.Background(), cred); errFetch == nil || len(models) != 0 {
		t.Fatalf("independent instance borrowed another catalog: %+v, err = %v", models, errFetch)
	}
	vendor.(core.Configurable).Configure(core.Settings{}, map[string]string{"autoclawcn_base_url": server.URL + "/other"})
	if models, errFetch := vendor.StaticModels(context.Background()); errFetch == nil || len(models) != 0 {
		t.Fatalf("changed upstream borrowed previous catalog: %+v, err = %v", models, errFetch)
	}
}

func TestFetchModelsEnvelopeSupportedInBothRegions(t *testing.T) {
	for _, region := range []Region{RegionCN, RegionGlobal} {
		t.Run(fmt.Sprint(region), func(t *testing.T) {
			baseURL := serveModels(t, modelConfigEnvelopeFixture)
			models, errFetch := FetchModels(context.Background(), baseURL, "tok-abc", region)
			if errFetch != nil || len(models) != 1 || models[0].ID != "legacy-model" {
				t.Fatalf("%s envelope catalog = %+v, err = %v", region, models, errFetch)
			}
		})
	}
}

func TestCNModelsForAuthConcurrentRefreshPreservesCatalog(t *testing.T) {
	hostbridgetest.UseLoopbackHost(t)
	var failed atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if failed.Load() {
			http.Error(w, "catalog outage", http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte(modelConfigCNFixture))
	}))
	t.Cleanup(server.Close)
	vendor := Instances(core.Env{})[0]
	vendor.(core.Configurable).Configure(core.Settings{}, map[string]string{"autoclawcn_base_url": server.URL})
	cred, errParse := vendor.Parse([]byte(`{"accessToken":"tok-abc","region":"cn"}`), "autoclawcn-test.json")
	if errParse != nil {
		t.Fatal(errParse)
	}
	provider := vendor.(core.ModelsProvider)
	for _, isFailure := range []bool{false, true} {
		failed.Store(isFailure)
		var workers sync.WaitGroup
		for range 8 {
			workers.Add(1)
			go func() {
				defer workers.Done()
				models, errFetch := provider.ModelsForAuth(context.Background(), cred)
				if errFetch != nil || len(models) != 6 || models[4].ID != "zai_glm-5.3-flashx-legal" {
					t.Errorf("concurrent catalog = %+v, err = %v", models, errFetch)
				}
			}()
		}
		workers.Wait()
	}
}

func TestCNModelsForAuthExpiredCredentialDoesNotUseCachedCatalog(t *testing.T) {
	hostbridgetest.UseLoopbackHost(t)
	var expired atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		if expired.Load() {
			http.Error(w, "credential expired", http.StatusUnauthorized)
			return
		}
		_, _ = w.Write([]byte(modelConfigCNFixture))
	}))
	t.Cleanup(server.Close)
	vendor := Instances(core.Env{})[0]
	vendor.(core.Configurable).Configure(core.Settings{}, map[string]string{"autoclawcn_base_url": server.URL})
	cred, errParse := vendor.Parse([]byte(`{"accessToken":"tok-abc","region":"cn"}`), "autoclawcn-test.json")
	if errParse != nil {
		t.Fatal(errParse)
	}
	provider := vendor.(core.ModelsProvider)
	if models, errFetch := provider.ModelsForAuth(context.Background(), cred); errFetch != nil || len(models) != 6 {
		t.Fatalf("first catalog = %+v, err = %v", models, errFetch)
	}
	expired.Store(true)
	models, errFetch := provider.ModelsForAuth(context.Background(), cred)
	if len(models) != 0 || errFetch == nil || !strings.Contains(errFetch.Error(), "credential expired") {
		t.Fatalf("expired credential borrowed cached catalog: %+v, err = %v", models, errFetch)
	}
}
