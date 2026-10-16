"""提交本地演示 MySQL → SeaTunnel → Doris 作业，只写指定影子表。"""

import argparse
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from app.core.config import settings
from app.services.seatunnel import UrllibSeatunnelTransport

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "deploy/seatunnel/mysql-to-doris.test.conf"
_TABLE_IN_TEMPLATE = 'table = "ods_order_wide_shadow"'
_SAFE_NAME = re.compile(r"ods_order_wide__shadow_demo_[A-Za-z0-9_]{1,40}\Z")


def main() -> int:
    parser = argparse.ArgumentParser(description="只写独立演示影子表的 SeaTunnel 联调")
    parser.add_argument("target_table", help="预先创建的 ods_order_wide__shadow_demo_* 表")
    args = parser.parse_args()
    if not _SAFE_NAME.fullmatch(args.target_table):
        parser.error("目标表必须是 ods_order_wide__shadow_demo_*，不可指向正式表")

    config = _CONFIG_PATH.read_text(encoding="utf-8")
    if config.count(_TABLE_IN_TEMPLATE) != 1 or config.count("e2e-docker-test") != 1:
        raise ValueError("本地示例配置与预期模板不一致，拒绝提交")
    run_label = f"etl-demo-{uuid.uuid4().hex[:12]}"
    config = config.replace(_TABLE_IN_TEMPLATE, f'table = "{args.target_table}"')
    config = config.replace("e2e-docker-test", run_label)

    transport = UrllibSeatunnelTransport(
        settings.seatunnel_base_url, settings.seatunnel_http_timeout_seconds
    )
    path = "/submit-job?" + urlencode({"format": "hocon", "jobName": run_label})
    reply = transport.request("POST", path, body=config)
    job_id = reply.get("jobId")
    if not job_id:
        raise RuntimeError("SeaTunnel 未返回 jobId")
    print(f"已提交作业 jobId={job_id}，仅写 ods.{args.target_table}")

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        info = transport.request("GET", f"/job-info/{job_id}")
        status = str(info.get("jobStatus") or "")
        print(f"状态：{status}")
        if status == "FINISHED":
            metrics = info.get("metrics") or {}
            print(f"源端读取行数：{metrics.get('SourceReceivedCount', '未知')}")
            return 0
        if status in ("FAILED", "CANCELED", "UNKNOWABLE"):
            return 1
        time.sleep(3)

    transport.request(
        "POST", "/stop-job", body={"jobId": int(job_id), "isStopWithSavePoint": False}
    )
    raise TimeoutError("演示作业超时，已请求停止")


if __name__ == "__main__":
    raise SystemExit(main())
