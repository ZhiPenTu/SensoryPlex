"""终止旧的 LaunchAgent 插件进程，让 launchd 以更新后的代码重新拉起。"""
import os
import signal
import sys
import time

for pid in [85425, 18194]:
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Terminated {pid}")
    except OSError as e:
        print(f"Error {pid}: {e}")

time.sleep(2)
for pid in [85425, 18194]:
    try:
        os.kill(pid, 0)
        print(f"Warning: {pid} still alive")
    except OSError:
        print(f"Confirmed: {pid} exited")
