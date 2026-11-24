from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .config import METRICS_PATH, REPORT_PATH


def _pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"


def _min(sec: float) -> str:
    return f"{sec / 60:.1f} 分钟"


def render_markdown(metrics: dict) -> str:
    s = metrics["scale"]
    safe = metrics["safe"]
    fast = metrics["fast"]
    amort = metrics["nightly_amortized"]
    acc = metrics["accuracy"]
    det = metrics["detection"]

    lines = []
    lines.append("# CI 智能选词与不稳定测试报告")
    lines.append("")
    lines.append(
        f"模拟仓库包含 **{s['modules']}** 个模块、**{s['tests']:,}** 个测试；"
        f"回放 **{s['days']}** 天 / **{s['commits']}** 次提交。"
        f"基线全量流水线约 **{_min(s['baseline_wall_sec'])}**。"
    )
    lines.append("")

    lines.append("## 1. 选词与耗时")
    lines.append("")
    lines.append("| 策略 | 平均测试数 | 跳过比例 | 平均反馈耗时 | 耗时下降 | 新破坏漏检率 | 全部真实失败漏检率 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    lines.append(
        f"| 快速档 | {fast['avg_selected']:,.0f} | {_pct(fast['skip_ratio'])} | "
        f"{_min(fast['avg_duration_sec'])} | {_pct(fast['duration_reduction'])} | "
        f"{_pct(fast['new_failure_miss_rate'])} | {_pct(fast['true_failure_miss_rate'])} |"
    )
    lines.append(
        f"| **安全档（PR 默认）** | **{safe['avg_selected']:,.0f}** | "
        f"**{_pct(safe['skip_ratio'])}** | **{_min(safe['avg_duration_sec'])}** | "
        f"**{_pct(safe['duration_reduction'])}** | "
        f"**{_pct(safe['new_failure_miss_rate'])}** | "
        f"**{_pct(safe['true_failure_miss_rate'])}** |"
    )
    lines.append(
        f"| 全量 | {s['tests']:,} | 0.0% | {_min(s['baseline_wall_sec'])} | 0.0% | 0.0% | 0.0% |"
    )
    lines.append("")
    lines.append(
        f"加入每天夜里 **{s['nightly_full_runs']}** 次全量兜底并把成本摊到提交后，"
        f"平均每次提交仍执行 **{amort['avg_tests_per_commit']:,.0f}** 个测试，"
        f"总 CI 耗时/资源下降 **{_pct(amort['duration_reduction'])}**。"
        "夜间全量会在第二天提供漏检反馈并自动校准风险模型。"
    )
    lines.append("")

    lines.append("## 2. 漏检估计与实际校准")
    lines.append("")
    lines.append(
        f"- 安全档实际漏掉的“新引入真实失败”比例：**{_pct(safe['new_failure_miss_rate'])}**"
        f"（{safe['missed_new_true_failures']}/{safe['new_true_failures']}）。"
    )
    lines.append(
        f"- 若把同一破坏在后续提交上的重复失败也算入，最近失败必选规则使总漏检率为 "
        f"**{_pct(safe['true_failure_miss_rate'])}**"
        f"（{safe['missed_true_failures']}/{safe['all_true_failures']}）。"
    )
    lines.append(
        f"- 风险模型每次提交平均预测漏检数：**{safe['avg_predicted_missed_per_commit']:.3f}**；"
        "页面同时给出基于夜间全量反馈的 Wilson 95% 漏检率上界。"
    )
    lines.append("")

    lines.append("## 3. 不稳定测试识别")
    lines.append("")
    lines.append(
        f"识别精确率 **{_pct(acc['precision'])}**，召回率 **{_pct(acc['recall'])}**，"
        f"F1 **{acc['f1']:.3f}**，整体准确率 **{_pct(acc['accuracy'], 2)}**"
        f"（TP={acc['tp']}, FP={acc['fp']}, FN={acc['fn']}, TN={acc['tn']}）。"
    )
    lines.append(
        f"共识别 {det['detected_count']} 个不稳定/转变测试；"
        f"原因 Top-1 准确率为 **{_pct(acc['cause_accuracy'])}**"
        f"（{acc['cause_correct']}/{acc['cause_total']}）。"
    )
    lines.append(
        f"“先不稳定、后来被提交弄坏”的 {acc['transition_total']} 个测试中，"
        f"{acc['transition_correct']} 个被最终改判为 flaky_then_broken。"
    )
    lines.append("")

    lines.append("## 4. 提交破坏是否被错当成不稳定")
    lines.append("")
    lines.append(
        f"- 普通提交破坏测试对：{acc['bad_episode_test_pairs']}；"
        f"在破坏窗口内被错误隔离且未先报 deterministic break 的数量："
        f"**{acc['wrong_masked_bad_pairs']}**。"
    )
    lines.append(
        f"- 先 flaky 后 broken、但在破坏后 12 次运行内未升级的数量："
        f"**{acc['transition_not_escalated_12_runs']}**。"
    )
    lines.append("")
    lines.append("判定原则：同一 SHA 稳定翻转、重跑恢复、失败离散 -> flaky；"
                 "从某提交边界开始连续高失败率、重跑仍失败 -> broken / flaky_then_broken。")
    lines.append("")

    lines.append("## 5. 隔离与自动恢复")
    lines.append("")
    for event, count in sorted(metrics.get("quarantine_events", {}).items()):
        lines.append(f"- `{event}`: {count}")
    lines.append("")
    lines.append("当前 finding 状态分布：")
    for status, count in sorted(metrics.get("current_status", {}).items()):
        lines.append(f"- `{status}`: {count}")
    lines.append("")

    lines.append("## 6. 不稳定原因分布（模拟真值）")
    lines.append("")
    cause_labels = {
        "order_dependence": "执行顺序依赖",
        "concurrency": "并发/竞态",
        "time_or_timeout": "时间/超时",
        "shared_state": "共享状态",
        "external_dependency": "外部依赖",
    }
    for code, count in sorted(metrics.get("cause_distribution_truth", {}).items(), key=lambda x: -x[1]):
        lines.append(f"- {cause_labels.get(code, code)}: {count}")
    lines.append("")

    lines.append("## 7. 给开发者的单次运行报告示例字段")
    lines.append("")
    lines.append("每次运行结束后，系统会说明：")
    lines.append("")
    lines.append("- 本次从 20,400 个测试中跳过了多少、预计节省多少分钟；")
    lines.append("- 哪些失败是提交引入的真实失败，对应哪个变更边界；")
    lines.append("- 哪些失败来自已知隔离 flaky 测试，不阻塞合并但继续记录；")
    lines.append("- 是否出现“隔离测试转为持续确定性失败”，若是则升级为真实破坏；")
    lines.append("- 夜间全量是否补抓到漏检，并用于校准下一次选词。")
    lines.append("")
    return "\n".join(lines)


def write_report(metrics: Optional[dict] = None, metrics_path: Path = METRICS_PATH,
                 report_path: Path = REPORT_PATH) -> str:
    if metrics is None:
        with metrics_path.open("r", encoding="utf-8") as f:
            metrics = json.load(f)
    markdown = render_markdown(metrics)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(markdown, encoding="utf-8")
    return markdown
