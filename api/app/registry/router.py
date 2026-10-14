"""算子能力注册表只读接口（SPEC §3 M4，对应 FR-11、FR-14）。

注册表是**静态只读**数据，因此本路由**不引入数据库会话（DbSession）与鉴权
（CurrentUser）依赖** —— 任何进程内一致性、无外部依赖的接口都不应把这两者作为前置。

挂载前缀 ``/operators``（由 M1 控制面网关以 ``/api/v1`` 接入，本模块不自行添加）。
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.registry import catalog
from app.registry.schemas import (
    OperatorBrief,
    OperatorDetail,
    OperatorSummary,
    OperatorType,
)

router = APIRouter(prefix="/operators", tags=["operators"])


@router.get("", response_model=list[OperatorSummary], summary="列出全部受管算子")
def list_operators(
    operator_type: Annotated[
        OperatorType | None,
        Query(alias="type", description="按算子类型过滤：source / sink / transform；缺省返回全部"),
    ] = None,
) -> list[OperatorDetail]:
    """列出受管算子（精简信息：标识 / 类型 / 实现制品 / 描述），支持按 ``type`` 过滤。"""
    return catalog.list_operators(operator_type)


@router.get(
    "/summary",
    response_model=list[OperatorBrief],
    summary="算子精简清单（供生成端注入大模型上下文）",
)
def operator_summary() -> list[OperatorDetail]:
    """返回「标识 + 类型 + 一句话描述」的精简清单（FR-11 / FR-14）。

    该清单是 M3 需求生成的**唯一合法算子来源**：模型只能从中选算子，选不中即须
    返回「当前算子不支持」并请求澄清，禁止臆造组件。

    注：本路由必须声明在 ``/{operator_id}`` **之前**，否则 ``summary`` 会被当作
    路径参数 ``operator_id`` 吞掉。
    """
    return catalog.list_operators()


@router.get("/{operator_id}", response_model=OperatorDetail, summary="算子详情（含完整属性清单）")
def operator_detail(operator_id: str) -> OperatorDetail:
    """按算子标识返回完整详情（属性清单 + 方言约束）；未注册返回 404。"""
    operator = catalog.get_operator(operator_id)
    if operator is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"算子未注册：{operator_id}")
    return operator
