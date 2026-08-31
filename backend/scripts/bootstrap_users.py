from __future__ import annotations

import getpass
import os

from app.services.auth_store import AuthStore


def main() -> None:
    store = AuthStore(os.getenv("AUTH_DB", "data/auth.sqlite3"))
    print("创建共享工作区用户（密码至少 8 位，不会显示在屏幕上）")
    for index in (1, 2):
        username = input(f"用户 {index} 登录名：").strip()
        display_name = input(f"用户 {index} 显示名：").strip()
        password = getpass.getpass(f"用户 {index} 密码：")
        confirm = getpass.getpass("再次输入密码：")
        if password != confirm:
            raise SystemExit("两次密码不一致。")
        store.create_user(username, display_name, password)
        print(f"已创建 {username}")


if __name__ == "__main__":
    main()
