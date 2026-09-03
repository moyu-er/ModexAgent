"""Simulate apt/debconf's transient raw-mode terminal juggle at command end.

Usage: python3 _fake_apt_hook.py <raw-hold-seconds>
Sets the tty raw, stays silent for the hold (like debconf's frontend
handshake), restores canonical mode, prints a completion line, exits 0.
"""
import sys
import termios
import time

hold = float(sys.argv[1]) if len(sys.argv) > 1 else 0.9
fd = 0
try:
    attrs = termios.tcgetattr(fd)
    raw = termios.tcgetattr(fd)
    raw[3] &= ~(termios.ECHO | termios.ICANON)
    termios.tcsetattr(fd, termios.TCSANOW, raw)
    time.sleep(hold)
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
except termios.error:
    pass
print("apt-hook-done")
