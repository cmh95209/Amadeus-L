"""Exercise the POSIX credential paths without changing the host's os.name."""
import builtins
import ctypes
import importlib
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chatgpt_auth as auth
import memory_plan_bridge as bridge


def test_plan_modules_import_without_windows_apis(monkeypatch, tmp_path):
    class PosixCtypes(ModuleType):
        def __getattribute__(self, name):
            if name in {"WinDLL", "wintypes"}:
                raise AssertionError(f"Windows-only ctypes attribute accessed: {name}")
            return super().__getattribute__(name)

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "ctypes.wintypes":
            raise AssertionError("Windows-only ctypes.wintypes imported")
        if name == "os" and globals and globals.get("__name__") in names:
            return posix_os
        return original_import(name, globals, locals, fromlist, level)

    original_import = builtins.__import__
    names = ("chatgpt_auth", "chatgpt_llm", "memory_plan_bridge")
    posix_os = SimpleNamespace(**vars(os))
    posix_os.name = "posix"
    posix_ctypes = PosixCtypes("ctypes")
    vars(posix_ctypes).update(vars(ctypes))
    for name in names:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(auth.memory, "DATA_DIR", tmp_path)
    monkeypatch.setitem(sys.modules, "ctypes", posix_ctypes)
    monkeypatch.setitem(sys.modules, "ctypes.wintypes", None)
    monkeypatch.setattr(builtins, "__import__", guarded_import)

    for name in names:
        importlib.import_module(name)


@pytest.fixture
def posix_auth(monkeypatch, tmp_path):
    # Swapping only this module's os reference keeps pathlib and pytest native.
    local_os = SimpleNamespace(**vars(os))
    monkeypatch.setattr(auth, "os", local_os)
    monkeypatch.setattr(auth.os, "name", "posix")
    monkeypatch.setattr(bridge, "os", local_os)
    monkeypatch.setattr(auth.memory, "DATA_DIR", tmp_path)
    dpapi = Mock(side_effect=AssertionError("DPAPI called on POSIX"))
    monkeypatch.setattr(auth, "_dpapi", dpapi)
    yield tmp_path
    dpapi.assert_not_called()


def test_auth_and_settings_chmod_before_write_and_round_trip(posix_auth, monkeypatch):
    events = []
    native_fchmod = getattr(os, "fchmod", None)
    named_temp = auth.tempfile.NamedTemporaryFile

    def record_chmod(fd, mode):
        events.append(("chmod", fd, mode))
        if native_fchmod is not None:
            native_fchmod(fd, mode)

    @contextmanager
    def record_temp(*args, **kwargs):
        with named_temp(*args, **kwargs) as temp:
            write = temp.write
            fd = temp.fileno()
            events.append(("open", fd))

            def record_write(data):
                events.append(("write", fd))
                return write(data)

            temp.write = record_write
            yield temp

    monkeypatch.setattr(auth.os, "fchmod", record_chmod, raising=False)
    monkeypatch.setattr(auth, "tempfile", SimpleNamespace(NamedTemporaryFile=record_temp))
    record = {
        "ext_agent_host_id": "urn:uuid:00000000-0000-4000-8000-000000000001",
        "client_id": "test-client",
        "subject": "test-account",
        "session": {
            "access_token": "placeholder-access",
            "refresh_token": "placeholder-refresh",
            "id_token": "placeholder-id",
            "email": "test@example.invalid",
            "scope": auth.PLAN_SCOPE,
            "refresh_at": 1_800_000_000,
            "expires_at": 1_800_000_600,
        },
    }
    auth._save_auth(record)
    auth.set_background_use_allowed(True)

    assert len(events) == 6
    for start in (0, 3):
        fd = events[start][1]
        assert events[start:start + 3] == [
            ("open", fd), ("chmod", fd, 0o600), ("write", fd),
        ]
    assert auth._load_auth() == record
    assert auth.get_background_use_allowed() is True
    if os.name == "posix":
        for filename in (auth.AUTH_FILE, auth.SETTINGS_FILE):
            assert stat.S_IMODE((posix_auth / filename).stat().st_mode) == 0o600


@pytest.mark.parametrize("unreadable", [False, True], ids=["windows-blob", "permission-denied"])
def test_unreadable_auth_is_signed_out(posix_auth, monkeypatch, unreadable):
    path = posix_auth / auth.AUTH_FILE
    path.write_bytes(b"\x01\x00\x00\x00\xd0\x8c\x9d\xdf\x01\xff")
    if unreadable:
        original_read = Path.read_bytes

        def deny_auth_read(self):
            if self == path:
                raise PermissionError("permission denied")
            return original_read(self)

        monkeypatch.setattr(Path, "read_bytes", deny_auth_read)

    assert auth.status() == {
        "signed_in": False, "email": "", "expires_in_s": 0, "plan_usage_scope": False,
    }


def test_bridge_secret_uses_owner_only_temp_file(posix_auth, monkeypatch):
    modes = []
    original_open = os.open

    def record_open(path, flags, mode=0o777, **kwargs):
        if flags & os.O_CREAT and Path(path).parent == posix_auth:
            modes.append(mode)
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(bridge, "_secret", None)
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", record_open)
        bridge.initialize()

    path = posix_auth / bridge.SECRET_FILE
    assert path.is_file()
    assert modes == [0o600]
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
