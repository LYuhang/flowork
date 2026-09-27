"""Unit tests for the pure, host-side-testable file-op executor.

``run_fileop(op, roots)`` runs ONE file op against the filesystem, contained to
``roots``. In production it runs inside the sandbox's serve loop; here a
``tmp_path`` directory stands in for the sandbox mount root. No gVisor, no
sandbox boot, no async — plain pytest asserting the real return dicts.
"""

import json
import os
import sys
import threading
import time

from vibecanvas_api.sandbox_entry import serve_loop_parallel, serve_once_api
from vibecanvas_api.services.sandbox.fileops import run_fileop
from vibecanvas_api.services.sandbox.gvisor import _workflow_python_env


def test_exec_uses_selected_python_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/unrelated/host/tools")
    env = _workflow_python_env()
    assert env["PATH"].split(os.pathsep)[0] == os.path.dirname(sys.executable)
    assert "/unrelated/host/tools" not in env["PATH"]
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    result = run_fileop(
        {"op": "exec", "command": "python -c 'import sys; print(sys.prefix)'"},
        [str(tmp_path)],
    )
    assert result["exit_code"] == 0, result
    assert result["stdout"].strip() == sys.prefix


def test_read_text(tmp_path):
    p = tmp_path / "hello.txt"
    p.write_text("line one\nline two\n", encoding="utf-8")

    res = run_fileop({"op": "read", "path": str(p)}, [str(tmp_path)])

    assert res == {"ok": True, "kind": "text", "content": "line one\nline two\n"}


def test_read_binary_sniffs_null_byte(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"PNG\x00\x01\x02\x03binary-ish")

    res = run_fileop({"op": "read", "path": str(p)}, [str(tmp_path)])

    assert res["ok"] is True
    assert res["kind"] == "binary"
    assert res["size"] == len(b"PNG\x00\x01\x02\x03binary-ish")
    assert isinstance(res["content_type"], str) and res["content_type"]
    # Binary reads must NOT leak file bytes back as content.
    assert "content" not in res


def test_write_then_read_roundtrip(tmp_path):
    target = tmp_path / "sub" / "dir" / "out.txt"
    body = "café\nμlibre\n"

    w = run_fileop(
        {"op": "write", "path": str(target), "content": body}, [str(tmp_path)]
    )
    assert w == {"ok": True, "bytes": len(body.encode("utf-8"))}
    assert target.read_text(encoding="utf-8") == body

    r = run_fileop({"op": "read", "path": str(target)}, [str(tmp_path)])
    assert r == {"ok": True, "kind": "text", "content": body}


def test_read_literal_path_with_shell_glob_chars(tmp_path):
    target = tmp_path / "PBR_MP[GB].md"
    target.write_text("# report\n", encoding="utf-8")

    res = run_fileop({"op": "read", "path": str(target)}, [str(tmp_path)])

    assert res == {"ok": True, "kind": "text", "content": "# report\n"}


def test_list_one_level_sorted(tmp_path):
    (tmp_path / "b.txt").write_text("bb", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "zdir").mkdir()
    # A nested file that must NOT appear (one level only).
    (tmp_path / "zdir" / "deep.txt").write_text("x", encoding="utf-8")

    res = run_fileop({"op": "list", "path": str(tmp_path)}, [str(tmp_path)])

    assert res["ok"] is True
    names = [e["name"] for e in res["entries"]]
    assert names == ["a.txt", "b.txt", "zdir"]
    by_name = {e["name"]: e for e in res["entries"]}
    assert by_name["a.txt"]["is_dir"] is False
    assert by_name["a.txt"]["size"] == 1
    assert by_name["zdir"]["is_dir"] is True


def test_list_not_a_directory(tmp_path):
    f = tmp_path / "afile.txt"
    f.write_text("x", encoding="utf-8")

    res = run_fileop({"op": "list", "path": str(f)}, [str(tmp_path)])
    assert res == {"ok": False, "error": "not_a_directory"}


