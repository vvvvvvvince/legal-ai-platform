from __future__ import annotations

import getpass
import os

from app.services.auth_store import AuthStore


def main() -> None:
    store = AuthStore(os.getenv("AUTH_DB", "data/auth.sqlite3"))
    print("此操作会停用现有账号并撤销其登录状态，然后创建恰好 3 个新账号。")
    users: list[tuple[str, str, str | None, str, bool]] = []
    for index in range(1, 4):
        print(f"\n账号 {index}")
        username = input("用户名：").strip()
        display_name = input("显示名：").strip()
        phone = input("手机号：").strip()
        password = getpass.getpass("初始密码（至少 8 位）：")
        if password != getpass.getpass("再次输入密码："):
            raise SystemExit("两次密码不一致，未修改任何账号。")
        users.append((username, display_name, phone, password, False))
    if input("输入 REPLACE 确认替换：").strip() != "REPLACE":
        raise SystemExit("已取消，未修改任何账号。")
    created = store.replace_active_users(users)
    print("已创建：" + "、".join(user.username for user in created))


if __name__ == "__main__":
    main()
