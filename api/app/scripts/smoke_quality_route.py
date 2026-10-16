"""在隔离演示表中验证 DataValidator → Doris 有效/错误数据分流。"""

import argparse
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

from app.core.config import settings
from app.services.seatunnel import UrllibSeatunnelTransport

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "deploy/seatunnel/mysql-to-doris.test.conf"
_SAFE_NAME = re.compile(r"ods_order_wide__(?:shadow|errors)_quality_[A-Za-z0-9_]{1,40}\Z")


def main() -> int:
    parser = argparse.ArgumentParser(description="仅写独立质量影子表与错误表")
    parser.add_argument("shadow_table")
    parser.add_argument("error_table")
    args = parser.parse_args()
    if not _SAFE_NAME.fullmatch(args.shadow_table) or not _SAFE_NAME.fullmatch(
        args.error_table
    ):
        parser.error("只允许 ods_order_wide__shadow/errors_quality_* 演示表")

    config = _CONFIG_PATH.read_text(encoding="utf-8")
    config = config.replace('  Jdbc {', '  Jdbc {\n    plugin_output = "source_orders"', 1)
    transform = f'''
transform {{
  TableRename {{
    plugin_input = "source_orders"
    plugin_output = "renamed_orders"
    replacements_with_regex = [
      {{ replace_from = "^orders$", replace_to = "{args.shadow_table}" }}
    ]
  }}
  DataValidator {{
    plugin_input = "renamed_orders"
    plugin_output = "validated_orders"
    row_error_handle_way = "ROUTE_TO_TABLE"
    row_error_handle_way.error_table = "{args.error_table}"
    field_rules = [
      {{ field_name = "user_name", rule_type = "REGEX",
         pattern = "^[^吴].*$",
         custom_message = "USER_NAME_REGEX" }}
    ]
  }}
}}
'''
    config = config.replace('\nsink {', '\n' + transform + '\nsink {', 1)
    config = config.replace('  Doris {', '  Doris {\n    plugin_input = "validated_orders"', 1)
    config = config.replace('table = "ods_order_wide_shadow"', 'table = "${table_name}"', 1)
    label = f"etl-quality-{uuid.uuid4().hex[:12]}"
    config = config.replace('e2e-docker-test', label, 1)

    transport = UrllibSeatunnelTransport(
        settings.seatunnel_base_url, settings.seatunnel_http_timeout_seconds
    )
    path = '/submit-job?' + urlencode({'format': 'hocon', 'jobName': label})
    reply = transport.request('POST', path, body=config)
    job_id = reply.get('jobId')
    if not job_id:
        raise RuntimeError('SeaTunnel 未返回 jobId')
    print(f'质量分流作业 jobId={job_id}')

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        info = transport.request('GET', f'/job-info/{job_id}')
        status = str(info.get('jobStatus') or '')
        print(f'状态：{status}')
        if status == 'FINISHED':
            print(f'源端读取：{(info.get("metrics") or {}).get("SourceReceivedCount", "未知")}')
            return 0
        if status in ('FAILED', 'CANCELED', 'UNKNOWABLE'):
            return 1
        time.sleep(3)

    transport.request(
        'POST', '/stop-job', body={'jobId': int(job_id), 'isStopWithSavePoint': False}
    )
    raise TimeoutError('质量分流作业超时，已请求停止')


if __name__ == '__main__':
    raise SystemExit(main())
