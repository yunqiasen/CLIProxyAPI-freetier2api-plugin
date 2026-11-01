"""Exercise the immutable builder with real git and a local Docker fixture."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

try:
    from scripts.local_delivery.build import DockerBuilder
except ModuleNotFoundError:
    DockerBuilder = None


IMAGE = "golang@sha256:" + "a" * 64
DOCKER_FIXTURE = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import struct
import sys

args = sys.argv[1:]
with open(os.environ["DOCKER_CALLS"], "a") as calls:
    calls.write(json.dumps(args) + "\n")
if args[:2] == ["image", "inspect"]:
    print(json.dumps({"Os": "linux", "Architecture": os.getenv("IMAGE_ARCH", "amd64"),
                      "Id": "sha256:" + "a" * 64}))
    sys.exit(int(os.getenv("INSPECT_EXIT", "0")))
if args[:2] == ["rm", "-f"]:
    sys.exit(0)
if args[0] != "run":
    raise SystemExit("unexpected docker command")
print("all Go tests passed", flush=True)
if os.getenv("BUILD_EXIT"):
    print("fixture build/test failure", flush=True)
    sys.exit(int(os.environ["BUILD_EXIT"]))
mounts = [args[i+1] for i, arg in enumerate(args) if arg == "-v"]
out = Path(next(m for m in mounts if m.endswith(":/out")).removesuffix(":/out"))
artifact = out / args[-1]
header = bytearray(64)
header[:7] = b"\x7fELF\x02\x01\x01"
struct.pack_into("<HHI", header, 16, 3, 183 if os.getenv("IMAGE_ARCH") == "arm64" else 62, 1)
struct.pack_into("<H", header, 52, 64)
if os.getenv("BAD_ELF"):
    header[:4] = b"oops"
if os.getenv("BAD_ARCH"):
    struct.pack_into("<H", header, 18, 183)
artifact.write_bytes(header + b"fixture shared object " + args[-2].encode())
(out / "symbols.txt").write_text("\n".join(
    f"   {i}: 0000000000010000 20 FUNC GLOBAL DEFAULT {'UND' if os.getenv('BAD_SYMBOL') else '13'} {symbol}"
    for i, symbol in enumerate(("cliproxy_plugin_init", "cliproxyPluginCall",
                               "cliproxyPluginFree", "cliproxyPluginShutdown"), 1)))
print("shared object built", flush=True)
'''


class DockerBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="plugin builder ")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        app_dir = self.repo / "internal/app"
        app_dir.mkdir(parents=True)
        (app_dir / "app.go").write_text('package app\nconst DefaultVersion = "0.1.23"\n')
        (self.repo / "internal/admin").mkdir()
        (self.repo / "internal/admin/console.html").write_bytes(
            b'<html>__BASE__/status<script>var BASE = "__BASE__";</script></html>\r\n')
        (self.repo / "go.mod").write_text("module fixture\n\ngo 1.22\n")
        (self.repo / "main.go").write_text("package main\n")
        (self.repo / ".gitignore").write_text(".env\nauths/\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(DOCKER_FIXTURE)
        docker.chmod(0o755)
        self.calls_path = self.root / "docker-calls.jsonl"
        self.env = patch.dict(os.environ, {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "DOCKER_CALLS": str(self.calls_path),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for path, directories, _files in os.walk(self.root):
            Path(path).chmod(0o755)
            for name in directories:
                child = Path(path) / name
                if not child.is_symlink():
                    child.chmod(0o755)
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def builder(self, **kwargs):
        self.assertIsNotNone(DockerBuilder, "DockerBuilder implementation is missing")
        return DockerBuilder(IMAGE, **kwargs)

    def calls(self):
        return [json.loads(line) for line in self.calls_path.read_text().splitlines()]

    def test_builds_committed_snapshot_without_dirty_or_private_files(self):
        (self.repo / "internal/app/app.go").write_text('package app\nconst DefaultVersion = "9.9.9"\n')
        (self.repo / ".env").write_text("TOKEN=private-token")
        (self.repo / "untracked.txt").write_text("private note")
        output = self.root / "run one"

        result = self.builder().build(self.repo, self.commit, output)

        version = result["version"]
        self.assertEqual(set(result), {"commit", "version", "sha256", "artifact", "builder", "platform", "console_sha256"})
        self.assertEqual(result["commit"], self.commit)
        self.assertRegex(version, rf"^0\.1\.23-fork\.{self.commit[:12]}-b\.[0-9a-f]{{12}}$")
        self.assertEqual(result["builder"], IMAGE)
        self.assertEqual(result["platform"], "linux/amd64")
        artifact = Path(result["artifact"])
        self.assertTrue(artifact.is_absolute())
        self.assertEqual(artifact.name, f"freetier2api-v{version}.so")
        self.assertEqual(artifact.parent, output / "dist")
        self.assertEqual(result["sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.assertEqual(json.loads((output / "manifest.json").read_text()), result)
        self.assertTrue((output / "snapshot.tar").is_file())
        snapshot = output / "snapshot"
        self.assertIn('"0.1.23"', (snapshot / "internal/app/app.go").read_text())
        self.assertFalse((snapshot / ".env").exists())
        self.assertFalse((snapshot / "untracked.txt").exists())
        self.assertFalse((snapshot / ".git").exists())
        self.assertEqual(stat.S_IMODE((snapshot / "main.go").stat().st_mode) & 0o222, 0)
        self.assertIn('"9.9.9"', (self.repo / "internal/app/app.go").read_text())
        log = (output / "build.log").read_text()
        self.assertIn("all Go tests passed", log)
        self.assertIn("shared object built", log)
        self.assertNotIn("private-token", log)
        run = next(call for call in self.calls() if call[0] == "run")
        for flag, value in (("--network", "none"), ("--platform", "linux/amd64"),
                            ("--user", f"{os.getuid()}:{os.getgid()}")):
            self.assertEqual(run[run.index(flag) + 1], value)
        self.assertIn(f"{snapshot}:/w:ro", run)
        self.assertIn("CGO_ENABLED=1", run)
        self.assertIn("GOTOOLCHAIN=local", run)
        self.assertIn("GOPROXY=off", run)
        script = run[run.index("-ec") + 1]
        self.assertIn("go test -count=1 -timeout=300s ./...", script)
        self.assertIn("go build -buildmode=c-shared", script)
        self.assertIn("main.pluginVersion=", script)
        self.assertIn("bookworm", script)
        self.assertIn("readelf", script)
        self.assertLess(script.index("go test"), script.index("go build"))
        self.assertEqual(run[-2], version)
        name = run[run.index("--name") + 1]
        self.assertIn(["rm", "-f", name], self.calls())


    def test_rejects_unpinned_or_malformed_image(self):
        self.assertIsNotNone(DockerBuilder, "DockerBuilder implementation is missing")
        for image in ("golang:1.24-bookworm", "golang@sha256:short", "sha256:" + "z" * 64,
                      "--privileged@sha256:" + "a" * 64):
            with self.subTest(image=image), self.assertRaises(RuntimeError):
                DockerBuilder(image)
        self.assertFalse(self.calls_path.exists())

    def test_rejects_platform_mismatch_before_starting_container(self):
        with patch.dict(os.environ, {"IMAGE_ARCH": "arm64"}):
            with self.assertRaisesRegex(RuntimeError, "platform"):
                self.builder().build(self.repo, self.commit, self.root / "mismatch")
        self.assertFalse(any(call[0] == "run" for call in self.calls()))

    def test_unavailable_image_is_not_pulled(self):
        with patch.dict(os.environ, {"INSPECT_EXIT": "1"}):
            with self.assertRaises(RuntimeError):
                self.builder().build(self.repo, self.commit, self.root / "unavailable")
        self.assertFalse(any(call[0] in ("run", "pull") for call in self.calls()))

    def test_existing_output_is_untouched(self):
        output = self.root / "already there"
        output.mkdir()
        marker = output / "installed.so"
        marker.write_bytes(b"existing plugin")
        with self.assertRaises(RuntimeError):
            self.builder().build(self.repo, self.commit, output)
        self.assertEqual(marker.read_bytes(), b"existing plugin")
        self.assertEqual(list(output.iterdir()), [marker])
        self.assertFalse(self.calls_path.exists())

    def test_ref_and_option_injection_are_rejected(self):
        for commit in ("HEAD", "--output=somewhere", self.commit + "; touch PWNED"):
            with self.subTest(commit=commit), self.assertRaises(RuntimeError):
                self.builder().build(self.repo, commit, self.root / "invalid-ref")
        self.assertFalse(self.calls_path.exists())
        self.assertFalse((self.root / "invalid-ref").exists())

    def test_unknown_commit_is_rejected(self):
        with self.assertRaises(RuntimeError):
            self.builder().build(self.repo, "b" * 40, self.root / "unknown")
        self.assertFalse(self.calls_path.exists())

    def test_failed_test_or_build_has_log_and_no_manifest(self):
        output = self.root / "failed"
        with patch.dict(os.environ, {"BUILD_EXIT": "2"}):
            with self.assertRaises(RuntimeError) as caught:
                self.builder().build(self.repo, self.commit, output)
        self.assertIn("fixture build/test failure", (output / "build.log").read_text())
        self.assertNotIn("fixture build/test failure", str(caught.exception))
        self.assertIn(str(output / "build.log"), str(caught.exception))
        self.assertFalse((output / "manifest.json").exists())
        run = next(call for call in self.calls() if call[0] == "run")
        self.assertIn(["rm", "-f", run[run.index("--name") + 1]], self.calls())

    def test_interrupt_removes_only_named_build_container(self):
        output = self.root / "interrupted"
        invocations = []

        def interrupt(argv, **kwargs):
            invocations.append(argv)
            if argv[:2] == ["docker", "run"]:
                raise KeyboardInterrupt()
            return subprocess.run(argv, **kwargs)

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.builder(run=interrupt).build(self.repo, self.commit, output)
        run = next(call for call in invocations if call[:2] == ["docker", "run"])
        container_name = run[run.index("--name") + 1]
        self.assertIn(["docker", "rm", "-f", container_name], invocations)
        self.assertTrue(container_name.startswith("freetier2api-build-"))
        self.assertFalse((output / "manifest.json").exists())

    def test_rejects_invalid_elf_or_wrong_architecture(self):
        for setting in ("BAD_ELF", "BAD_ARCH"):
            with self.subTest(setting=setting), patch.dict(os.environ, {setting: "1"}):
                output = self.root / setting
                with self.assertRaisesRegex(RuntimeError, "ELF"):
                    self.builder().build(self.repo, self.commit, output)
                self.assertFalse((output / "manifest.json").exists())

    def test_rejects_undefined_plugin_symbols(self):
        output = self.root / "bad-symbols"
        with patch.dict(os.environ, {"BAD_SYMBOL": "1"}):
            with self.assertRaisesRegex(RuntimeError, "symbol"):
                self.builder().build(self.repo, self.commit, output)
        self.assertFalse((output / "manifest.json").exists())

    def test_rejects_missing_exact_default_version(self):
        (self.repo / "internal/app/app.go").write_text('package app\nconst OtherVersion = "0.1.23"\n')
        self.git("add", "internal/app/app.go")
        self.git("commit", "-qm", "missing version")
        commit = self.git("rev-parse", "HEAD").strip()
        with self.assertRaisesRegex(RuntimeError, "DefaultVersion"):
            self.builder().build(self.repo, commit, self.root / "missing-version")
        self.assertFalse(any(call[0] == "run" for call in self.calls()))

    def test_snapshot_symlinks_are_rejected_without_reading_external_files(self):
        private = self.root / "external-private.txt"
        private.write_text("fixture secret")
        (self.repo / "linked-private").symlink_to(private)
        self.git("add", "linked-private")
        self.git("commit", "-qm", "external link")
        commit = self.git("rev-parse", "HEAD").strip()
        output = self.root / "bad-link"
        with self.assertRaisesRegex(RuntimeError, "snapshot"):
            self.builder().build(self.repo, commit, output)
        self.assertEqual(private.read_text(), "fixture secret")
        self.assertFalse((output / "snapshot/linked-private").exists())

    def test_repeated_commit_uses_separate_output_and_container_names(self):
        first = self.builder().build(self.repo, self.commit, self.root / "first")
        second = self.builder().build(self.repo, self.commit, self.root / "second")
        self.assertNotEqual(first["artifact"], second["artifact"])
        self.assertNotEqual(first["version"], second["version"])
        self.assertNotEqual(Path(first["artifact"]).name, Path(second["artifact"]).name)
        self.assertNotEqual(first["sha256"], second["sha256"])
        self.assertTrue(Path(first["artifact"]).is_file())
        names = [call[call.index("--name") + 1] for call in self.calls() if call[0] == "run"]
        self.assertEqual(len(set(names)), 2)


    def test_image_id_and_arm64_platform_are_supported(self):
        self.assertIsNotNone(DockerBuilder, "DockerBuilder implementation is missing")
        image = "sha256:" + "a" * 64
        with patch.dict(os.environ, {"IMAGE_ARCH": "arm64"}):
            result = DockerBuilder(image, platform="linux/arm64").build(
                self.repo, self.commit, self.root / "arm64")
        self.assertEqual(result["platform"], "linux/arm64")
        self.assertEqual(result["builder"], image)

    def test_rejects_output_symlink_without_touching_target(self):
        target = self.root / "installed"
        target.mkdir()
        marker = target / "freetier2api.so"
        marker.write_bytes(b"installed")
        output = self.root / "aliased-run"
        output.symlink_to(target)
        with self.assertRaises(RuntimeError):
            self.builder().build(self.repo, self.commit, output)
        self.assertEqual(list(target.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b"installed")

    def test_validation_failure_is_recorded_in_build_log(self):
        output = self.root / "logged-failure"
        with patch.dict(os.environ, {"BAD_ELF": "1"}):
            with self.assertRaises(RuntimeError):
                self.builder().build(self.repo, self.commit, output)
        self.assertIn("not a compatible 64-bit ELF", (output / "build.log").read_text())


    def test_console_hash_matches_rendered_committed_template_not_dirty_tree(self):
        original = (self.repo / "internal/admin/console.html").read_bytes()
        (self.repo / "internal/admin/console.html").write_bytes(b"dirty private page")
        result = self.builder().build(self.repo, self.commit, self.root / "console")
        rendered = original.replace(b"__BASE__", b"/plugins/freetier2api")
        self.assertIn("console_sha256", result)
        self.assertEqual(result["console_sha256"], hashlib.sha256(rendered).hexdigest())
        self.assertNotEqual(result["console_sha256"], hashlib.sha256(original).hexdigest())
        manifest = json.loads((self.root / "console/manifest.json").read_text())
        self.assertEqual(manifest["console_sha256"], result["console_sha256"])
        self.assertEqual((self.root / "console/snapshot/internal/admin/console.html").read_bytes(), original)

    def test_missing_console_template_stops_before_build(self):
        self.git("rm", "internal/admin/console.html")
        self.git("commit", "-qm", "missing console")
        commit = self.git("rev-parse", "HEAD").strip()
        with self.assertRaisesRegex(RuntimeError, "console"):
            self.builder().build(self.repo, commit, self.root / "missing-console")
        self.assertFalse(any(call[0] == "run" for call in self.calls()))

    def test_artifact_filename_matches_installed_version_convention(self):
        result = self.builder().build(self.repo, self.commit, self.root / "filename")
        self.assertEqual(Path(result["artifact"]).name, f"freetier2api-v{result['version']}.so")


    def test_new_run_log_and_manifest_are_private_even_with_open_umask(self):
        output = self.root / "private-run"
        old_umask = os.umask(0)
        try:
            self.builder().build(self.repo, self.commit, output)
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        for filename in ("build.log", "manifest.json"):
            self.assertEqual(stat.S_IMODE((output / filename).stat().st_mode), 0o600)

    def test_build_timeout_is_bounded_and_removes_only_its_own_container(self):
        output = self.root / "timed-out"
        invocations = []

        def timeout(argv, **kwargs):
            invocations.append((argv, kwargs))
            if argv[:2] == ["docker", "run"]:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", -1))
            return subprocess.run(argv, **kwargs)

        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.builder(run=timeout).build(self.repo, self.commit, output)
        argv, kwargs = next(call for call in invocations if call[0][:2] == ["docker", "run"])
        self.assertEqual(kwargs["timeout"], 900)
        container_name = argv[argv.index("--name") + 1]
        self.assertIn(["docker", "rm", "-f", container_name], [call[0] for call in invocations])
        self.assertIn("timed out", (output / "build.log").read_text())
        self.assertFalse((output / "manifest.json").exists())

    def test_builder_explicitly_overrides_image_entrypoint(self):
        self.builder().build(self.repo, self.commit, self.root / "entrypoint")
        argv = next(call for call in self.calls() if call[0] == "run")
        self.assertIn("--entrypoint", argv)
        self.assertEqual(argv[argv.index("--entrypoint") + 1], "/bin/sh")
        self.assertEqual(argv[argv.index(IMAGE) + 1], "-ec")

    def test_image_inspection_only_logs_nonsecret_identity_fields(self):
        self.builder().build(self.repo, self.commit, self.root / "image-log")
        argv = next(call for call in self.calls() if call[:2] == ["image", "inspect"])
        template = argv[argv.index("--format") + 1]
        self.assertNotEqual(template, "{{json .}}")
        for field in (".Id", ".Os", ".Architecture"):
            self.assertIn(field, template)
        self.assertNotIn(".Config", template)

    def test_tool_start_failure_logs_error_kind_without_exception_secrets(self):
        output = self.root / "tool-failure"

        def missing(argv, **kwargs):
            if argv[:2] == ["docker", "image"]:
                raise FileNotFoundError("fixture-private-value")
            return subprocess.run(argv, **kwargs)

        with self.assertRaises(RuntimeError) as caught:
            self.builder(run=missing).build(self.repo, self.commit, output)
        log = (output / "build.log").read_text()
        self.assertIn("FileNotFoundError", log)
        self.assertNotIn("fixture-private-value", log + str(caught.exception))


    def test_tests_run_in_writable_copy_but_build_uses_immutable_snapshot(self):
        self.builder().build(self.repo, self.commit, self.root / "test-scratch")
        argv = next(call for call in self.calls() if call[0] == "run")
        script = argv[argv.index("-ec") + 1]
        self.assertIn("cp -R /w/. /cache/test-src/", script)
        self.assertIn("chmod -R u+w /cache/test-src", script)
        self.assertLess(script.index("cd /cache/test-src"), script.index("go test"))
        self.assertLess(script.index("go test"), script.index("cd /w"))
        self.assertLess(script.index("cd /w"), script.index("go build"))


if __name__ == "__main__":
    unittest.main()
