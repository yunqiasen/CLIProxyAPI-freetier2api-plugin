package admin

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

// Exercise the actual page through DOM events and the external fetch boundary.
// The fixture uses Node's standard library only, with no test-only page exports.
func TestConsoleModelProbeDOM(t *testing.T) {
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("node not installed")
	}
	pagePath := filepath.Join(t.TempDir(), "console.html")
	if err := os.WriteFile(pagePath, []byte(Page("/plugins/freetier2api")), 0600); err != nil {
		t.Fatal(err)
	}
	for _, cache := range []string{"warm", "cold"} {
		t.Run(cache, func(t *testing.T) {
			out, err := exec.Command(node, "testdata/console_probe.cjs", pagePath, cache).CombinedOutput()
			if err != nil {
				t.Fatalf("console probe DOM: %v\n%s", err, out)
			}
			t.Log(string(out))
		})
	}

}
