"""Windows password validation and local append-only activity log for Streamlit.

Only use with a loopback-bound Streamlit server. Does not persist credentials.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading

_LOCK = threading.RLock()
LOG_PATH = Path(__file__).resolve().parent / 'data' / 'operator_audit.jsonl'


def verify_windows_credentials(account: str, password: str) -> str | None:
    """Validate Windows credentials using LogonUserW; return qualified identity or None."""
    if os.name != 'nt' or not account or not password:
        return None
    account = account.strip()
    if '\\' in account:
        domain, username = account.split('\\', 1)
    elif '@' in account:
        domain, username = None, account
    else:
        domain, username = '.', account
    if not username or not domain and '@' not in username:
        return None
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    logon = advapi.LogonUserW
    logon.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                      wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    logon.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = wintypes.HANDLE()
    # LOGON32_LOGON_INTERACTIVE=2; LOGON32_PROVIDER_DEFAULT=0.
    if not logon(username, domain, password, 2, 0, ctypes.byref(handle)):
        return None
    close(handle)
    machine = os.environ.get('COMPUTERNAME', 'LOCAL')
    return f'{machine}\\{username}' if domain == '.' else (account if '@' in account else f'{domain}\\{username}')


def record_activity(actor: str, action: str, *, target: str = '', alert_id: str = '', details: str = '') -> None:
    """Append a single audit event. Errors propagate instead of silently discarding events."""
    if not actor or not action:
        raise ValueError('Audit actor and action are required')
    now = datetime.now(timezone.utc)
    entry = {
        'timestamp_utc': now.isoformat(timespec='seconds'),
        'actor': actor,
        'action': action,
        'target': target,
        'alert_id': alert_id,
        'details': details,
    }
    line = (json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    with _LOCK:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(LOG_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise OSError('Incomplete audit write')
            os.fsync(fd)
        finally:
            os.close(fd)


def read_activity() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with _LOCK, LOG_PATH.open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]
