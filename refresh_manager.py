"""Reliable detached Datto collection for Streamlit and the scheduler.

- No work during import.
- Manual/scheduled/overdue triggers share one exclusive lock.
- Background worker survives Streamlit reruns/navigation.
- Child logs are unbuffered and Windows console is hidden.
- Orphaned locks are recovered ONLY if a recorded worker PID is confirmed dead.
- Existing collectors, JSON snapshots, and dashboard interfaces are unchanged.
"""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / 'data'
SETTINGS_PATH = ROOT / 'refresh_settings.json'
STATE_PATH = DATA_DIR / 'refresh_state.json'
LOCK_PATH = DATA_DIR / '.collection.lock'
LOG_PATH = DATA_DIR / 'refresh_worker.log'
INTERVAL_HOURS = 8
DEFAULT_SETTINGS = {'automatic_refresh': False, 'check_overdue_on_open': True}


def _now():
    return datetime.now(timezone.utc)


def _parse_timestamp(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _read_json(path, fallback):
    try:
        with Path(path).open(encoding='utf-8') as handle:
            result = json.load(handle)
        return result if isinstance(result, dict) else dict(fallback)
    except (OSError, ValueError, TypeError):
        return dict(fallback)


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.refresh_', suffix='.tmp', delete=False) as handle:
            name = handle.name
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def get_settings():
    data = _read_json(SETTINGS_PATH, DEFAULT_SETTINGS)
    return {key: data.get(key) is True for key in DEFAULT_SETTINGS}


def save_settings(automatic_refresh, check_overdue_on_open):
    data = {'automatic_refresh': bool(automatic_refresh),
            'check_overdue_on_open': bool(check_overdue_on_open)}
    _atomic_json(SETTINGS_PATH, data)
    return data


def live_enabled():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env', override=False)
    except ImportError:
        pass
    return os.getenv('DATTO_ENABLE_LIVE_COLLECTION', '0') == '1'


def _worker_alive(pid):
    """True/False only when known; None means uncertain. Never guess dead."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
        return None
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.GetExitCodeProcess.restype = ctypes.c_int
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.restype = ctypes.c_int
        handle = kernel.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            exit_code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return None
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None


def _lock_owner():
    try:
        return LOCK_PATH.read_text(encoding='utf-8').strip()
    except OSError:
        return None


def _remove_owned_lock(token):
    if _lock_owner() == token:
        LOCK_PATH.unlink(missing_ok=True)


def _reconcile():
    """Handle an abruptly killed modern worker. Legacy pid-less locks stay untouched."""
    state = _read_json(STATE_PATH, {})
    if state.get('last_result') != 'running':
        return state
    pid = state.get('worker_pid')
    if not LOCK_PATH.exists() or not isinstance(pid, int):
        return state
    if _worker_alive(pid) is not False:
        return state
    token = state.get('lock_token')
    # Only repair a dead worker if this exact run still owns the lock.
    if not isinstance(token, str) or _lock_owner() != token:
        return state
    state.update({'last_result': 'failed', 'last_error': 'Background worker exited before completing',
                  'last_attempt_at': _now().isoformat()})
    _atomic_json(STATE_PATH, state)
    _remove_owned_lock(token)
    return state


def get_state():
    return _reconcile()


def lock_present():
    _reconcile()
    return LOCK_PATH.exists()


def next_due(state=None, domain_snapshot=None):
    state = get_state() if state is None else state
    anchor = (_parse_timestamp(state.get('last_attempt_at'))
              or _parse_timestamp(state.get('last_successful_at'))
              or _parse_timestamp(domain_snapshot))
    return anchor + timedelta(hours=INTERVAL_HOURS) if anchor else None


def is_due(state=None, domain_snapshot=None, now=None):
    due = next_due(state, domain_snapshot)
    return due is None or (now or _now()) >= due



def overdue_notice(state=None, domain_snapshot=None, now=None):
    """Advisory for the UI only. Never starts collection or changes scheduler timing.

    The scheduler continues using next_due/is_due. A failed or partial attempt
    must not mask data older than eight hours just because an attempt was recent.
    """
    state = get_state() if state is None else state
    now = now or _now()
    result = str(state.get('last_result') or '').strip().lower()
    due = next_due(state, domain_snapshot)
    successful = _parse_timestamp(state.get('last_successful_at'))
    baseline = successful or _parse_timestamp(domain_snapshot)
    stale = baseline is None or now >= baseline + timedelta(hours=INTERVAL_HOURS)
    overdue = due is None or now >= due
    # The latest attempt is incomplete or failed. Preserve a visible advisory
    # even if next_due moved forward due to that unsuccessful attempt.
    needs_attention = result in ('partial', 'failed')
    return {
        'show': result != 'running' and (overdue or stale or needs_attention),
        'overdue': overdue or stale,
        'incomplete': needs_attention,
        'due': due,
        'last_successful_at': successful,
    }


def _successful(summary):
    if not isinstance(summary, dict):
        return False
    domain_status = str((summary.get('domains') or {}).get('status', '')).lower()
    if domain_status not in ('updated', 'unchanged', 'success', 'no_change', 'no_changes'):
        return False
    try:
        count = int(summary.get('customers', 0))
    except (ValueError, TypeError):
        return False
    if count <= 0:
        return False
    for name in ('seats', 'applications'):
        section = summary.get(name) or {}
        try:
            if int(section.get('failed', -1)) != 0 or int(section.get('successful', -1)) != count:
                return False
        except (ValueError, TypeError):
            return False
    return True


def run_refresh(trigger='manual'):
    """Return immediately. Worker runs independently of Streamlit's UI lifecycle."""
    if trigger not in ('manual', 'scheduled', 'overdue'):
        raise ValueError('Unknown refresh trigger')
    if not live_enabled():
        return {'status': 'disabled', 'message': 'Live collection remains disabled'}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _reconcile()
    started = _now().isoformat()
    token = f'launcher_pid={os.getpid()} started={started}'
    try:
        descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return {'status': 'busy', 'message': 'A collection is running, or a legacy lock needs manual review'}
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(token + '\n')
        state = get_state()
        state.update({'last_attempt_at': started, 'last_trigger': trigger,
                      'last_result': 'running', 'last_error': None,
                      'worker_pid': None, 'lock_token': token})
        _atomic_json(STATE_PATH, state)
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        with LOG_PATH.open('a', encoding='utf-8') as log:
            proc = subprocess.Popen(
                [sys.executable, '-u', str(ROOT / 'refresh_manager.py'), '--worker', trigger, token],
                cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                close_fds=True, creationflags=flags, env=env,
            )
        # Worker also writes its pid on startup; don't overwrite a completed run.
        state = _read_json(STATE_PATH, {})
        if state.get('lock_token') == token and state.get('last_result') == 'running':
            state['worker_pid'] = proc.pid
            _atomic_json(STATE_PATH, state)
        return {'status': 'started', 'message': 'Background collection started', 'pid': proc.pid}
    except Exception as error:
        state = _read_json(STATE_PATH, {})
        state.update({'last_attempt_at': _now().isoformat(), 'last_trigger': trigger,
                      'last_result': 'failed', 'last_error': f'Launch failed: {error}'})
        _atomic_json(STATE_PATH, state)
        _remove_owned_lock(token)
        return {'status': 'failed', 'message': f'Unable to launch collection: {error}'}


def _worker(trigger, token):
    if not live_enabled() or _lock_owner() != token:
        print('Worker refused: live gate disabled or lock ownership changed', flush=True)
        return 1
    state = _read_json(STATE_PATH, {})
    state['worker_pid'] = os.getpid()
    state['lock_token'] = token
    _atomic_json(STATE_PATH, state)
    print(f'=== Datto refresh started: {trigger} / PID {os.getpid()} / {_now().isoformat()} ===', flush=True)
    try:
        from collection_manager import run_collection
        summary = run_collection()
        result = 'success' if _successful(summary) else 'partial'
        finished = _now().isoformat()
        state = _read_json(STATE_PATH, {})
        state.update({'last_attempt_at': finished, 'last_trigger': trigger,
                      'last_result': result, 'last_summary': summary, 'last_error': None})
        if result == 'success':
            state['last_successful_at'] = finished
        _atomic_json(STATE_PATH, state)
        print(f'=== Datto refresh completed: {result} ===', flush=True)
        return 0
    except BaseException as error:
        state = _read_json(STATE_PATH, {})
        state.update({'last_attempt_at': _now().isoformat(), 'last_trigger': trigger,
                      'last_result': 'failed', 'last_error': f'{type(error).__name__}: {error}'})
        _atomic_json(STATE_PATH, state)
        print(f'=== Datto refresh failed: {type(error).__name__}: {error} ===', flush=True)
        return 1
    finally:
        _remove_owned_lock(token)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        sys.exit(_worker(sys.argv[2], sys.argv[3]))
    print('Refresh manager ready. Launch collections through run_refresh().')

