"""Build one committed plugin snapshot without touching installed plugins.

Only the Python standard library, git and an already-pulled Docker image are
required on the host. Build/test processes receive no network or host secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import struct
import subprocess
import tarfile
from typing import Callable, TextIO
import uuid


_DIGEST_IMAGE = re.compile(r"(?:[a-z0-9][a-zA-Z0-9._:/-]*@)?sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_DEFAULT_VERSION = re.compile(
    r'^\s*const\s+DefaultVersion\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)"'
    r'\s*(?://[^\n]*)?$', re.MULTILINE,
)
_BUILD_TIMEOUT = 900
_INSPECT_FORMAT = '{"Id":{{json .Id}},"Os":{{json .Os}},"Architecture":{{json .Architecture}}}'
_MACHINES = {"linux/amd64": 62, "linux/arm64": 183}
_SYMBOLS = {"cliproxy_plugin_init", "cliproxyPluginCall", "cliproxyPluginFree", "cliproxyPluginShutdown"}
_BUILD_SCRIPT = r'''set -eu
. /etc/os-release
if [ "${VERSION_CODENAME:-}" != bookworm ]; then
    echo "builder must use Debian bookworm" >&2
    exit 1
fi
go version
gcc --version
# Some integration fixtures intentionally write into their package directory.
# Give tests a disposable copy, then compile from the untouched snapshot.
mkdir /cache/test-src
cp -R /w/. /cache/test-src/
chmod -R u+w /cache/test-src
cd /cache/test-src
go test -count=1 -timeout=300s ./...
cd /w
go build -buildmode=c-shared -trimpath -ldflags "-s -w -X main.pluginVersion=$1" -o "/out/$2" .
readelf --dyn-syms --wide "/out/$2" > /out/symbols.txt
cat /out/symbols.txt
'''


def _private_text(path: Path) -> TextIO:
    return open(path, "x", encoding="utf-8",
                opener=lambda name, flags: os.open(name, flags, 0o600))


class DockerBuilder:
    """Produce a versioned .so plus snapshot, log and manifest in a new directory.

    ``run`` is an optional subprocess.run-compatible executor. Errors (including
    cancellation) are RuntimeError; details remain in the run's build.log.
    """

    def __init__(self, image: str, platform: str = "linux/amd64", *, run: Callable = subprocess.run):
        if not _DIGEST_IMAGE.fullmatch(image):
            raise RuntimeError("builder image must be pinned to a full sha256 digest")
        if platform not in _MACHINES:
            raise RuntimeError("builder platform must be linux/amd64 or linux/arm64")
        self.image = image
        self.platform = platform
        self.run = run

    def build(self, repo: Path, commit: str, output: Path) -> dict:
        if not _COMMIT.fullmatch(commit):
            raise RuntimeError("build commit must be a full 40- or 64-character git object ID")
        output = Path(output).absolute()
        # Never resolve an existing output symlink into somebody else's directory.
        try:
            output.mkdir(parents=True, exist_ok=False, mode=0o700)
        except OSError:
            raise RuntimeError("build output must be a new run directory") from None
        output = output.resolve()
        log_path = output / "build.log"
        with _private_text(log_path) as log:
            try:
                repo = Path(repo).resolve(strict=True)
                resolved = self._command(
                    ["git", "-C", str(repo), "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"],
                    log, capture=True,
                ).strip()
                if not _COMMIT.fullmatch(resolved) or resolved.lower() != commit.lower():
                    raise RuntimeError("git did not resolve the requested commit exactly")
                image = json.loads(self._command(
                    ["docker", "image", "inspect", "--format", _INSPECT_FORMAT, self.image],
                    log, capture=True,
                ))
                if not isinstance(image, dict) or f"{image.get('Os')}/{image.get('Architecture')}" != self.platform:
                    raise RuntimeError("builder image platform does not match requested platform")
                snapshot = self._snapshot(repo, resolved, output, log)
                matches = _DEFAULT_VERSION.findall((snapshot / "internal/app/app.go").read_text(encoding="utf-8"))
                if len(matches) != 1:
                    raise RuntimeError("snapshot must contain exactly one const DefaultVersion")
                # Go shared libraries must receive a new pathname and metadata
                # generation on every build, including retries of one commit.
                version = f"{matches[0]}-fork.{resolved[:12]}-b.{uuid.uuid4().hex[:12]}"
                console = snapshot / "internal/admin/console.html"
                if not console.is_file():
                    raise RuntimeError("snapshot is missing the console template")
                console_sha256 = hashlib.sha256(
                    console.read_bytes().replace(b"__BASE__", b"/plugins/freetier2api")
                ).hexdigest()
                dist = output / "dist"
                dist.mkdir()
                cache = output / "cache"
                for part in ("home", "go-build", "go-mod", "tmp", "go"):
                    (cache / part).mkdir(parents=True)
                filename = f"freetier2api-v{version}.so"
                self._build_container(snapshot, dist, cache, version, filename, log)
                artifact = dist / filename
                self._validate_artifact(artifact, dist / "symbols.txt")
                digest = hashlib.sha256()
                with artifact.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                result = {
                    "commit": resolved,
                    "version": version,
                    "sha256": digest.hexdigest(),
                    "artifact": str(artifact),
                    "builder": self.image,
                    "platform": self.platform,
                    "console_sha256": console_sha256,
                }
                with _private_text(output / "manifest.json") as manifest:
                    manifest.write(json.dumps(result, indent=2) + "\n")
                return result
            except KeyboardInterrupt:
                log.write("build cancelled\n")
                raise RuntimeError(f"build cancelled; see {log_path}") from None
            except RuntimeError as error:
                log.write(f"build error: {error}\n")
                raise
            except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as error:
                # Record the error kind, not arbitrary exception text that may
                # contain tool credentials or private environment values.
                log.write(f"build failed: {type(error).__name__}\n")
                raise RuntimeError(f"build failed; see {log_path}") from None

    def _command(self, argv: list[str], log: TextIO, *, capture: bool = False,
                 timeout: float | None = None) -> str:
        log.write("$ " + shlex.join(argv) + "\n")
        log.flush()
        result = self.run(argv, stdout=subprocess.PIPE if capture else log,
                          stderr=subprocess.STDOUT, text=True, check=False, timeout=timeout)
        if capture:
            log.write(result.stdout or "")
            log.flush()
        if result.returncode:
            raise RuntimeError(f"{argv[0]} command failed (exit {result.returncode}); see {log.name}")
        return (result.stdout or "") if capture else ""

    def _snapshot(self, repo: Path, commit: str, output: Path, log: TextIO) -> Path:
        archive = output / "snapshot.tar"
        self._command(["git", "-C", str(repo), "archive", "--format=tar", f"--output={archive}", commit], log)
        snapshot = output / "snapshot"
        snapshot.mkdir()
        # Extract only ordinary files/directories. A tracked symlink must never
        # pull a credential file (or any host path) into the build's view.
        with tarfile.open(archive, "r:") as source:
            for entry in source:
                name = PurePosixPath(entry.name)
                if name.is_absolute() or ".." in name.parts or not name.parts:
                    raise RuntimeError("invalid path in git snapshot")
                destination = snapshot.joinpath(*name.parts)
                if entry.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif entry.isfile():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    data = source.extractfile(entry)
                    if data is None:
                        raise RuntimeError("invalid file in git snapshot")
                    with data, destination.open("xb") as target:
                        shutil.copyfileobj(data, target)
                    destination.chmod(0o555 if entry.mode & 0o111 else 0o444)
                else:
                    raise RuntimeError("git snapshot contains unsupported link or special file")
        for directory, _dirs, _files in os.walk(snapshot, topdown=False):
            Path(directory).chmod(0o555)
        archive.chmod(0o444)
        return snapshot

    def _build_container(self, snapshot: Path, dist: Path, cache: Path,
                         version: str, filename: str, log: TextIO) -> None:
        name = "freetier2api-build-" + uuid.uuid4().hex
        argv = ["docker", "run", "--rm", "--name", name, "--network", "none", "--platform", self.platform,
                "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "/bin/sh",
                "-v", f"{snapshot}:/w:ro", "-v", f"{dist}:/out", "-v", f"{cache}:/cache", "-w", "/w"]
        for value in ("CGO_ENABLED=1", "GOFLAGS=-buildvcs=false", "GOTOOLCHAIN=local", "GOPROXY=off",
                      "GOSUMDB=off", "HOME=/cache/home", "GOCACHE=/cache/go-build", "GOMODCACHE=/cache/go-mod",
                      "GOPATH=/cache/go", "TMPDIR=/cache/tmp"):
            argv.extend(["-e", value])
        argv.extend([self.image, "-ec", _BUILD_SCRIPT, "local-delivery", version, filename])
        try:
            self._command(argv, log, timeout=_BUILD_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"build timed out after {_BUILD_TIMEOUT}s; see {log.name}") from None
        finally:
            # subprocess.run terminates its Docker CLI child on interruption,
            # but Docker's daemon may still be running that named container.
            log.write("$ docker rm -f " + name + "\n")
            log.flush()
            try:
                cleaned = self.run(["docker", "rm", "-f", name], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, check=False, timeout=30)
                log.write(cleaned.stdout or "")
                if cleaned.returncode and "No such container" not in (cleaned.stdout or ""):
                    raise RuntimeError(f"build container cleanup failed; see {log.name}")
            except (OSError, subprocess.SubprocessError):
                raise RuntimeError(f"build container cleanup failed; see {log.name}") from None

    def _validate_artifact(self, artifact: Path, symbols_path: Path) -> None:
        if artifact.is_symlink() or not artifact.is_file():
            raise RuntimeError("build produced no regular ELF artifact")
        with artifact.open("rb") as source:
            header = source.read(64)
        if (len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01"
                or struct.unpack_from("<HH", header, 16) != (3, _MACHINES[self.platform])):
            raise RuntimeError("build artifact is not a compatible 64-bit ELF shared object")
        if symbols_path.is_symlink():
            raise RuntimeError("invalid plugin symbol report")
        symbols = set(re.findall(
            r"^\s*\d+:\s+\S+\s+\d+\s+FUNC\s+GLOBAL\s+DEFAULT\s+(?!UND\b)\S+\s+(\w+)\s*$",
            symbols_path.read_text(encoding="utf-8"), re.MULTILINE,
        ))
        if not _SYMBOLS <= symbols:
            raise RuntimeError("build artifact is missing exported plugin ABI symbols")
