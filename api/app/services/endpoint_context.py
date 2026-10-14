"""数据源上下文（M2 → M3 的传递结构）。

生成阶段需要知道两端的表结构与字段分级，但**不需要、也不允许拿到凭据**。
本结构只承载非敏感连接参数与探查产出，因此可以安全地进入提示词与日志。

字段分级的来源是 T-03/T-04 产出的 ``METADATA_PROFILE``，
它让方案生成阶段就能对敏感字段保持警觉（并在 T-11 的风险评级中复用）。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EndpointContext:
    """一端的数据源上下文。"""

    datasource_id: str
    name: str
    type: str
    config: dict[str, Any] = field(default_factory=dict)
    # 来自某个 METADATA_PROFILE 的 schema_json["tables"]
    tables: list[dict[str, Any]] = field(default_factory=list)
    # 本上下文依据的探查记录 id。生成时绑定、冻结时按此还原，
    # 避免「生成看的是 Profile v1、冻结时读到的却是重新探查后的 v2」。
    profile_id: str | None = None

    @property
    def table_names(self) -> list[str]:
        return [str(table.get("name", "")) for table in self.tables]

    def to_prompt_text(self, *, max_tables: int = 20, max_columns: int = 40) -> str:
        """渲染成给模型看的文本。

        刻意**不含样本值**（哪怕已脱敏）——样本对"该同步哪些字段"没有帮助，
        只会稀释上下文、增加模型编造字段的机会。
        """
        connection = "、".join(
            f"{key}={value}" for key, value in self.config.items() if key != "connect_args"
        )
        lines = [f"数据源：{self.name}（{self.type}）"]
        if connection:
            lines.append(f"连接参数：{connection}")

        if not self.tables:
            lines.append("表结构：**尚未执行元数据探查，可用表未知**")
            return "\n".join(lines)

        # 上下文截断只控制模型输入规模；完整 Schema 仍保留在 tables 中供确定性门禁使用。
        lines.append(f"可用表（共 {len(self.tables)} 张，最多展示 {max_tables} 张）：")
        for table in self.tables[:max_tables]:
            lines.append(f"- {table.get('name')}（整表分级 {table.get('max_level', 'L2')}）")
            columns = table.get("columns") or []
            for column in columns[:max_columns]:
                tag = ""
                if column.get("level") in ("L3", "L4"):
                    tag = f"【{column['level']} {column.get('category', '')}】"
                lines.append(
                    f"    字段：{column.get('name')} {column.get('type', '')}{tag}"
                )
            if len(columns) > max_columns:
                lines.append(f"    …还有 {len(columns) - max_columns} 个字段")
        return "\n".join(lines)

    def find_table(self, table_name: str) -> dict[str, Any] | None:
        for table in self.tables:
            if str(table.get("name")) == table_name:
                return table
        return None

    def known_fields(self, table_name: str) -> list[str]:
        table = self.find_table(table_name)
        if table is None:
            return []
        return [str(column.get("name", "")) for column in (table.get("columns") or [])]
