"""默认离线 L0；显式 --backend deepseek 才调用真实模型，--persist 才写数据库。"""

import argparse
import json
import sys

from app.core.errors import AppError
from app.services.benchmark import DatasetError, load_dataset, run_benchmark


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="版本化 Benchmark：L0 自检 / L2 真实生成评测")
    parser.add_argument("--dataset", default="v1")
    parser.add_argument(
        "--backend",
        choices=("injected", "deepseek"),
        default="injected",
        help="deepseek 会消耗模型额度；只执行生成图，不提交数据作业",
    )
    parser.add_argument("--persist", action="store_true", help="将报告写入已迁移的控制面数据库")
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(load_dataset(args.dataset), backend=args.backend)
    except (DatasetError, AppError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    result = report.to_dict()
    if args.persist:
        from app.db.session import SessionLocal
        from app.services.benchmark_store import save_report

        with SessionLocal() as db:
            result["run_id"] = str(save_report(db, report).id)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
