package qoder

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// TestExtractEmbeddedRuntimeInfo 验证内置组件能落盘成可执行文件。
func TestExtractEmbeddedRuntimeInfo(t *testing.T) {
	if embeddedAssetPath() == "" {
		t.Skip("该平台未内置 runtime-info")
	}
	if embeddedRuntimeDisabled() {
		t.Skip("QODER_NO_EMBEDDED_RUNTIME_INFO 已设置（离线构建）")
	}
	path, okExtract := extractEmbeddedRuntimeInfo()
	if !okExtract {
		t.Fatalf("解包失败")
	}
	info, errStat := os.Stat(path)
	if errStat != nil {
		t.Fatalf("解包产物不可用: %v", errStat)
	}
	blob, errBlob := embeddedAssets.ReadFile(embeddedAssetPath())
	if errBlob != nil {
		t.Fatalf("读取内置资源失败: %v", errBlob)
	}
	if info.Size() != int64(len(blob)) {
		t.Fatalf("大小不符: %d != %d", info.Size(), len(blob))
	}
	if runtime.GOOS != "windows" && info.Mode().Perm()&0o100 == 0 {
		t.Fatalf("缺少可执行位: %v", info.Mode().Perm())
	}
	// 二次调用必须命中同一份，不重复写盘。
	again, okAgain := extractEmbeddedRuntimeInfo()
	if !okAgain || again != path {
		t.Fatalf("重复解包路径不一致: %q != %q", again, path)
	}
}

// TestEmbeddedMachineIdentity 验证内置组件在本机真的能产出身份。
func TestEmbeddedMachineIdentity(t *testing.T) {
	if testing.Short() {
		t.Skip("短模式跳过进程调用")
	}
	if embeddedAssetPath() == "" {
		t.Skip("该平台未内置 runtime-info")
	}
	// 组件首次运行需要联网注册设备身份；离线构建（--network none）里只能跳过。
	if embeddedRuntimeDisabled() {
		t.Skip("QODER_NO_EMBEDDED_RUNTIME_INFO 已设置（离线构建）")
	}
	identity := embeddedMachineIdentity(RegionGlobal)
	if !identity.Native() {
		t.Fatalf("未生成机器身份")
	}
	if len(identity.Token) < 32 || identity.Type == "" {
		t.Fatalf("身份字段不完整: token_len=%d type=%q", len(identity.Token), identity.Type)
	}
}

// embeddedRuntimeDisabled 报告是否显式关闭了内置组件（离线构建用）。
func embeddedRuntimeDisabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("QODER_NO_EMBEDDED_RUNTIME_INFO"))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

func TestLocalMachineIdentityKeepsEmbeddedRegionsSeparate(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("shell fixture")
	}
	disableLocalMachineIdentity(t)
	dir := t.TempDir()
	executable := filepath.Join(dir, "runtime-info")
	script := "#!/bin/sh\nprintf '{\"machineToken\":\"fixture-%s\",\"machineType\":\"fixture-type\"}' \"$1\"\n"
	if err := os.WriteFile(executable, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	oldExecutable := embeddedRuntimeInfoExecutable
	oldCache := embeddedIdentities
	embeddedIdentities = nil
	embeddedRuntimeInfoExecutable = func() (string, bool) { return executable, true }
	t.Cleanup(func() {
		embeddedRuntimeInfoExecutable = oldExecutable
		embeddedIdentities = oldCache
	})
	for _, region := range []Region{RegionCN, RegionGlobal, RegionCN, RegionGlobal} {
		got := LocalMachineIdentity(region)
		want := "fixture-0"
		if region == RegionGlobal {
			want = "fixture-3"
		}
		if got.Token != want {
			t.Fatalf("region %s reused wrong identity: %q want %q", region, got.Token, want)
		}
	}
	// Cached identities stay usable without relaunching the component.
	if err := os.Remove(executable); err != nil {
		t.Fatal(err)
	}
	for _, region := range []Region{RegionCN, RegionGlobal} {
		got := LocalMachineIdentity(region)
		want := "fixture-0"
		if region == RegionGlobal {
			want = "fixture-3"
		}
		if got.Token != want {
			t.Fatalf("region %s lost cached identity: %q", region, got.Token)
		}
	}
}
