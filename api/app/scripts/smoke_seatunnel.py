"""用 FakeSource → Console 验证 Zeta REST V2，不触碰业务数据。"""

import time
import uuid
from urllib.parse import urlencode

from app.core.config import settings
from app.services.seatunnel import UrllibSeatunnelTransport

_CONFIG = """
env {
  job.mode = "BATCH"
}
source {
  FakeSource {
    plugin_output = "smoke"
    row.num = 1
    schema {
      fields {
        id = "int"
      }
    }
  }
}
sink {
  Console {
    plugin_input = ["smoke"]
  }
}
"""


def main() -> int:
    transport = UrllibSeatunnelTransport(
        settings.seatunnel_base_url, settings.seatunnel_http_timeout_seconds
    )
    job_name = f"etl-rest-smoke-{uuid.uuid4().hex[:8]}"
    path = "/submit-job?" + urlencode({"format": "hocon", "jobName": job_name})
    reply = transport.request("POST", path, body=_CONFIG)
    job_id = reply.get("jobId")
    if not job_id:
        raise RuntimeError("SeaTunnel 未返回 jobId")
    print(f"已提交 Console 测试作业 jobId={job_id}")

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        info = transport.request("GET", f"/job-info/{job_id}")
        status = str(info.get("jobStatus") or "")
        print(f"状态：{status}")
        if status == "FINISHED":
            return 0
        if status in ("FAILED", "CANCELED", "UNKNOWABLE"):
            return 1
        time.sleep(2)

    transport.request(
        "POST", "/stop-job", body={"jobId": int(job_id), "isStopWithSavePoint": False}
    )
    raise TimeoutError("测试作业超时，已请求停止")


if __name__ == "__main__":
    raise SystemExit(main())
