"""T-20 审计查询接口（API-11 ~ API-13，FR-39 / FR-40）。

这里最有分量的一条是 ``test_report_is_self_contained_and_recomputable``：
它**不调用被测代码的任何哈希函数**，只用报告自身公布出来的配方与公钥，把整条链
重算一遍。一份需要回查数据库才能验证的报告不算审计报告——那正是这条测试要挡住
的退化。
"""

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.keys import SigningKey
from app.services import audit, audit_verifier
from app.services.audit import record_audit

_KEY = SigningKey("api-key-1", Ed25519PrivateKey.generate())


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    """四处取密钥的入口都要禁掉。

    报告装配与路由各自 ``from app.core.keys import get_signing_key``，只禁服务层
    的两处会让报告里公布的公钥与检查点实际用的密钥不是同一把——那正是这把测试
    要挡住的不一致。
    """
    monkeypatch.setattr(audit, "get_signing_key", lambda: _KEY)
    monkeypatch.setattr(audit_verifier, "get_signing_key", lambda: _KEY)
    monkeypatch.setattr("app.services.audit_report.get_signing_key", lambda: _KEY)
    monkeypatch.setattr("app.api.routes.audit.get_signing_key", lambda: _KEY)
    return _KEY


@pytest.fixture()
def project(client, auth_headers):
    headers = auth_headers("auditor@example.com")
    resp = client.post("/api/v1/projects", json={"name": "审计项目"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


def _seed(session_factory, project_id: str, count: int = 3) -> list[uuid.UUID]:
    with session_factory() as db:
        for index in range(count):
            record_audit(
                db,
                event_type="test.event" if index % 2 == 0 else "test.other",
                actor="user:00000000-0000-0000-0000-000000000001",
                project_id=uuid.UUID(project_id),
                payload={"index": index},
            )
        db.commit()
    return []


def test_records_require_authentication(client, project):
    project_id, _ = project
    resp = client.get(f"/api/v1/audit/records?project_id={project_id}")
    assert resp.status_code == 401


def test_records_are_scoped_to_the_project(client, auth_headers, project):
    """跨租户读不到别人的证据——隔离在 SQL 层，不是查完再在 Python 里过滤。"""
    project_id, _ = project
    other_headers = auth_headers("outsider@example.com")
    resp = client.get(
        f"/api/v1/audit/records?project_id={project_id}", headers=other_headers
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_CROSS_TENANT"


def test_records_support_filters_and_keyset_paging(client, session_factory, project):
    project_id, headers = project
    _seed(session_factory, project_id, count=5)

    resp = client.get(
        f"/api/v1/audit/records?project_id={project_id}&event_type=test.event",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [item["event_type"] for item in body["items"]] == ["test.event"] * 3
    assert body["head"]["seq"] == 5

    # keyset 分页：游标指向一个确定的位置，不会被并发追加影响
    first = client.get(
        f"/api/v1/audit/records?project_id={project_id}&limit=2", headers=headers
    ).json()
    assert len(first["items"]) == 2
    assert first["next_after_seq"] == first["items"][-1]["seq"]

    second = client.get(
        f"/api/v1/audit/records?project_id={project_id}&limit=2"
        f"&after_seq={first['next_after_seq']}",
        headers=headers,
    ).json()
    assert [item["seq"] for item in second["items"]] == [3, 4]


def test_verification_reports_the_chain_state(client, session_factory, project):
    project_id, headers = project
    _seed(session_factory, project_id, count=2)

    resp = client.get(
        f"/api/v1/audit/verification?project_id={project_id}&refresh=true",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "verified"
    assert body["checked"] == 2
    assert body["stale"] is False
    # 公钥随结论一起返回：调用方与离线复算方必须用同一把，口径才可比
    assert body["public_key"]["kid"] == "api-key-1"
    assert body["public_key"]["pem"].startswith("-----BEGIN PUBLIC KEY-----")


def test_report_is_self_contained_and_recomputable(client, session_factory, project):
    """**用报告自己公布的配方重算整条链。**

    这条测试里没有一处调用被测代码的哈希函数——字段序、序列化参数、时间格式、
    创世哈希全部从报告里读。它通过，才说明「第三方拿着这份文件就能独立验证」
    不是一句自我描述。
    """
    project_id, headers = project
    _seed(session_factory, project_id, count=3)

    resp = client.get(f"/api/v1/audit/report?project_id={project_id}", headers=headers)
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    report = resp.json()

    chain = report["chain"]
    assert chain["algorithm"] == "sha256"
    assert report["report_version"] == "audit-report-v1"

    expected_prev = chain["genesis_hash"]
    for item in report["timeline"]:
        # 严格按报告公布的字段序与序列化参数拼材料
        material = {
            "chain_id": chain["chain_id"],
            "seq": item["seq"],
            "event_type": item["event_type"],
            "actor": item["actor"],
            "project_id": item["project_id"],
            "run_id": item["run_id"],
            "approval_id": item["approval_id"],
            "version_id": item["version_id"],
            "preparation_id": item["preparation_id"],
            "occurred_at": (
                datetime.fromisoformat(item["occurred_at"])
                .astimezone(UTC)
                .strftime(chain["timestamp_format"])
            ),
            "payload": item["payload"],
            "prev_hash": item["prev_hash"],
        }
        digest = hashlib.sha256(
            json.dumps(
                material, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()

        assert item["prev_hash"] == expected_prev
        assert item["entry_hash"] == digest
        expected_prev = item["entry_hash"]

    assert chain["head_hash"] == expected_prev

    # 检查点用报告里的公钥验签——不碰进程内的签名密钥
    from cryptography.hazmat.primitives import serialization

    public_key = serialization.load_pem_public_key(chain["public_key"]["pem"].encode())
    for checkpoint in report["checkpoints"]:
        version, kid, encoded = checkpoint["signature"].split(":", 2)
        assert version == chain["signature_version"]
        assert kid == chain["public_key"]["kid"]
        message = json.dumps(
            {
                "chain_id": chain["chain_id"],
                "seq": checkpoint["seq"],
                "entry_hash": checkpoint["entry_hash"],
                "prev_anchor_hash": chain["genesis_hash"],
                "anchored_at": (
                    datetime.fromisoformat(checkpoint["anchored_at"])
                    .astimezone(UTC)
                    .strftime(chain["timestamp_format"])
                ),
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        # 验签失败会抛 InvalidSignature，测试因此失败
        public_key.verify(raw, message)


def test_report_marks_truncation_instead_of_hiding_it(
    client, session_factory, project, monkeypatch
):
    """被截断的报告必须说自己被截断了。

    一份看不出自己不全的报告比没有报告更危险——它会让人以为「这就是全部证据」。
    """
    from app.core.config import settings

    project_id, headers = project
    _seed(session_factory, project_id, count=4)
    monkeypatch.setattr(settings, "audit_report_max_records", 2)

    body = client.get(
        f"/api/v1/audit/report?project_id={project_id}", headers=headers
    ).json()

    assert body["scope"]["truncated"] is True
    assert body["scope"]["record_count"] == 2


def test_the_audit_surface_is_read_only(client, project):
    """审计面没有任何写方法——一段被改过的历史不该能通过接口再写回去。"""
    project_id, headers = project
    assert client.post("/api/v1/audit/records", headers=headers).status_code == 405
    assert client.put("/api/v1/audit/report", headers=headers).status_code == 405
    assert client.delete("/api/v1/audit/verification", headers=headers).status_code == 405


def test_exporting_is_itself_recorded(client, session_factory, project):
    """「谁批量取走了证据」本身是审计事实。

    「查询不限角色」的补偿控制就在这里：不加一道能被绕过的角色门槛，而是让
    导出这个动作留下痕迹。
    """
    project_id, headers = project
    _seed(session_factory, project_id, count=1)

    client.get(f"/api/v1/audit/report?project_id={project_id}", headers=headers)

    body = client.get(
        f"/api/v1/audit/records?project_id={project_id}&event_type=audit.exported",
        headers=headers,
    ).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["payload"]["truncated"] is False
