import json
import os
from collections import Counter
from pathlib import Path

if __package__:
    from .workflow_log import log
else:
    from workflow_log import log

# At most 256 rows: bound each cell before Markdown escaping to stay below 1 MiB.
REASON_BYTES = 1024


def summary_reason(record):
    reason = record.get("reason", "")
    count = record.get("conflict_count")
    if count:
        reason = "; ".join(reason.split("; ", 5)[:5])
        if count > 5:
            reason = f"共 {count} 项冲突; " + reason
    encoded = reason.encode("utf-8")
    if len(encoded) > REASON_BYTES:
        reason = encoded[:REASON_BYTES].decode("utf-8", errors="ignore") + "..."
    if reason != record.get("reason", ""):
        reason += " (完整原因见结果 artifact)"
    reason = (
        reason.replace("&", "&amp;")
        .replace("|", "&#124;")
        .replace("\n", "<br>")
        .replace("\r", "")
    )
    if len(reason.encode("utf-8")) > 2048:
        return (
            reason.encode("utf-8")[:2048].decode("utf-8", errors="ignore")
            + "... (完整原因见结果 artifact)"
        )
    return reason


def main():
    expected = list(dict.fromkeys(json.loads(os.environ["EXPECTED_REPOS"])))
    records = {}
    log(
        "汇总",
        f"预期 {len(expected)} 个仓库; 结果目录 {os.environ['RESULTS_DIR']}; 下载状态 {os.environ['DOWNLOAD_OUTCOME']}",
    )
    preflight = Path(os.environ["RESULTS_DIR"]) / "fork-sync-preflight.json"
    if preflight.exists():
        for record in json.loads(preflight.read_text(encoding="utf-8")):
            if record["result"] == "待同步":
                record.update(
                    result="缺少结果", reason="预检通过, 但未收到同步任务结果"
                )
            records[record["repo"]] = record
    for path in Path(os.environ["RESULTS_DIR"]).glob("fork-sync-result-*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        records[record["repo"]] = record
        log("读取结果", f"{path.name}: {record['result']}", record["repo"])

    rows = [
        records.get(repo, {"repo": repo, "result": "缺少结果"}) for repo in expected
    ]
    counts = Counter(row["result"] for row in rows)
    for row in rows:
        log(
            "仓库结果",
            f"{row['result']}; 成功 {row.get('synced_count', 0)}, 冲突 {row.get('conflict_count', 0)}, 失败 {row.get('failed_count', 0)}",
            row["repo"],
        )
    log("汇总统计", json.dumps(dict(counts), ensure_ascii=False))
    statuses = [
        "同步成功",
        "部分同步",
        "全部冲突",
        "无需同步",
        "失败",
        "已取消",
        "未完成",
        "无法同步",
        "预检失败",
        "缺少结果",
    ]
    lines = [
        "# Fork 同步汇总",
        "",
        f"共 {len(rows)} 个仓库.",
        "",
        "| 状态 | 仓库数 |",
        "| --- | ---: |",
        *(f"| {status} | {counts[status]} |" for status in statuses),
        "",
        "| 仓库 | 上游 | 结果 | 差异引用数 | 已同步 | 冲突跳过 | 推送失败 | 失败阶段 | 退出码 | 原因 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for row in rows:
        reason = summary_reason(row)
        repo = row["repo"]
        upstream = row.get("upstream")
        if upstream:
            url = upstream.removesuffix(".git")
            upstream = f"[{url.removeprefix('https://github.com/')}]({url})"
        count = row.get("diff_count")
        lines.append(
            f"| [{repo}](https://github.com/{repo}) | {upstream or '-'} "
            f"| {row['result']} | {count if count is not None else '-'} "
            f"| {row.get('synced_count', 0)} | {row.get('conflict_count', 0)} | {row.get('failed_count', 0)} "
            f"| {row.get('failed_stage') or '-'} | {row.get('exit_code') or '-'} | {reason or '-'} |"
        )
    lines.extend(
        [
            "",
            "没有可同步引用的仓库不创建同步任务. 引用明细及错误信息见结果 artifact 或各仓库任务日志.",
        ]
    )
    if os.environ["DOWNLOAD_OUTCOME"] != "success":
        lines.extend(["", "结果文件下载失败, 请检查下载步骤的任务日志."])
    content = "\n".join(lines) + "\n"
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(content)

    log(
        "汇总",
        f"已生成 Actions 汇总, 大小 {len(content.encode('utf-8'))} 字节",
    )

    if (
        counts["预检失败"]
        or counts["缺少结果"]
        or os.environ["DOWNLOAD_OUTCOME"] != "success"
    ):
        raise SystemExit(
            "存在预检失败或汇总结果不完整, 请检查预检及结果文件的上传, 下载步骤."
        )


if __name__ == "__main__":
    main()
