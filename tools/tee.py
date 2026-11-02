"""Run a command, streaming its combined output to console AND a log file.

Usage:
    python tools/tee.py <logfile> <command> [args...]

The previous log (if any) is kept as <logfile>.prev. Exits with the
command's exit code, so `set -e` / `if errorlevel` keep working.
Binary-safe streaming: pip-style \\r progress lines are preserved.
"""

import os
import subprocess
import sys


def main(argv):
    if len(argv) < 3 or argv[1] in ("-h", "--help"):
        print(__doc__.strip().splitlines()[0])
        print("Usage: python tools/tee.py <logfile> <command> [args...]")
        return 2
    logfile, cmd = argv[1], argv[2:]
    if cmd[0].lower().endswith((".bat", ".cmd")):
        cmd = ["cmd", "/c"] + cmd
    parent = os.path.dirname(os.path.abspath(logfile))
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(logfile):
        try:
            os.replace(logfile, logfile + ".prev")
        except OSError:
            pass
    print(f"[log] full output saved to {logfile} "
          f"(send this file when reporting a problem)", flush=True)
    with open(logfile, "w", encoding="utf-8", errors="replace") as fh:
        fh.write("$ " + " ".join(cmd) + "\n\n")
        fh.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            fh.write(chunk.decode("utf-8", errors="replace"))
            fh.flush()
        rc = proc.wait()
    print(f"[log] done (exit={rc}): {logfile}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
