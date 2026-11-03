"""Run a command, streaming its combined output to console AND a log file.

Usage:
    python tools/tee.py <logfile> <command> [args...]

The previous log (if any) is kept as <logfile>.prev. Exits with the
command's exit code, so `set -e` / `if errorlevel` keep working.
Binary-safe streaming: pip-style \\r progress lines are preserved.
Progress is live (not buffered to child exit): the child runs
unbuffered and output is forwarded chunk-by-chunk as it arrives.
"""

import codecs
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
    env = dict(os.environ)
    # Live progress: a Python child with a piped stdout block-buffers
    # (typically 8 KB), so without this the console would stay empty until
    # each install/migrate step exits and only the file would grow.
    # Unbuffered child output flushes every print straight through to
    # console AND file.
    env["PYTHONUNBUFFERED"] = "1"
    with open(logfile, "w", encoding="utf-8", errors="replace") as fh:
        fh.write("$ " + " ".join(cmd) + "\n\n")
        fh.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env)
        assert proc.stdout is not None
        # read1 (not read): BufferedReader.read(n) on a pipe blocks until
        # the buffer is FULL or EOF, so the console would stay empty until
        # the whole child exits. read1 returns after a single raw read,
        # which is what streams progress live. The incremental decoder
        # keeps multi-byte chars intact across chunk boundaries.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            fh.write(decoder.decode(chunk))
            fh.flush()
        fh.write(decoder.decode(b"", final=True))
        fh.flush()
        rc = proc.wait()
    print(f"[log] done (exit={rc}): {logfile}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
