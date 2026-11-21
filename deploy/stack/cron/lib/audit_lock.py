#!/usr/bin/env python3
"""Shared gbrain corpus-audit lockfile helper.

The lock is intentionally a plain file so shell scripts and Python audit workers can
share one interlock. Acquisition is atomic via O_CREAT|O_EXCL. The file content is
JSON and always includes the owning PID plus start_ts, matching the audit spec.
"""

import argparse
import errno
import json
import os
import socket
import sys
import time
from pathlib import Path

DEFAULT_LOCK_PATH = Path.home() / "gbrain" / ".gbrain" / ".audit-lock"
DEFAULT_STALE_AFTER_SECONDS = 4 * 60 * 60
EX_TEMPFAIL = 75


class LockHeld(RuntimeError):
    """Raised when a fresh lock exists and cannot be acquired."""

    def __init__(self, path, info):
        self.path = Path(path)
        self.info = info or {}
        super().__init__("audit lock held at {} by pid {}".format(self.path, self.info.get("pid")))


def _coerce_path(path):
    return Path(path).expanduser()


def _now():
    return time.time()


def read_lock(path=DEFAULT_LOCK_PATH):
    """Return lock JSON as a dict, or None when the lock file does not exist."""
    path = _coerce_path(path)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw, "invalid": True}
    if not isinstance(info, dict):
        return {"raw": raw, "invalid": True}
    return info


def pid_is_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        if exc.errno == errno.EPERM:
            return True
        return False
    return True


def _lock_age_seconds(path, info, now):
    start_ts = info.get("start_ts") if isinstance(info, dict) else None
    try:
        if start_ts is None:
            raise ValueError("missing start_ts")
        return now - float(start_ts)
    except (TypeError, ValueError):
        try:
            return now - _coerce_path(path).stat().st_mtime
        except FileNotFoundError:
            return 0


def is_stale_lock(path, info=None, now=None, stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS):
    """A lock is stale when its PID is dead or its age exceeds stale_after_seconds."""
    path = _coerce_path(path)
    if info is None:
        info = read_lock(path)
    if info is None:
        return False
    now = _now() if now is None else now
    if not isinstance(info, dict) or info.get("invalid"):
        return True
    if not pid_is_alive(info.get("pid")):
        return True
    return _lock_age_seconds(path, info, now) > stale_after_seconds


def _build_lock_info(pid=None, start_ts=None, owner="audit", now=None):
    now = _now() if now is None else now
    if pid is None:
        pid = os.getpid()
    if start_ts is None:
        start_ts = now
    return {
        "pid": int(pid),
        "start_ts": start_ts,
        "owner": owner,
        "host": socket.gethostname(),
    }


def _create_lock_file(path, info):
    path = _coerce_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(path), flags, 0o644)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(info, sort_keys=True) + "\n")
    return info


def acquire_lock(
    path=DEFAULT_LOCK_PATH,
    pid=None,
    start_ts=None,
    owner="audit",
    stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS,
):
    """Atomically acquire the audit lock or raise LockHeld for a fresh holder."""
    path = _coerce_path(path)
    for _ in range(3):
        info = _build_lock_info(pid=pid, start_ts=start_ts, owner=owner)
        try:
            return _create_lock_file(path, info)
        except FileExistsError:
            existing = read_lock(path)
            if not is_stale_lock(path, existing, stale_after_seconds=stale_after_seconds):
                raise LockHeld(path, existing)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
    existing = read_lock(path)
    raise LockHeld(path, existing)


def try_acquire_lock(path=DEFAULT_LOCK_PATH, **kwargs):
    """Return lock info when acquired, or None when a fresh lock is held."""
    try:
        return acquire_lock(path, **kwargs)
    except LockHeld:
        return None


def release_lock(path=DEFAULT_LOCK_PATH, pid=None):
    """Release the lock.

    When pid is supplied, only that owning PID may remove the file; this keeps a
    cleanup trap from deleting a lock retaken by another process after a crash.
    """
    path = _coerce_path(path)
    info = read_lock(path)
    if info is None:
        return False
    if pid is not None:
        try:
            if not isinstance(info, dict) or info.get("pid") is None:
                return False
            expected = int(pid)
            actual = int(info.get("pid"))
        except (TypeError, ValueError):
            return False
        if actual != expected:
            return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _cmd_acquire(args):
    try:
        info = acquire_lock(
            args.path,
            pid=args.pid,
            start_ts=args.start_ts,
            owner=args.owner,
            stale_after_seconds=args.stale_seconds,
        )
    except LockHeld as exc:
        if not args.quiet:
            print("audit lock held: {}".format(json.dumps(exc.info, sort_keys=True)), file=sys.stderr)
        return EX_TEMPFAIL
    if args.print_json:
        print(json.dumps(info, sort_keys=True))
    elif not args.quiet:
        print("acquired audit lock {} pid={}".format(_coerce_path(args.path), info["pid"]))
    return 0


def _cmd_release(args):
    released = release_lock(args.path, pid=args.pid)
    if args.print_json:
        print(json.dumps({"released": released}, sort_keys=True))
    elif not args.quiet:
        print("released audit lock" if released else "audit lock not released")
    return 0 if released or args.ok_if_missing else 1


def _cmd_status(args):
    info = read_lock(args.path)
    payload = {
        "path": str(_coerce_path(args.path)),
        "held": info is not None,
        "info": info,
        "stale": bool(info is not None and is_stale_lock(args.path, info, stale_after_seconds=args.stale_seconds)),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Acquire/release the shared gbrain audit lock")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--path", default=str(DEFAULT_LOCK_PATH), help="lockfile path")
        p.add_argument("--stale-seconds", type=float, default=DEFAULT_STALE_AFTER_SECONDS)
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--print-json", action="store_true")

    acquire = sub.add_parser("acquire")
    add_common(acquire)
    acquire.add_argument("--pid", type=int, default=None, help="owner PID to write; shell callers pass $$")
    acquire.add_argument("--start-ts", type=float, default=None)
    acquire.add_argument("--owner", default="audit")
    acquire.set_defaults(func=_cmd_acquire)

    release = sub.add_parser("release")
    add_common(release)
    release.add_argument("--pid", type=int, default=None, help="only release when lock PID matches")
    release.add_argument("--ok-if-missing", action="store_true")
    release.set_defaults(func=_cmd_release)

    status = sub.add_parser("status")
    add_common(status)
    status.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
