"""走公开 API 跑一次完整的受管演示：质量契约 → 冻结 → 双维审批 → Commit → 执行。

【为什么需要这个脚本】

质量规则里的中文必须**原样**进入控制面。2026-10-15 那次演示声明的规则是
``^[^吴].*$``，但提交时经过 Windows 的 GBK 转换变成了 ``^[^?].*$``——
数据库里实测 8 字节，而 ``吴`` 是 3 字节、正确的写法是 10 字节。

正则语义于是从「不以**吴**开头」悄悄变成「不以**问号**开头」：8 行源数据
全部通过校验，分流从未发生，而运行状态仍然是 ``succeeded``。控制系统没有
任何一处报错，因为**被改写的是一份语法完全合法的契约**。

因此规则写在 Python 源文件里、由 JSON 层负责编码，不经过 shell 的字符集；
提交后再回读一次并逐字比对——把一个只发生过一次的坑变成每次都执行的检查。

用法：
    python -m app.scripts.run_demo_managed_flow <source_id> <target_id> <target_table>

``target_table`` 必须匹配 ``ods_order_wide__formal_pipeline_demo_*``——
独立演示表，不碰正式表。
"""

import argparse
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.version import PipelineVersion, VersionStatus

API_ROOT = Path(__file__).resolve().parents[2]
BASE_URL = "http://127.0.0.1:8000/api/v1"
DEMO_PASSWORD = "Demo@12345"

# 演示账号（README 有登记）。审批人按维度取用，职责分离由服务端强制。
ACCOUNTS = {
    "maker": "maker@example.com",
    "operator": "operator@example.com",
    "data": "checker.data@example.com",
    "security": "checker.security@example.com",
}

# **由数据工程师显式声明**的质量契约（FR-32）。源端 1008 号的用户名为「吴十」，
# 这一条把它挡在正式表之外：8 行源数据 → 7 行有效 + 1 行错误。
DEMO_RULES: list[dict[str, Any]] = [
    {
        "field": "user_name",
        "rule": "regex",
        "params": {"pattern": "^[^吴].*$"},
        "error_code": "USER_NAME_REGEX",
        "action": "reject",
    }
]

_TERMINAL = {"succeeded", "failed", "cancelled"}


