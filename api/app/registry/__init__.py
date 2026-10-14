"""算子能力注册表模块（SPEC §3 M4，对应 FR-11、FR-14）。

对外仅暴露 ``router``，由 M1 控制面网关挂载到 ``/api/v1``。
"""

from app.registry.router import router

__all__ = ["router"]
