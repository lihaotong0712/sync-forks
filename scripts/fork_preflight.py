import json
import os
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import ContextVar
from pathlib import Path
from time import monotonic

if __package__:
    from .workflow_log import log
else:
    from workflow_log import log

CURRENT_REPO = ContextVar("current_repo", default=None)

PREFLIGHT_SECONDS = 600
REPOSITORY_SECONDS = 90
COMMAND_DEADLINE = ContextVar("command_deadline", default=None)


def run(*args):
    deadline = COMMAND_DEADLINE.get()
    remaining = 120 if deadline is None else deadline - monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired(args, 0)
    started = monotonic()
    log(
        "命令",
        f"开始: {shlex.join(args)}; 超时上限 {min(120, remaining):.1f} 秒",
        CURRENT_REPO.get(),
    )
    try:
        output = subprocess.check_output(
            args, text=True, stderr=subprocess.PIPE, timeout=min(120, remaining)
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        log(
            "命令失败",
            f"耗时 {monotonic() - started:.1f} 秒; {error}; {getattr(error, 'stderr', '') or ''}",
            CURRENT_REPO.get(),
        )
        raise
    log("命令", f"完成, 耗时 {monotonic() - started:.1f} 秒", CURRENT_REPO.get())
    return output


def api(endpoint):
    return json.loads(run("gh", "api", endpoint))


def refs(url):
    output = run("git", "ls-remote", "--refs", url, "refs/heads/*", "refs/tags/*")
    return {ref: sha for sha, ref in (line.split() for line in output.splitlines())}


def check(repo):
    started = monotonic()
    token = CURRENT_REPO.set(repo)
    log("预检", "开始检查目标权限, 上游访问和引用差异", repo)
    record = {
        "repo": repo,
        "result": "预检失败",
        "diff_count": None,
        "failed_stage": "预检",
        "reason": "",
    }
    try:
        target = api(f"repos/{repo}")
        parent = target.get("parent")
        if not parent:
            record.update(result="无法同步", reason="目标不是 fork 仓库")
            return record
        record["upstream"] = parent["clone_url"]
        record["target_url"] = target["clone_url"]
        log(
            "仓库",
            f"上游: {parent['full_name']}; 归档: {target.get('archived', False)}; 禁用: {target.get('disabled', False)}; 写权限: {target.get('permissions', {}).get('push', False)}",
            repo,
        )
        # Metadata visibility alone does not prove Git contents access.
        api(f"repos/{parent['full_name']}")
        upstream_refs = refs(parent["clone_url"])
        target_refs = refs(target["clone_url"])
        changed = [
            ref for ref, sha in upstream_refs.items() if target_refs.get(ref) != sha
        ]
        record["diff_count"] = len(changed)
        log(
            "引用",
            f"上游 {len(upstream_refs)} 个, fork {len(target_refs)} 个, 差异 {len(changed)} 个; fork 独有 {len(target_refs.keys() - upstream_refs.keys())} 个",
            repo,
        )
        if not changed:
            record.update(result="无需同步", failed_stage=None)
            return record
        if target.get("archived") or target.get("disabled"):
            record.update(result="无法同步", reason="目标仓库已归档或被禁用")
            return record
        if not target.get("permissions", {}).get("push"):
            record.update(result="无法同步", reason="当前令牌未获得目标仓库写权限")
            return record
        conflicts = []
        syncable_refs = {}
        for ref in changed:
            if ref not in target_refs:
                syncable_refs[ref] = upstream_refs[ref]
                log("可同步", f"{ref}: 新增引用, 上游 {upstream_refs[ref]}", repo)
                continue
            log(
                "比较",
                f"{ref}: fork {target_refs[ref]} -> 上游 {upstream_refs[ref]}",
                repo,
            )
            if ref.startswith("refs/tags/"):
                conflicts.append(f"{ref}: 同名标签对象不同")
                log("保留 fork", conflicts[-1], repo)
                continue
            # Compare the observed SHAs, avoiding ambiguous branch names and moving refs.
            try:
                comparison = api(
                    f"repos/{repo}/compare/{target_refs[ref]}...{upstream_refs[ref]}?per_page=1"
                )
            except subprocess.CalledProcessError as error:
                if "HTTP 404" not in (error.stderr or ""):
                    raise
                # Unrelated histories cannot be compared; preserve that branch.
                conflicts.append(
                    f"{ref}: 无法确认提交关系 (HTTP 404, 可能无共同历史或权限不足)"
                )
                log("保留 fork", conflicts[-1], repo)
                continue
            status = comparison["status"]
            if status in {"behind", "diverged"}:
                conflicts.append(f"{ref}: 无法快进 ({status})")
                log("保留 fork", conflicts[-1], repo)
            elif status not in {"ahead", "identical"}:
                raise ValueError(f"未知提交比较状态: {status}")
            else:
                syncable_refs[ref] = upstream_refs[ref]
                log("可同步", f"{ref}: {status}", repo)
        record.update(
            result="待同步" if syncable_refs else "全部冲突",
            failed_stage=None,
            syncable_refs=syncable_refs,
            reason="; ".join(conflicts),
            conflict_count=len(conflicts),
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip()
        record["reason"] = f"访问或比较失败 (可能不存在, 权限不足或服务异常): {detail}"
        record["exit_code"] = str(error.returncode)
    except (subprocess.TimeoutExpired, ValueError, KeyError) as error:
        record["reason"] = f"预检未完成: {error}"
    finally:
        log(
            "预检结果",
            f"{record['result']}; 差异 {record.get('diff_count')}, 可同步 {len(record.get('syncable_refs', {}))}, 冲突 {record.get('conflict_count', 0)}; 耗时 {monotonic() - started:.1f} 秒",
            repo,
        )
        if record.get("reason"):
            log("原因", record["reason"], repo)
        CURRENT_REPO.reset(token)
    return record


def check_with_deadline(repo, deadline):
    token = COMMAND_DEADLINE.set(min(deadline, monotonic() + REPOSITORY_SECONDS))
    try:
        if monotonic() >= deadline:
            log("预算耗尽", "跳过: 预检总时间预算已耗尽", repo)
            return {
                "repo": repo,
                "result": "预检失败",
                "diff_count": None,
                "failed_stage": "预检",
                "reason": "预检总时间预算已耗尽, 未检查此仓库",
            }
        return check(repo)
    finally:
        COMMAND_DEADLINE.reset(token)


def save_records(records):
    path = Path(os.environ["RESULT_FILE"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(records, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main():
    repos = json.loads(os.environ["EXPECTED_REPOS"])
    if not isinstance(repos, list) or any(
        not isinstance(repo, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        for repo in repos
    ):
        raise SystemExit("FORK_REPOS 必须是 owner/repo 字符串组成的 JSON 数组")
    repos = list(dict.fromkeys(repos))
    if len(repos) > 256:
        raise SystemExit("FORK_REPOS 超过 GitHub Actions 的 256 个矩阵任务限制")
    if not os.environ.get("GH_TOKEN"):
        raise SystemExit("请配置 FORK_SYNC_TOKEN secret")
    log(
        "预检配置",
        f"仓库数 {len(repos)}; 并发 3; 总预算 {PREFLIGHT_SECONDS} 秒; 单仓库预算 {REPOSITORY_SECONDS} 秒",
    )
    run("gh", "auth", "setup-git")
    deadline = monotonic() + PREFLIGHT_SECONDS
    records = {
        repo: {
            "repo": repo,
            "result": "预检失败",
            "diff_count": None,
            "failed_stage": "预检",
            "reason": "预检尚未完成, 任务可能已中断",
        }
        for repo in repos
    }
    save_records(list(records.values()))
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(check_with_deadline, repo, deadline): repo for repo in repos
        }
        for completed, future in enumerate(as_completed(futures), 1):
            record = future.result()
            records[record["repo"]] = record
            save_records(list(records.values()))
            log(
                "进度",
                f"{completed}/{len(repos)} 完成; 结果: {record['result']}; 结果已保存",
                record["repo"],
            )
    eligible = [repo for repo in repos if records[repo]["result"] == "待同步"]
    log(
        "矩阵",
        f"{len(eligible)}/{len(repos)} 个仓库进入同步矩阵: {json.dumps(eligible, ensure_ascii=False)}",
    )
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("repos=" + json.dumps(eligible, separators=(",", ":")) + "\n")
        output.write("has_work=" + str(bool(eligible)).lower() + "\n")


if __name__ == "__main__":
    main()
