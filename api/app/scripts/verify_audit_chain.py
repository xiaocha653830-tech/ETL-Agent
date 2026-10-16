"""校验证据链（M11，T-20 / FR-39）。

容器启动脚本与运维都能用：跑一次完整校验，把结论打到标准输出，
**链不通过时以非零退出码结束**——只有这样才能把它当成一道启动门禁或健康探针。

用法：
    python -m app.scripts.verify_audit_chain            # 只校验并打印
    python -m app.scripts.verify_audit_chain --record   # 顺便把结论写进校验历史

与 ``app.main`` 启动期那次校验的区别：那里**不阻断启动**（把检测结果变成
可用性故障等于给攻击者一个 DoS 杠杆），而这里是一个显式的、由人发起的动作——
它失败就该失败，退出码才是有意义的信号。
"""

import argparse
import json
import sys

from app.db.session import SessionLocal
from app.models.audit import IntegrityStatus
from app.services.audit_verifier import record_verification, verify_chain


def main() -> int:
    parser = argparse.ArgumentParser(description="校验证据账本哈希链")
    parser.add_argument(
        "--record", action="store_true", help="把本次结论写入校验历史"
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    with SessionLocal() as db:
        result = verify_chain(db, trigger="manual")
        if args.record:
            record_verification(db, result, trigger="manual")

    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))

    if result.status is IntegrityStatus.BREACHED:
        print(
            f"证据链校验不通过：自 seq={result.first_failure_seq} 起；"
            "授权类操作（Commit / 回滚）在此之前会被拒绝",
            file=sys.stderr,
        )
        return 1
    if result.status is IntegrityStatus.INDETERMINATE:
        print(
            "哈希链逐条完好，但存在无法验证的签名检查点（通常是密钥轮换）；"
            "这不等于被篡改，也不会阻断授权",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
