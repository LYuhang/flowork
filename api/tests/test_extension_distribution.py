"""Native extension downloads must match the Settings URL and container aliases."""

from pathlib import Path
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[2]


def test_native_extension_publication_serves_both_names_in_both_modes(tmp_path):
    source = tmp_path / "extension" / "flowork-extension.zip"
    source.parent.mkdir()
    runtime_dist = tmp_path / "runtime" / "web-dist"
    runtime_dist.mkdir(parents=True)
    (runtime_dist / "index.html").write_text("existing application")
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("manifest.json", '{"manifest_version":3}')
        archive.writestr("service-worker.js", "// current browser bridge")
    launcher = (ROOT / "launch.sh").read_text()
    function = launcher.split("publish_extension_archive() {", 1)[1].split(
        "\nstart_stack() {", 1
    )[0]
    subprocess.run(
        ["bash", "-c", 'set -eu; umask 077; REPO_ROOT="$1"; NATIVE_RUNTIME_DIR="$1/runtime"; '
         + "publish_extension_archive() {" + function + "\npublish_extension_archive",
         "extension-publication-test", str(tmp_path)],
        check=True, capture_output=True, text=True,
    )
    for output in (tmp_path / "web" / "public", tmp_path / "web" / "dist", runtime_dist):
        for name in ("flowork-extension.zip", "vibecanvas-extension.zip"):
            target = output / "downloads" / name
            assert target.read_bytes() == source.read_bytes()
            assert target.stat().st_mode & 0o777 == 0o644
            with zipfile.ZipFile(target) as archive:
                assert archive.testzip() is None
    assert (runtime_dist / "index.html").read_text() == "existing application"


def test_extension_only_rebuild_does_not_restart_the_application():
    launcher = (ROOT / "launch.sh").read_text()
    branch = launcher.split("\n  extension)", 1)[1].split(";;", 1)[0]
    assert "build_extension" in branch
    assert "publish_extension_archive" in branch
    assert "stop_stack" not in branch
    assert "start_stack" not in branch
    assert 'extension_archive="$extension_dir/flowork-extension.zip"' in launcher
