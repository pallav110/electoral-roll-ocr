"""What does the container actually see for CPU count?

Docker Desktop reports NCPU=10 after the .wslconfig change, but `nproc` inside
the OCR container still returns 1.  Those disagree, and which one the worker
pool actually uses decides whether OCR runs on 1 core or 10.

This prints every view: the cgroup quota, the cpuset, /proc/cpuinfo, os.cpu_count,
len(sched_getaffinity), and the value the module itself reads for its pool size.
"""
import sys

sys.stdout = open("/data/cpu.txt", "w", encoding="utf-8", errors="replace")
sys.path.insert(0, "/app")

import os


def read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except Exception as exc:
        return f"<unavailable: {exc}>"


print("cgroup v2 cpu.max        :", read("/sys/fs/cgroup/cpu.max"))
print("cgroup v2 cpuset.cpus    :", read("/sys/fs/cgroup/cpuset.cpus.effective"))
print("cgroup v2 cpuset.cpus.max:", read("/sys/fs/cgroup/cpuset.cpus.max"))
print("cgroup v1 cpu.cfs_quota  :", read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
print("cgroup v1 cpu.cfs_period :", read("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
print("cgroup v1 cpuset.cpus    :", read("/sys/fs/cgroup/cpuset/cpuset.cpus"))
print("processors in /proc/cpuinfo:",
      read("/proc/cpuinfo").count("processor\t:"))
print("os.cpu_count()           :", os.cpu_count())
try:
    print("sched_getaffinity len    :", len(os.sched_getaffinity(0)))
except Exception as exc:
    print("sched_getaffinity len    : <unavailable>", exc)
print("OMP_THREAD_LIMIT         :", os.getenv("OMP_THREAD_LIMIT"))

import ocr_pdf_api as A
print("_OCR_CARD_WORKERS        :", A._OCR_CARD_WORKERS)
print("_TESS_BACKEND            :", A._TESS_BACKEND)
try:
    print("tesserocr available      :", A.tesserocr.__version__)
except Exception:
    print("tesserocr available      : no")