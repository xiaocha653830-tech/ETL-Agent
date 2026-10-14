"""演示数据种子脚本（开发 / 演示环境）。

用法（在 ``api/`` 目录下执行）：

    python -m app.scripts.seed

**幂等**：已存在的项目、用户、成员关系会被跳过，可重复执行。
种子覆盖 PRD 第 2 章的五角色，便于演示职责分离（FR-24）与双维审批（FR-23）。
"""

import logging

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.identity import Membership, Project, User, UserRole

logger = logging.getLogger(__name__)

DEMO_PASSWORD = "Demo@12345"
DEMO_PROJECT_NAME = "演示项目"
DEMO_TENANT = "default"

# 五角色各一人 —— 与 PRD 第 2 章权限矩阵一一对应。
# 邮箱用 example.com：RFC 2606 保留域名，且能通过接口层的 EmailStr 校验
# （.local 这类特殊 TLD 会被 email-validator 拒绝，导致种子账号无法登录）。
DEMO_USERS: list[tuple[str, str, UserRole]] = [
    ("maker@example.com", "王五", UserRole.MAKER),
    ("checker.data@example.com", "李数据", UserRole.CHECKER_DATA),
    ("checker.security@example.com", "赵安全", UserRole.CHECKER_SECURITY),
    ("operator@example.com", "孙操作", UserRole.OPERATOR),
    ("auditor@example.com", "周审计", UserRole.AUDITOR),
]


def seed() -> None:
    """写入演示数据。"""
    db = SessionLocal()
    try:
        project = db.scalar(
            select(Project).where(
                Project.name == DEMO_PROJECT_NAME, Project.tenant_id == DEMO_TENANT
            )
        )
        if project is None:
            project = Project(name=DEMO_PROJECT_NAME, tenant_id=DEMO_TENANT)
            db.add(project)
            db.flush()
            logger.info("创建项目：%s", project.name)

        created_users = 0
        created_members = 0
        for email, display_name, role in DEMO_USERS:
            user = db.scalar(select(User).where(User.email == email))
            if user is None:
                user = User(
                    email=email,
                    hashed_password=hash_password(DEMO_PASSWORD),
                    display_name=display_name,
                )
                db.add(user)
                db.flush()
                created_users += 1

            existed = db.scalar(
                select(Membership).where(
                    Membership.user_id == user.id,
                    Membership.project_id == project.id,
                )
            )
            if existed is None:
                db.add(Membership(user_id=user.id, project_id=project.id, role=role))
                created_members += 1

        db.commit()

        print(f"项目：{project.name}（tenant={project.tenant_id}，id={project.id}）")
        print(f"新增用户 {created_users} 个、成员关系 {created_members} 条")
        print(f"统一口令：{DEMO_PASSWORD}")
        print("-" * 56)
        for email, display_name, role in DEMO_USERS:
            print(f"  {role.value:<16} {email:<32} {display_name}")
        print("-" * 56)
    finally:
        db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    seed()
