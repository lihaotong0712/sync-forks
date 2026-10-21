"""Synchronize eligible refs individually, preserving fork changes."""

import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from time import monotonic

if __package__:
    from . import fork_preflight
    from .workflow_log import group, log
else:
    import fork_preflight
    from workflow_log import group, log


def save_result(record):
    path = Path(os.environ["RESULT_FILE"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def git(repository, *args, input=None, timeout=120):
    started = monotonic()
    command = ["git", "-C", str(repository), *args]
    log("Git", f"开始: {shlex.join(command)}; 超时上限 {timeout} 秒")
    if input:
        for refspec in input.splitlines():
            log("获取引用", refspec)
    with group(f"Git {args[0]} 输出"):
        try:
            result = subprocess.run(
                command,
                input=input,
                text=True,
                capture_output=True,
                check=True,
                timeout=timeout,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            for stream in (
                getattr(error, "stdout", None),
                getattr(error, "stderr", None),
            ):
                if stream:
                    if isinstance(stream, bytes):
                        stream = stream.decode("utf-8", errors="replace")
                    for line in stream.splitlines():
                        log("Git 输出", line)
            log("Git 失败", f"{error}; 耗时 {monotonic() - started:.1f} 秒")
            raise
        for stream in (result.stdout, result.stderr):
            for line in stream.splitlines():
                log("Git 输出", line)
    log("Git", f"完成; 耗时 {monotonic() - started:.1f} 秒; 退出码 {result.returncode}")
    return result


def error_detail(error):
    if isinstance(error, subprocess.CalledProcessError):
        return (error.stderr or error.stdout or str(error)).strip()
    return str(error)


def synchronize(record, repository):
    selected = record["syncable_refs"]
    log(
        "同步计划",
        f"上游 {record['upstream']} -> {record['target_url']}; 待推送 {len(selected)} 个引用; 保留冲突 {record.get('conflict_count', 0)} 个",
    )
    record.update(result="未完成", synced_count=0, failed_count=0, failed_refs={})
    skipped_reason = record.get("reason", "")
    save_result(record)
    record["failed_stage"] = "获取上游对象"
    git(repository, "init", "--bare")
    # Fetch the exact objects checked earlier; do not follow moving upstream refs.
    git(
        repository,
        "fetch",
        "--no-tags",
        "--stdin",
        record["upstream"],
        input="".join(f"{sha}:{ref}\n" for ref, sha in selected.items()),
        timeout=3000,
    )
    record["failed_stage"] = "推送到 fork"
    for index, (ref, sha) in enumerate(selected.items(), 1):
        log("推送进度", f"{index}/{len(selected)}: {ref} -> {sha}")
        try:
            # One ref per push: a rejection must not block unrelated updates.
            git(repository, "push", "--porcelain", record["target_url"], f"{sha}:{ref}")
            record["synced_count"] += 1
            log("推送成功", ref)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            record["failed_refs"][ref] = error_detail(error)
            log("推送失败", f"{ref}: {error_detail(error)}; 继续处理其余引用")
        record["failed_count"] = len(record["failed_refs"])
        record["result"] = "部分同步" if record["synced_count"] else "失败"
        failures = [f"{ref}: {detail}" for ref, detail in record["failed_refs"].items()]
        record["reason"] = "; ".join(filter(None, [skipped_reason, *failures]))
        save_result(record)
    if (
        record["synced_count"]
        and not record.get("conflict_count")
        and not record["failed_count"]
    ):
        record["result"] = "同步成功"
    record["failed_stage"] = "推送到 fork" if record["failed_count"] else None
    save_result(record)
    return bool(record["failed_count"])


def main():
    started = monotonic()
    log("同步", "开始准备 Git 认证并重新筛选可同步引用")
    record = {
        "repo": os.environ["TARGET_REPO"],
        "result": "失败",
        "failed_stage": "准备同步",
    }
    save_result(record)
    failed = False
    try:
        if not os.environ.get("GH_TOKEN"):
            raise ValueError("请配置 FORK_SYNC_TOKEN secret")
        fork_preflight.run("gh", "auth", "setup-git")
        record.update(
            fork_preflight.check_with_deadline(record["repo"], monotonic() + 90)
        )
        save_result(record)
        if record["result"] == "待同步":
            with tempfile.TemporaryDirectory(
                prefix="fork-sync-", dir=os.environ["RUNNER_TEMP"]
            ) as directory:
                failed = synchronize(record, directory)
        else:
            log("跳过同步", f"复查结果: {record['result']}; {record.get('reason', '')}")
            failed = record["result"] == "预检失败"
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError,
        ValueError,
    ) as error:
        record.update(
            result="部分同步" if record.get("synced_count") else "失败",
            reason="; ".join(filter(None, [record.get("reason"), error_detail(error)])),
        )
        failed = True
    finally:
        save_result(record)
        log(
            "同步结果",
            f"{record['result']}; 成功 {record.get('synced_count', 0)}, 冲突跳过 {record.get('conflict_count', 0)}, 推送失败 {record.get('failed_count', 0)}; 耗时 {monotonic() - started:.1f} 秒; 结果文件 {os.environ['RESULT_FILE']}",
        )
        if record.get("reason"):
            log("原因", record["reason"])
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