def test_grep_matches_and_skips_binary(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("alpha\nNEEDLE here\nomega\n", encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text("nothing\nNEEDLE again\n", encoding="utf-8")
    blob = tmp_path / "blob.bin"
    blob.write_bytes(b"NEEDLE\x00inside binary")

    res = run_fileop(
        {"op": "grep", "pattern": "NEEDLE", "path": str(tmp_path)}, [str(tmp_path)]
    )

    assert res["ok"] is True
    matches = res["matches"]
    # ripgrep-style "<abs_path>:<lineno>:<line>"
    assert f"{a}:2:NEEDLE here" in matches
    assert f"{b}:2:NEEDLE again" in matches
    # The binary file must be skipped entirely (NUL sniff).
    assert not any(str(blob) in m for m in matches)


def test_grep_invalid_regex(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    res = run_fileop(
        {"op": "grep", "pattern": "(", "path": str(tmp_path)}, [str(tmp_path)]
    )
    assert res == {"ok": False, "error": "invalid_regex"}


def test_path_outside_roots_absolute(tmp_path):
    # An absolute path entirely outside the root.
    res = run_fileop({"op": "read", "path": "/etc/passwd"}, [str(tmp_path)])
    assert res == {"ok": False, "error": "path_outside_roots"}


def test_path_outside_roots_dotdot_escape(tmp_path):
    sub = tmp_path / "inner"
    sub.mkdir()
    escape = os.path.join(str(sub), "..", "..", "escapee.txt")
    res = run_fileop({"op": "read", "path": escape}, [str(sub)])
    assert res == {"ok": False, "error": "path_outside_roots"}


def test_read_symlink_escape_rejected(tmp_path):
    # A symlink INSIDE the root pointing OUTSIDE must be rejected: realpath
    # resolves the link before containment, so the secret is never read.
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("SECRET", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    link = root / "link.txt"
    os.symlink(str(outside), str(link))

    res = run_fileop({"op": "read", "path": str(link)}, [str(root)])
    assert res == {"ok": False, "error": "path_outside_roots"}


def test_write_symlink_escape_rejected(tmp_path):
    # A NOT-yet-existing file under a symlinked dir: realpath resolves the
    # parent symlink so the new file's path lands outside the root → rejected
    # (no file is created outside).
    outside_dir = tmp_path.parent / "outside_dir"
    outside_dir.mkdir()
    root = tmp_path / "root2"
    root.mkdir()
    linkdir = root / "ld"
    os.symlink(str(outside_dir), str(linkdir))

    res = run_fileop(
        {"op": "write", "path": str(linkdir / "x.txt"), "content": "hi"},
        [str(root)],
    )
    assert res == {"ok": False, "error": "path_outside_roots"}
    # And nothing was written through the escaping link.
    assert not (outside_dir / "x.txt").exists()


def test_read_missing_file_not_found(tmp_path):
    res = run_fileop(
        {"op": "read", "path": str(tmp_path / "nope.txt")}, [str(tmp_path)]
    )
    assert res == {"ok": False, "error": "not_found"}


def test_exec_runs_shell_command(tmp_path):
    res = run_fileop(
        {"op": "exec", "command": "echo hello-exec"}, [str(tmp_path)]
    )
    assert res["ok"] is True
    assert res["exit_code"] == 0
    assert res["stdout"].strip() == "hello-exec"
    assert res["stderr"] == ""


def test_exec_uses_bash_semantics(tmp_path):
    res = run_fileop(
        {"op": "exec", "command": "[[ -n ${BASH_VERSION:-} ]] && echo bash"},
        [str(tmp_path)],
    )
    assert res["ok"] is True
    assert res["exit_code"] == 0
    assert res["stdout"].strip() == "bash"
    assert res["stderr"] == ""


def test_exec_captures_stderr_like_terminal(tmp_path):
    res = run_fileop(
        {"op": "exec", "command": "__vibecanvas_missing_command__"},
        [str(tmp_path)],
    )
    assert res["ok"] is True
    assert res["exit_code"] != 0
    assert res["stdout"] == ""
    assert "__vibecanvas_missing_command__" in res["stderr"]


# --- serve-loop kind:"fileop" dispatch (host-side, no gVisor) ---------------
#
# ``serve_once_api`` is pure-Python file IO: it globs inbox/*.ready, claims via
# atomic rename, reads the job json, and for a ``kind:"fileop"`` job runs the op
# inside ``run_fileop`` and writes the result to the outbox before the .done
# marker. No tenant/run_id, no sandbox boot needed — a tmp work_dir + runs_root
# stand in for the in-sandbox mounts.


def _seed_channel(tmp_path):
    """Create work_dir(inbox/outbox) + runs_root; return (work_dir, runs_root)."""
    work_dir = tmp_path / "work"
    (work_dir / "inbox").mkdir(parents=True)
    (work_dir / "outbox").mkdir(parents=True)
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    return str(work_dir), str(runs_root)


def _write_job(work_dir, job_id, job):
    """Write the job json then the .ready claim gate (LAST, as the host does)."""
    inbox = os.path.join(work_dir, "inbox")
    with open(os.path.join(inbox, f"{job_id}.json"), "w", encoding="utf-8") as f:
        json.dump(job, f)
    # .ready written LAST so the claim gate only opens once the json is present.
    with open(os.path.join(inbox, f"{job_id}.ready"), "w", encoding="utf-8") as f:
        f.write("")


def _assert_markers_cleaned(work_dir, job_id):
    inbox = os.path.join(work_dir, "inbox")
    assert not os.path.exists(os.path.join(inbox, f"{job_id}.json"))
    assert not os.path.exists(os.path.join(inbox, f"{job_id}.taken"))
    # .done present in outbox.
    assert os.path.exists(os.path.join(work_dir, "outbox", f"{job_id}.done"))


def test_serve_fileop_read(tmp_path):
    work_dir, runs_root = _seed_channel(tmp_path)
    seeded = os.path.join(runs_root, "note.txt")
    with open(seeded, "w", encoding="utf-8") as f:
        f.write("hi there\n")

    job_id = "job-read-1"
    _write_job(work_dir, job_id,
               {"kind": "fileop", "op": {"op": "read", "path": seeded}})

    assert serve_once_api(work_dir, runs_root) == job_id

    res_path = os.path.join(work_dir, "outbox", f"{job_id}.result.json")
    assert os.path.exists(res_path)
    with open(res_path, "r", encoding="utf-8") as f:
        result = json.load(f)
    assert result == {"ok": True, "kind": "text", "content": "hi there\n"}
    _assert_markers_cleaned(work_dir, job_id)


def test_serve_fileop_write_creates_file(tmp_path):
    work_dir, runs_root = _seed_channel(tmp_path)
    target = os.path.join(runs_root, "sub", "out.txt")
    body = "written-by-serve\n"

    job_id = "job-write-1"
    _write_job(work_dir, job_id,
               {"kind": "fileop",
                "op": {"op": "write", "path": target, "content": body}})

    assert serve_once_api(work_dir, runs_root) == job_id

    res_path = os.path.join(work_dir, "outbox", f"{job_id}.result.json")
    with open(res_path, "r", encoding="utf-8") as f:
        result = json.load(f)
    assert result == {"ok": True, "bytes": len(body.encode("utf-8"))}
    # The op ran inside the loop and actually wrote the file on disk.
    with open(target, "r", encoding="utf-8") as f:
        assert f.read() == body
    _assert_markers_cleaned(work_dir, job_id)


def test_serve_parallel_fileop_jobs_share_one_channel(tmp_path):
    work_dir, runs_root = _seed_channel(tmp_path)
    target_a = os.path.join(runs_root, "a.txt")
    target_b = os.path.join(runs_root, "b.txt")
    loop_thread = threading.Thread(
        target=serve_loop_parallel,
        args=(work_dir, runs_root, 2),
        daemon=True,
    )
    loop_thread.start()
    try:
        _write_job(
            work_dir,
            "job-a",
            {"kind": "fileop", "op": {"op": "write", "path": target_a, "content": "A"}},
        )
        _write_job(
            work_dir,
            "job-b",
            {"kind": "fileop", "op": {"op": "write", "path": target_b, "content": "B"}},
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            done_a = os.path.exists(os.path.join(work_dir, "outbox", "job-a.done"))
            done_b = os.path.exists(os.path.join(work_dir, "outbox", "job-b.done"))
            if done_a and done_b:
                break
            time.sleep(0.01)
        assert os.path.exists(os.path.join(work_dir, "outbox", "job-a.done"))
        assert os.path.exists(os.path.join(work_dir, "outbox", "job-b.done"))
        assert open(target_a, encoding="utf-8").read() == "A"
        assert open(target_b, encoding="utf-8").read() == "B"
    finally:
        open(os.path.join(work_dir, "shutdown"), "w").close()
        loop_thread.join(timeout=2)


def test_serve_fileop_path_escape_rejected(tmp_path):
    work_dir, runs_root = _seed_channel(tmp_path)

    job_id = "job-escape-1"
    _write_job(work_dir, job_id,
               {"kind": "fileop",
                "op": {"op": "read", "path": "/etc/passwd"}})

    assert serve_once_api(work_dir, runs_root) == job_id

    res_path = os.path.join(work_dir, "outbox", f"{job_id}.result.json")
    with open(res_path, "r", encoding="utf-8") as f:
        result = json.load(f)
    assert result == {"ok": False, "error": "path_outside_roots"}
    # Even a rejected op still finishes: .done written, markers cleaned.
    _assert_markers_cleaned(work_dir, job_id)


# --- VIBECANVAS_FILEOP_ROOTS env parse (host-side, no gVisor) ----------------
#
# Task 4b-i defense-in-depth: the serve loop reads its containment roots from the
# VIBECANVAS_FILEOP_ROOTS env var. A malformed value with an empty colon-segment
# ("/data:" or ":/mount") must NOT produce a "" root — an empty root realpaths to
# the worker cwd and would silently re-admit it. The parse filters empties and
# falls back to [runs_root] when the whole env is empty/garbage.


def test_serve_fileop_roots_env_empty_segment_not_widened(tmp_path, monkeypatch):
    """A malformed ``VIBECANVAS_FILEOP_ROOTS="/data:"`` (trailing empty segment)
    must NOT re-admit runs_root: a fileop whose path is under runs_root (but NOT
    under /data) is rejected ``path_outside_roots`` — the "" segment was filtered,
    so runs_root is genuinely outside the configured roots."""
    work_dir, runs_root = _seed_channel(tmp_path)
    # The single configured root is a sibling dir, NOT runs_root.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("VIBECANVAS_FILEOP_ROOTS", f"{data_dir}:")

    # The op path is under runs_root — allowed ONLY if the empty segment wrongly
    # re-admitted runs_root (the bug). With the fix it is outside the roots.
    seeded = os.path.join(runs_root, "note.txt")
    with open(seeded, "w", encoding="utf-8") as f:
        f.write("hi there\n")

    job_id = "job-emptyseg-1"
    _write_job(work_dir, job_id,
               {"kind": "fileop", "op": {"op": "read", "path": seeded}})

    assert serve_once_api(work_dir, runs_root) == job_id

    res_path = os.path.join(work_dir, "outbox", f"{job_id}.result.json")
    with open(res_path, "r", encoding="utf-8") as f:
        result = json.load(f)
    assert result == {"ok": False, "error": "path_outside_roots"}, result
    _assert_markers_cleaned(work_dir, job_id)


def test_serve_fileop_roots_env_empty_falls_back_to_runs_root(tmp_path, monkeypatch):
    """``VIBECANVAS_FILEOP_ROOTS=""`` (all-empty/garbage) falls back to
    ``[runs_root]``: a path under runs_root is allowed (never an empty roots list,
    which would admit nothing)."""
    work_dir, runs_root = _seed_channel(tmp_path)
    monkeypatch.setenv("VIBECANVAS_FILEOP_ROOTS", "")

    seeded = os.path.join(runs_root, "note.txt")
    with open(seeded, "w", encoding="utf-8") as f:
        f.write("hi there\n")

    job_id = "job-emptyenv-1"
    _write_job(work_dir, job_id,
               {"kind": "fileop", "op": {"op": "read", "path": seeded}})

    assert serve_once_api(work_dir, runs_root) == job_id

    res_path = os.path.join(work_dir, "outbox", f"{job_id}.result.json")
    with open(res_path, "r", encoding="utf-8") as f:
        result = json.load(f)
    assert result == {"ok": True, "kind": "text", "content": "hi there\n"}, result
    _assert_markers_cleaned(work_dir, job_id)


# --- @gvisor: host submit_fileop over a REAL warm worker --------------------
#
# The host-side seam (Task 3): ``WarmGvisorPool.submit_fileop`` enqueues a
# ``{"kind":"fileop","op":...}`` job on the warm channel and reads the result
# dict back. This boots a REAL warm worker and proves the op runs INSIDE the
# sandbox (a write op lands on the host via the shared /runs bind, a read sees
# the host-seeded file). The API serve loop is credential-free.

import pytest  # noqa: E402

from vibecanvas_api.services.object_store import (  # noqa: E402
    FilesystemObjectStore,
)
from vibecanvas_api.services.sandbox import _gvisor_runnable  # noqa: E402


def _fileop_pool(tmp_path, monkeypatch):
    """A credential-free warm pool wired to a real gVisor provider."""
    from vibecanvas_api.services.sandbox import _resolve_runsc
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider
    from vibecanvas_api.services.sandbox.warm import WarmGvisorPool

    store_root = str(tmp_path / "store")
    work_root = str(tmp_path / "work")
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.warm.get_object_store",
        lambda: FilesystemObjectStore(root=store_root),
    )
    pool = WarmGvisorPool(
        provider=RootlessGvisorProvider(_resolve_runsc()),
        store_root=store_root,
        work_root=work_root,
    )
    return pool


@pytest.mark.skipif(not _gvisor_runnable(), reason="rootless gVisor not runnable here")
def test_submit_fileop_read_write_roundtrip_in_sandbox(tmp_path, monkeypatch):
    """submit_fileop runs the op INSIDE the warm sandbox: a host-seeded file is
    READ back; a WRITE lands on the host via the shared /runs bind and a
    subsequent READ returns it."""
    pool = _fileop_pool(tmp_path, monkeypatch)
    # Seed a file on the HOST at the real run-root path; in the sandbox that dir
    # is bound at /runs, so /runs/data/x.txt is the same inode.
    seed_dir = os.path.join(pool._runs_root, "data")
    os.makedirs(seed_dir, exist_ok=True)
    with open(os.path.join(seed_dir, "x.txt"), "w", encoding="utf-8") as f:
        f.write("hello")

    pool.start()
    try:
        # READ the host-seeded file (the op path is the IN-SANDBOX path).
        r = pool.submit_fileop({"op": "read", "path": "/runs/data/x.txt"})
        assert r == {"ok": True, "kind": "text", "content": "hello"}, r

        # WRITE a new file INSIDE the sandbox.
        w = pool.submit_fileop(
            {"op": "write", "path": "/runs/data/y.txt", "content": "world"}
        )
        assert w["ok"] is True
        assert w["bytes"] == len("world".encode("utf-8")), w

        # The write happened on the shared mount → READ it back through the
        # sandbox returns the same content.
        r2 = pool.submit_fileop({"op": "read", "path": "/runs/data/y.txt"})
        assert r2 == {"ok": True, "kind": "text", "content": "world"}, r2

        # And the host sees the file on the shared bind at the real path.
        host_y = os.path.join(pool._runs_root, "data", "y.txt")
        with open(host_y, "r", encoding="utf-8") as f:
            assert f.read() == "world"
    finally:
        pool.stop()


# --- @gvisor: file-op worker (Task 4a — NO DB, configurable network) --------
#
# The agent's personal file/exec sandbox must have no database or
# secret env, but its network posture is configurable through SANDBOX_NETWORK.
# Dev defaults to host networking so bash/curl behaves like a normal shell.


def _secure_fileop_pool(tmp_path, monkeypatch):
    """A ``fileops=True`` ``WarmGvisorPool``: no DB/secret env; network
    follows the sandbox config."""
    from vibecanvas_api.services.sandbox import _resolve_runsc
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider
    from vibecanvas_api.services.sandbox.warm import WarmGvisorPool

    store_root = str(tmp_path / "store")
    work_root = str(tmp_path / "work")
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.warm.get_object_store",
        lambda: FilesystemObjectStore(root=store_root),
    )
    return WarmGvisorPool(
        provider=RootlessGvisorProvider(_resolve_runsc()),
        store_root=store_root,
        work_root=work_root,
        fileops=True,
    )


@pytest.mark.skipif(not _gvisor_runnable(), reason="rootless gVisor not runnable here")
def test_secure_fileop_worker_no_db(tmp_path, monkeypatch):
    """A ``fileops=True`` worker runs the fileop serve loop with no DSN:
    a write then read roundtrips — proving the loop serves file ops without any
    database."""
    pool = _secure_fileop_pool(tmp_path, monkeypatch)
    pool.start()
    try:
        w = pool.submit_fileop(
            {"op": "write", "path": "/runs/data/a.txt", "content": "hi"}
        )
        assert w["ok"] is True, w
        assert w["bytes"] == len("hi".encode("utf-8")), w

        r = pool.submit_fileop({"op": "read", "path": "/runs/data/a.txt"})
        assert r == {"ok": True, "kind": "text", "content": "hi"}, r
    finally:
        pool.stop()


# --- @gvisor: multi-mount + configurable fileop roots (Task 4b-i) -----------
#
# The agent's warm worker mounts its files at clean paths (/data /mount, ...) and
# confines file ops to EXACTLY those mount dests, NOT /runs. Task 4b-i adds
# ``fileop_binds=[(dest, host_source), ...]`` on ``WarmGvisorPool``: each is
# bind-mounted rw at its dest (multi-bind passthrough via run_serve's
# extra_rw_binds) and the serve loop's roots come from VIBECANVAS_FILEOP_ROOTS
# (the dest mounts). So /data + /mount are writable inside, but /runs is OUTSIDE
# the configured roots and rejected.


def _multimount_fileop_pool(tmp_path, monkeypatch, data_dir, store_dir):
    """A ``fileops=True`` pool that binds two clean mounts:
    ``/data`` ← data_dir and ``/mount`` ← mount_dir, confining ops to them."""
    from vibecanvas_api.services.sandbox import _resolve_runsc
    from vibecanvas_api.services.sandbox.gvisor import RootlessGvisorProvider
    from vibecanvas_api.services.sandbox.warm import WarmGvisorPool

    store_root = str(tmp_path / "store")
    work_root = str(tmp_path / "work")
    monkeypatch.setattr(
        "vibecanvas_api.services.sandbox.warm.get_object_store",
        lambda: FilesystemObjectStore(root=store_root),
    )
    return WarmGvisorPool(
        provider=RootlessGvisorProvider(_resolve_runsc()),
        store_root=store_root,
        work_root=work_root,
        fileops=True,
        fileop_binds=[("/data", str(data_dir)), ("/mount", str(store_dir))],
    )


@pytest.mark.skipif(not _gvisor_runnable(), reason="rootless gVisor not runnable here")
def test_fileop_multibind_and_roots_confined(tmp_path, monkeypatch):
    """Task 4b-i: the worker binds /data + /mount at clean mount points and
    confines file ops to THOSE dests (not /runs).

    1. write+read /data/x.txt → proves /data is mounted + writable.
    2. write+read /mount/s.txt → proves the SECOND mount works (multi-bind).
    3. read /runs/anything → ``path_outside_roots`` (roots = /data:/mount, NOT
       /runs — the configurable-roots change genuinely confines).
    """
    # Real host dirs to bind (runsc rejects a non-existent bind source).
    tmp_data_dir = tmp_path / "host_data"
    tmp_store_dir = tmp_path / "host_store"
    tmp_data_dir.mkdir()
    tmp_store_dir.mkdir()

    pool = _multimount_fileop_pool(tmp_path, monkeypatch, tmp_data_dir, tmp_store_dir)
    pool.start()
    try:
        # (1) /data is mounted + writable.
        w = pool.submit_fileop(
            {"op": "write", "path": "/data/x.txt", "content": "hi"}
        )
        assert w["ok"] is True, w
        r = pool.submit_fileop({"op": "read", "path": "/data/x.txt"})
        assert r == {"ok": True, "kind": "text", "content": "hi"}, r

        # (2) the SECOND mount (/mount) works — multi-bind passthrough.
        w2 = pool.submit_fileop(
            {"op": "write", "path": "/mount/s.txt", "content": "yo"}
        )
        assert w2["ok"] is True, w2
        r2 = pool.submit_fileop({"op": "read", "path": "/mount/s.txt"})
        assert r2 == {"ok": True, "kind": "text", "content": "yo"}, r2

        # (3) /runs is OUTSIDE the configured roots → rejected.
        r3 = pool.submit_fileop({"op": "read", "path": "/runs/anything"})
        assert r3 == {"ok": False, "error": "path_outside_roots"}, r3
    finally:
        pool.stop()


@pytest.mark.skipif(not _gvisor_runnable(), reason="rootless gVisor not runnable here")
def test_fileop_worker_respects_network_none_config(tmp_path, monkeypatch):
    """When SANDBOX_NETWORK/config explicitly asks for ``none``, the fileop worker
    still runs without direct network. The default dev posture is host network."""
    from vibecanvas_api import config as cfg_mod

    monkeypatch.setattr(cfg_mod.config, "sandbox_network", "none")
    pool = _secure_fileop_pool(tmp_path, monkeypatch)
    pool.start()
    try:
        res = pool.submit_fileop(
            {
                "op": "exec",
                "command": (
                    "python3 -c \"import socket; "
                    "socket.create_connection(('8.8.8.8', 53), timeout=3)\""
                ),
            },
            timeout=30.0,
        )
        assert res["ok"] is True, res  # the exec op itself ran
        assert res["exit_code"] != 0, (
            f"network=none was not respected — egress succeeded: {res}"
        )
    finally:
        pool.stop()
