"""算子能力注册表（T-05 / M4）测试。

覆盖点：清单非空、P0 数量区间、每条算子属性清单完整、三个接口的返回结构、
按 type 过滤生效、未注册算子返回 404。

本测试**自建最小应用**挂载注册表路由（前缀 ``/api/v1``），不依赖 ``app.main`` ——
注册表是静态只读模块，其正确性不应耦合到主线装配进度。
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.registry import catalog
from app.registry.router import router
from app.registry.schemas import AttributeType, OperatorType

# 与生产装配保持一致：注册表挂到 /api/v1，自身前缀 /operators
_app = FastAPI()
_app.include_router(router, prefix="/api/v1")
client = TestClient(_app)

OPERATORS_URL = "/api/v1/operators"
SUMMARY_URL = "/api/v1/operators/summary"


# ---------- 数据层（catalog） ----------


def test_catalog_not_empty() -> None:
    """清单非空。"""
    assert catalog.OPERATOR_COUNT > 0
    assert len(catalog.list_operators()) == catalog.OPERATOR_COUNT


def test_p0_operator_count_in_expected_range() -> None:
    """P0 子集数量落在「约 10~15 个」的预期区间内。"""
    assert 10 <= catalog.OPERATOR_COUNT <= 15


def test_operator_ids_unique_and_well_formed() -> None:
    """算子标识唯一，且形如 seatunnel.<type>.<PluginName>，与 type 字段自洽。"""
    ids = [op.operator_id for op in catalog.list_operators()]
    assert len(ids) == len(set(ids)), "算子标识必须唯一"

    for op in catalog.list_operators():
        assert op.operator_id.startswith(f"seatunnel.{op.type.value}.")
        assert op.connector_artifact.startswith("connector-")
        assert op.description.strip()


def test_every_operator_has_complete_attribute_list() -> None:
    """每个算子的属性清单非空，且每条属性字段完整。"""
    for op in catalog.list_operators():
        assert op.attributes, f"{op.operator_id} 属性清单不得为空"
        for attr in op.attributes:
            assert attr.name.strip(), f"{op.operator_id} 存在空属性名"
            assert isinstance(attr.required, bool)
            assert isinstance(attr.type, AttributeType)
            assert attr.description.strip(), f"{op.operator_id}.{attr.name} 缺少说明"
            assert attr.default is None or isinstance(attr.default, str)
            # 枚举型属性必须给出可选值
            if attr.type is AttributeType.ENUM:
                assert attr.enum_values, f"{op.operator_id}.{attr.name} 为枚举但未给出可选值"


def test_attribute_names_within_operator_are_unique() -> None:
    """同一算子内属性名不得重复。"""
    for op in catalog.list_operators():
        names = [attr.name for attr in op.attributes]
        assert len(names) == len(set(names)), f"{op.operator_id} 属性名重复"


def test_required_attributes_have_no_default() -> None:
    """必填属性不应声明默认值（必填与默认值语义互斥）。"""
    for op in catalog.list_operators():
        for attr in op.attributes:
            if attr.required:
                assert attr.default is None, f"{op.operator_id}.{attr.name} 必填却带默认值"


def test_expected_p0_operators_present() -> None:
    """P0 覆盖范围的关键算子均已登记。"""
    expected = {
        "seatunnel.source.MySQL-CDC",
        "seatunnel.source.Postgres-CDC",
        "seatunnel.source.Jdbc",
        "seatunnel.sink.Jdbc",
        "seatunnel.source.Doris",
        "seatunnel.sink.Doris",
        "seatunnel.source.Clickhouse",
        "seatunnel.sink.Clickhouse",
        "seatunnel.source.LocalFile",
        "seatunnel.sink.LocalFile",
        "seatunnel.source.S3File",
        "seatunnel.sink.S3File",
        "seatunnel.source.Http",
    }
    registered = {op.operator_id for op in catalog.list_operators()}
    assert expected <= registered


def test_doris_sink_declares_dialect_constraints() -> None:
    """Doris Sink 必须记录 SPEC §11.1 V2 的两条方言约束。"""
    doris = catalog.get_operator("seatunnel.sink.Doris")
    assert doris is not None
    joined = " ".join(doris.dialect_constraints)
    assert "sink.label-prefix" in joined
    assert "唯一" in joined
    assert "table.identifier" in joined


def test_get_operator_miss_returns_none() -> None:
    """未注册算子返回 None（由路由层转换为 404）。"""
    assert catalog.get_operator("seatunnel.sink.NotExist") is None


# ---------- 接口层（router） ----------


def test_list_operators_endpoint() -> None:
    """GET /operators 返回精简列表，结构正确。"""
    resp = client.get(OPERATORS_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == catalog.OPERATOR_COUNT

    for item in body:
        assert set(item.keys()) == {"operator_id", "type", "connector_artifact", "description"}


def test_filter_operators_by_type() -> None:
    """按 type 过滤生效，且数量与数据层一致。"""
    for op_type in (OperatorType.SOURCE, OperatorType.SINK):
        resp = client.get(OPERATORS_URL, params={"type": op_type.value})
        assert resp.status_code == 200
        body = resp.json()
        assert body, f"{op_type.value} 过滤结果不应为空"
        assert all(item["type"] == op_type.value for item in body)
        assert len(body) == len(catalog.list_operators(op_type))

    sources = client.get(OPERATORS_URL, params={"type": "source"}).json()
    sinks = client.get(OPERATORS_URL, params={"type": "sink"}).json()
    assert len(sources) + len(sinks) == catalog.OPERATOR_COUNT


def test_list_operators_rejects_unknown_type() -> None:
    """非法 type 取值被拒（422）。"""
    resp = client.get(OPERATORS_URL, params={"type": "unknown"})
    assert resp.status_code == 422


def test_summary_endpoint_shape() -> None:
    """GET /operators/summary 返回极简清单（仅 标识 + 类型 + 描述），且未被路径参数吞掉。"""
    resp = client.get(SUMMARY_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == catalog.OPERATOR_COUNT

    for item in body:
        assert set(item.keys()) == {"operator_id", "type", "description"}
        assert item["description"]


def test_detail_endpoint_with_full_attributes() -> None:
    """GET /operators/{id} 返回含完整属性清单的详情。"""
    resp = client.get(f"{OPERATORS_URL}/seatunnel.sink.Doris")
    assert resp.status_code == 200
    body = resp.json()
    assert body["operator_id"] == "seatunnel.sink.Doris"
    assert body["type"] == "sink"
    assert body["connector_artifact"] == "connector-doris"
    assert body["seatunnel_version"] == "2.3.13"
    assert body["attributes"], "详情必须包含属性清单"

    for attr in body["attributes"]:
        assert set(attr.keys()) == {
            "name",
            "required",
            "default",
            "type",
            "enum_values",
            "description",
        }


def test_detail_endpoint_not_found() -> None:
    """未注册算子返回 404。"""
    resp = client.get(f"{OPERATORS_URL}/seatunnel.sink.NotExist")
    assert resp.status_code == 404


def test_router_has_no_auth_or_db_dependency() -> None:
    """注册表路由只读且无外部依赖：不得引入 DbSession / CurrentUser 等子依赖。"""
    assert len(router.routes) == 3
    for route in router.routes:
        assert route.dependant.dependencies == [], "注册表端点不应有子依赖（无鉴权 / 无数据库会话）"