def _login(email: str) -> str:
    reply = requests.post(
        f"{BASE_URL}/auth/login",
        json={"email": email, "password": DEMO_PASSWORD},
        timeout=30,
    )
    reply.raise_for_status()
    return reply.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _wait_for_run(token: str, run_id: str, timeout: float = 600.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        reply = requests.get(f"{BASE_URL}/runs/{run_id}", headers=_auth(token), timeout=30)
        reply.raise_for_status()
        run = reply.json()
        if run["status"] != last:
            print(f"  运行状态：{run['status']}")
            last = run["status"]
        if run["status"] in _TERMINAL:
            return run
        time.sleep(3)
    raise TimeoutError(f"运行 {run_id} 在 {timeout:.0f} 秒内未进入终态")


def _draft_version_id(target_table: str) -> uuid.UUID:
    """取刚由种子脚本建出的草稿版本。

    **只读这一处**：写操作一律走公开 API，这里读的只是一个标识符——
    种子脚本刻意只产草稿（"审批与执行仍必须走公开 API"），我们遵守它。
    """
    with SessionLocal() as db:
        versions = list(db.scalars(select(PipelineVersion)))
    for version in sorted(versions, key=lambda item: item.version_no, reverse=True):
        plan = version.etl_plan or {}
        if plan.get("target_table") == target_table and version.status is VersionStatus.DRAFT:
            return version.id
    raise LookupError(f"未找到目标表为 {target_table} 的草稿版本")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="受管演示：质量契约到发布的全链路")
    parser.add_argument("source_id", type=uuid.UUID)
    parser.add_argument("target_id", type=uuid.UUID)
    parser.add_argument("target_table")
    parser.add_argument(
        "--skip-seed", action="store_true", help="复用已存在的草稿版本，不新建"
    )
    args = parser.parse_args()

    tokens = {name: _login(email) for name, email in ACCOUNTS.items()}
    print("已登录 4 个演示账号（maker / operator / 数据审批 / 安全审批）")

    if not args.skip_seed:
        # 固定方案种子只产草稿；中文不经这里。
        subprocess.run(
            [
                sys.executable, "-m", "app.scripts.seed_demo_pipeline",
                str(args.source_id), str(args.target_id), args.target_table,
                # 门禁不过的草稿会被标记 rejected 并留在库里，而种子脚本默认
                # 「目标表已有版本就不再创建」。没有这个开关，演示脚本第二次
                # 就只会返回那份已经作废的草稿。种子脚本自己会拒绝「有活跃
                # 运行」的情况，所以这个开关不会把重复写入变成可能。
                "--retry-failed",
            ],
            check=True, cwd=str(API_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace",
        )
        print(f"已生成草稿版本（目标表 {args.target_table}）")

    version_id = _draft_version_id(args.target_table)

    # ---- 1. 提交质量契约 -------------------------------------------------
    reply = requests.put(
        f"{BASE_URL}/versions/{version_id}/quality-rules",
        headers=_auth(tokens["maker"]),
        json={"rules": DEMO_RULES},
        timeout=30,
    )
    reply.raise_for_status()

    # ---- 2. 回读校验：确认中文没有被传输链路改写 --------------------------
    stored = reply.json()["quality_rules"]
    expected = DEMO_RULES[0]["params"]["pattern"]
    actual = stored[0]["params"]["pattern"]
    print(
        f"质量规则已落库：{actual!r} "
        f"（{len(actual)} 字符 / {len(actual.encode('utf-8'))} 字节）"
    )
    if actual != expected:
        raise SystemExit(
            f"质量规则在传输中被改写：期望 {expected!r}，控制面存的是 {actual!r}。\n"
            "这正是 2026-10-15 那次「规则存在却零分流」的成因，拒绝继续。"
        )

    # ---- 3. 冻结（确定性门禁）-------------------------------------------
    reply = requests.post(
        f"{BASE_URL}/versions/{version_id}/freeze",
        headers=_auth(tokens["maker"]), timeout=60,
    )
    reply.raise_for_status()
    frozen = reply.json()
    print(f"版本已冻结 v{frozen['version_no']} 指纹={frozen['sha256'][:16]}…")

    # ---- 4. 准备单（零副作用）+ 双维独立审批 ------------------------------
    reply = requests.post(
        f"{BASE_URL}/preparations",
        headers=_auth(tokens["maker"]),
        json={"version_id": str(version_id)}, timeout=60,
    )
    reply.raise_for_status()
    preparation = reply.json()
    print(
        f"准备单 {preparation['id']} 风险={preparation['risk_level']} "
        f"影子表={preparation['impact_scope']['shadow_table']}"
    )
    budget = preparation["budget"]
    print(
        f"  预算：行数<={budget.get('max_rows')} 字节<={budget.get('max_bytes')} "
        f"时长<={budget.get('max_duration_seconds')}s "
        f"放大比<={budget.get('max_amplification_ratio')} "
        f"拒绝率<={budget.get('max_reject_ratio')}"
    )

    for approval in preparation["approvals"]:
        # 维度决定由谁签：数据维度与安全维度互相独立、互不替代（FR-23）
        signer = "data" if approval["dimension"] == "data" else "security"
        reply = requests.post(
            f"{BASE_URL}/approvals/{approval['id']}/decide",
            headers=_auth(tokens[signer]),
            json={"decision": "approved", "comment": f"{signer} 维度审批通过（演示）"},
            timeout=30,
        )
        reply.raise_for_status()
        print(f"  {approval['dimension']} 维度已批准")

    reply = requests.get(
        f"{BASE_URL}/preparations/{preparation['id']}",
        headers=_auth(tokens["maker"]), timeout=30,
    )
    reply.raise_for_status()
    missing = reply.json()["missing_dimensions"]
    if missing:
        raise SystemExit(f"审批未齐，仍缺少：{missing}")

    # ---- 5. Commit：复核指纹 → 签发令牌 → 落运行事实与 Outbox --------------
    reply = requests.post(
        f"{BASE_URL}/commits",
        headers=_auth(tokens["operator"]),
        json={
            "preparation_id": preparation["id"],
            "expected_fingerprint": preparation["fingerprint"],
        },
        timeout=60,
    )
    reply.raise_for_status()
    accepted = reply.json()
    run_id = accepted["run_id"]
    print(f"Commit 已受理 run={run_id} 状态={accepted['status']}")

    # ---- 6. 等执行层跑完 -------------------------------------------------
    run = _wait_for_run(tokens["operator"], run_id)

    print("\n================ 运行结论 ================")
    print(f"状态         : {run['status']}")
    print(f"读取行数     : {run['read_rows']}")
    print(f"写入字节     : {run['written_bytes']}")
    print(f"拒绝行数     : {run['rejected_rows']}")
    print(f"发布时刻     : {run['published_at'] or '(未发布)'}")
    if run.get("error"):
        print(f"错误         : {run['error']}")
    report = run.get("quality_report")
    if report:
        print("质量报告     :")
        print(f"  source_rows   = {report['source_rows']}")
        print(f"  valid_rows    = {report['valid_rows']}")
        print(f"  rejected_rows = {report['rejected_rows']}")
        print(f"  rejected_ratio= {report['rejected_ratio']}")
        print(f"  error_dist    = {report['error_dist']}")
        print(f"  error_table   = {report['error_table']}")
    else:
        print("质量报告     : (无——本次运行没有质量契约)")

    ok = (
        run["status"] == "succeeded"
        and run["published_at"] is not None
        and run["read_rows"] == 8
        and run["rejected_rows"] == 1
        and report is not None
        and report["valid_rows"] == 7
    )
    print("\n7/1 分流核对 :", "通过" if ok else "未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
