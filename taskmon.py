#!/usr/bin/env python3

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class TaskStatus(Enum):
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class TaskDef:
    name: str
    command: str
    timeout: int = 60
    cwd: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    shell: bool = True


@dataclass
class TaskResult:
    name: str
    command: str
    status: str
    exit_code: Optional[int]
    duration: float
    stdout_tail: str
    stderr_tail: str
    started_at: str
    finished_at: str
    timed_out: bool


STDOUT_TAIL_LINES = 20
STDERR_TAIL_LINES = 10


def load_config(path: str) -> List[TaskDef]:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    tasks = []
    for item in cfg.get("tasks", []):
        known = {k for k in TaskDef.__dataclass_fields__}
        filtered = {k: v for k, v in item.items() if k in known}
        tasks.append(TaskDef(**filtered))
    return tasks


def tail_text(text: str, n: int) -> str:
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else text


def run_task(task: TaskDef) -> TaskResult:
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    t0 = time.monotonic()

    env = None
    if task.env:
        env = os.environ.copy()
        env.update(task.env)

    try:
        proc = subprocess.Popen(
            task.command,
            shell=task.shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=task.cwd,
            env=env,
            start_new_session=True,
        )
    except Exception as exc:
        duration = time.monotonic() - t0
        return TaskResult(
            name=task.name,
            command=task.command,
            status=TaskStatus.ERROR.value,
            exit_code=-1,
            duration=duration,
            stdout_tail="",
            stderr_tail=str(exc),
            started_at=started_at,
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            timed_out=False,
        )

    timed_out = False
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=task.timeout)
        exit_code = proc.returncode
        status = TaskStatus.OK.value if exit_code == 0 else TaskStatus.FAILED.value
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except OSError:
            proc.terminate()
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
            stdout_bytes, stderr_bytes = proc.communicate()
        exit_code = proc.returncode
        status = TaskStatus.TIMEOUT.value

    duration = time.monotonic() - t0
    stdout_tail = tail_text(stdout_bytes.decode(errors="replace"), STDOUT_TAIL_LINES)
    stderr_tail = tail_text(stderr_bytes.decode(errors="replace"), STDERR_TAIL_LINES)

    return TaskResult(
        name=task.name,
        command=task.command,
        status=status,
        exit_code=exit_code,
        duration=duration,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
        started_at=started_at,
        finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        timed_out=timed_out,
    )


BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"

STATUS_STYLE = {
    TaskStatus.OK.value: (GREEN, "✔ OK"),
    TaskStatus.FAILED.value: (RED, "✘ FAILED"),
    TaskStatus.TIMEOUT.value: (YELLOW, "⏱ TIMEOUT"),
    TaskStatus.ERROR.value: (RED, "⚠ ERROR"),
}


def print_task_header(index: int, total: int, task: TaskDef):
    print(f"\n{BOLD}{CYAN}[{index}/{total}]{RESET} {BOLD}▶ {task.name}{RESET}  {DIM}{task.command}{RESET}")
    print(f"  timeout={task.timeout}s" + (f"  cwd={task.cwd}" if task.cwd else ""))


def print_task_result(r: TaskResult):
    color, label = STATUS_STYLE.get(r.status, ("", r.status))
    print(f"  {color}{label}{RESET}  exit={r.exit_code}  time={r.duration:.1f}s")
    if r.status != TaskStatus.OK.value and r.stdout_tail:
        print(f"  {DIM}── stdout (last {STDOUT_TAIL_LINES} lines) ──{RESET}")
        for line in r.stdout_tail.splitlines():
            print(f"    {line}")
    if r.stderr_tail:
        print(f"  {DIM}── stderr (last {STDERR_TAIL_LINES} lines) ──{RESET}")
        for line in r.stderr_tail.splitlines():
            print(f"    {line}")


def print_report(results: List[TaskResult]):
    total = len(results)
    ok_count = sum(1 for r in results if r.status == TaskStatus.OK.value)
    fail_count = sum(1 for r in results if r.status == TaskStatus.FAILED.value)
    timeout_count = sum(1 for r in results if r.status == TaskStatus.TIMEOUT.value)
    error_count = sum(1 for r in results if r.status == TaskStatus.ERROR.value)

    print(f"\n{'=' * 64}")
    print(f"{BOLD}  运行清单 / Run Report{RESET}")
    print(f"{'=' * 64}")

    header = f"  {'#':<3} {'名称':<16} {'状态':<10} {'退出码':<7} {'耗时':<8} {'超时'}"
    print(header)
    print(f"  {'-' * 58}")

    for i, r in enumerate(results, 1):
        color, _ = STATUS_STYLE.get(r.status, ("", ""))
        row = (
            f"  {i:<3} "
            f"{r.name:<16} "
            f"{color}{r.status:<10}{RESET} "
            f"{str(r.exit_code):<7} "
            f"{r.duration:.1f}s{'':>3} "
            f"{'是' if r.timed_out else '否'}"
        )
        print(row)

    print(f"  {'-' * 58}")
    print(
        f"  总计: {total}  "
        f"{GREEN}通过={ok_count}{RESET}  "
        f"{RED}失败={fail_count}{RESET}  "
        f"{YELLOW}超时={timeout_count}{RESET}  "
        f"{RED}错误={error_count}{RESET}"
    )
    print(f"{'=' * 64}\n")


def save_report(results: List[TaskResult], path: str):
    data = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total": len(results),
            "ok": sum(1 for r in results if r.status == TaskStatus.OK.value),
            "failed": sum(1 for r in results if r.status == TaskStatus.FAILED.value),
            "timeout": sum(1 for r in results if r.status == TaskStatus.TIMEOUT.value),
            "error": sum(1 for r in results if r.status == TaskStatus.ERROR.value),
        },
        "tasks": [asdict(r) for r in results],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"报告已保存至: {path}")


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="命令行作业监督器 — 逐个启动、限时等待、收集状态，生成运行清单"
    )
    parser.add_argument("config", help="任务配置文件 (JSON)")
    parser.add_argument(
        "-o", "--output",
        default="taskmon_report.json",
        help="报告输出路径 (默认: taskmon_report.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅展示任务列表，不执行",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.config):
        print(f"错误: 配置文件不存在 — {args.config}", file=sys.stderr)
        sys.exit(1)

    tasks = load_config(args.config)
    if not tasks:
        print("配置中没有任务。", file=sys.stderr)
        sys.exit(0)

    if args.dry_run:
        print(f"{BOLD}任务预览 (dry-run){RESET}")
        for i, t in enumerate(tasks, 1):
            print(f"  {i}. {t.name}  {DIM}{t.command}{RESET}  timeout={t.timeout}s")
        sys.exit(0)

    results: List[TaskResult] = []
    total = len(tasks)

    for i, task in enumerate(tasks, 1):
        print_task_header(i, total, task)
        result = run_task(task)
        results.append(result)
        print_task_result(result)

    print_report(results)
    save_report(results, args.output)

    all_ok = all(r.status == TaskStatus.OK.value for r in results)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
