"""version_str: the shared active-version formatter (VFS 2c).

Lives in the leaf util `utils.versioning` so both the agent's read_file tool
and the VFS HTTP route share it without importing an Agent Runtime SDK.
"""
from vibecanvas_api.utils.versioning import version_str


def test_version_str_formats_active():
    assert version_str({"active_major": 1, "active_sub": 2}) == "v1.sv2"


def test_version_str_none_when_missing():
    assert version_str({}) is None
    assert version_str({"active_major": 1}) is None
    assert version_str({"active_sub": 0}) is None


def test_version_str_zero_sub_is_valid():
    assert version_str({"active_major": 1, "active_sub": 0}) == "v1.sv0"
