"""Download the pinned verification tools into .tools/ and return their paths.

Each download is checked against the SHA-256 pinned below before it is unpacked.
Run directly to install every tool for the current platform:

    python scripts/tools.py            # prints "tool: path" lines
    python scripts/tools.py --path     # prints a PATH fragment with every tool
"""

import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent / ".tools"

# tool -> version, download URL template, {(os, arch): (archive, sha256)}
PINNED = {
    "helm": (
        "v4.3.1",
        "https://get.helm.sh/{archive}",
        {
            ("linux", "amd64"): ("helm-v4.3.1-linux-amd64.tar.gz", "0403a88ae20913d11b10a2467b50087721fea7581736da6b9d3992f5357fe54b"),
            ("linux", "arm64"): ("helm-v4.3.1-linux-arm64.tar.gz", "8cf8f66ecbc252ac8f7f06cca68aba88508ddfde3a6a34feb1678ad1694b4f88"),
            ("darwin", "amd64"): ("helm-v4.3.1-darwin-amd64.tar.gz", "34bec4eba761ba5c16d1965ee6b8f5b0abe5993974a802274e2a4f0418893d89"),
            ("darwin", "arm64"): ("helm-v4.3.1-darwin-arm64.tar.gz", "6b4e61c8cd27d99e5a801e63ff25dd5c70b36875c2cd659d3e9ce6b76fc23198"),
            ("windows", "amd64"): ("helm-v4.3.1-windows-amd64.zip", "fcfd7b796dcf0f225abc40b5636a3b01f55f3de2fb1dfb18475df94e62c14112"),
        },
    ),
    "kubeconform": (
        "v0.8.0",
        "https://github.com/yannh/kubeconform/releases/download/v0.8.0/{archive}",
        {
            ("linux", "amd64"): ("kubeconform-linux-amd64.tar.gz", "9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883"),
            ("linux", "arm64"): ("kubeconform-linux-arm64.tar.gz", "1f53fc8e81258197a35e8603054162a5af1de8c5af13746c71ab680d9534ed87"),
            ("darwin", "amd64"): ("kubeconform-darwin-amd64.tar.gz", "71dbc87ac9f24099a62b93570e65aa06312ba6ac8aea63b7f86e9d999edf5a92"),
            ("darwin", "arm64"): ("kubeconform-darwin-arm64.tar.gz", "f84f4dfbebf4a6b0b230385fa065a39ea35e02608c2b50d025dcf64775a69d67"),
            ("windows", "amd64"): ("kubeconform-windows-amd64.zip", "e3f56102bcf4f50b034a567e2482a1c5330799983ddd655952310211aef73d93"),
        },
    ),
    "gitleaks": (
        "v8.30.1",
        "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/{archive}",
        {
            ("linux", "amd64"): ("gitleaks_8.30.1_linux_x64.tar.gz", "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
            ("linux", "arm64"): ("gitleaks_8.30.1_linux_arm64.tar.gz", "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"),
            ("darwin", "amd64"): ("gitleaks_8.30.1_darwin_x64.tar.gz", "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"),
            ("darwin", "arm64"): ("gitleaks_8.30.1_darwin_arm64.tar.gz", "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"),
            ("windows", "amd64"): ("gitleaks_8.30.1_windows_x64.zip", "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e"),
        },
    ),
    # Published as a bare executable, not an archive; one minor version of the cluster.
    "kubectl": (
        "v1.37.2",
        "https://dl.k8s.io/release/v1.37.2/bin/{archive}",
        {
            ("linux", "amd64"): ("linux/amd64/kubectl", "c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4"),
            ("linux", "arm64"): ("linux/arm64/kubectl", "fcc16f63186659d9722014b7e02d5fdd2d4e669cb8c0ab66365d7d3b8ba03509"),
            ("darwin", "amd64"): ("darwin/amd64/kubectl", "88379aff5c8111de6be0a394adffba2819e44cde269f3ab23cef76de1a48eaa8"),
            ("darwin", "arm64"): ("darwin/arm64/kubectl", "55701af0d51815ee335f240e48acf0959c3c23017ed3fa816e880c4a92bae581"),
            ("windows", "amd64"): ("windows/amd64/kubectl.exe", "1691938f7de0e931969f9968c3601cf32d6b0d573573afb4ce471f3d28f51984"),
        },
    ),
}


def current_platform():
    os_name = {"linux": "linux", "darwin": "darwin", "win32": "windows"}.get(sys.platform)
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    return os_name, arch


def path(tool):
    """Return the path to the pinned binary, downloading it on first use."""
    version, url, archives = PINNED[tool]
    os_name, arch = current_platform()
    if (os_name, arch) not in archives:
        sys.exit(f"{tool} {version}: no pinned build for {sys.platform}/{platform.machine()}")

    binary = tool + (".exe" if os_name == "windows" else "")
    target = TOOLS_DIR / f"{tool}-{version}" / binary
    if target.exists():
        return str(target)

    archive, expected = archives[(os_name, arch)]
    print(f"Downloading {tool} {version} ({archive})", file=sys.stderr)
    with tempfile.TemporaryDirectory() as tmp:
        download = Path(tmp) / Path(archive).name
        with urllib.request.urlopen(url.format(archive=archive), timeout=60) as response:
            download.write_bytes(response.read())

        actual = hashlib.sha256(download.read_bytes()).hexdigest()
        if actual != expected:
            sys.exit(f"{tool} {version}: checksum mismatch for {archive}\n  expected {expected}\n  actual   {actual}")

        unpacked = Path(tmp) / "unpacked"
        if archive.endswith(".zip"):
            with zipfile.ZipFile(download) as z:
                z.extractall(unpacked)
        elif archive.endswith(".tar.gz"):
            with tarfile.open(download) as t:
                t.extractall(unpacked, filter="data")
        else:
            unpacked.mkdir()
            download.rename(unpacked / binary)

        found = next(unpacked.rglob(binary), None)
        if found is None:
            sys.exit(f"{tool} {version}: {binary} not found in {archive}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(found, target)
        target.chmod(0o755)
    return str(target)


if __name__ == "__main__":
    if sys.argv[1:] == ["--path"]:
        print(os.pathsep.join(str(Path(path(name)).parent) for name in PINNED))
    else:
        for name in PINNED:
            print(f"{name}: {path(name)}")
