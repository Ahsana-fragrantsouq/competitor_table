"""
Safe log output  (imported FIRST by app.py)
===========================================
Why: on Render (Python 3.14 + gunicorn threads) the normal stdout can get stuck. One request is cut off in the
middle of a print -> stdout's internal lock stays "taken" -> EVERY later print / log line raises
"RuntimeError: reentrant call inside <_io.BufferedWriter name='<stdout>'>" -> every page answers error 500
(seen on /settings/automate) until the website is restarted.
Fix: print() and the log handlers write straight to the output with os.write - no buffer, no lock that can stay
stuck. A log line that cannot be written is dropped; it never breaks a page.

Where things are:
  SECTION 1  SafeStream            -> the replacement for sys.stdout / sys.stderr
  SECTION 2  install()             -> swaps sys.stdout / sys.stderr + the log handlers that used them
"""

import os
import sys
import logging
import threading


# ============================================================ SECTION 1: SafeStream
class SafeStream:
    """Minimal text stream: write() goes straight to the file descriptor (unbuffered), errors are swallowed."""

    def __init__(self, fd, name):
        self.fd, self.name, self.encoding, self.errors = fd, name, "utf-8", "replace"
        self._lock = threading.RLock()          # RLock: the same thread can never block itself

    def write(self, s):
        if not s:
            return 0
        data = str(s).encode("utf-8", "replace")
        try:
            with self._lock:
                while data:
                    n = os.write(self.fd, data)
                    data = data[n:]
        except Exception:                       # output closed / full -> drop this line, keep the page working
            pass
        return len(s)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):                            # nothing is buffered
        pass

    def fileno(self):
        return self.fd

    def isatty(self):
        return False

    def writable(self):
        return True


# ============================================================ SECTION 2: install
def install():
    """Replace sys.stdout / sys.stderr and every logging handler that wrote to the old ones. Safe to call twice."""
    if isinstance(sys.stdout, SafeStream):
        return
    old = {id(sys.stdout): None, id(sys.stderr): None, id(sys.__stdout__): None, id(sys.__stderr__): None}
    out, err = SafeStream(1, "<stdout>"), SafeStream(2, "<stderr>")
    old_out_ids = {id(sys.stdout), id(sys.__stdout__)}
    sys.stdout, sys.stderr = out, err
    swapped = 0
    loggers = [logging.getLogger()] + [l for l in logging.Logger.manager.loggerDict.values()
                                       if isinstance(l, logging.Logger)]
    for lg in loggers:                          # gunicorn access / error logs, Flask, ...
        for h in lg.handlers:
            if isinstance(h, logging.StreamHandler) and id(getattr(h, "stream", None)) in old:
                h.stream = out if id(h.stream) in old_out_ids else err
                swapped += 1
    print(f"[SAFE-OUTPUT] Logs now written without a buffer lock (stdout/stderr + {swapped} log handlers)")
