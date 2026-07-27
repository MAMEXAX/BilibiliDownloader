"""
B站扫码登录与 Cookie 管理模块
基于 bilibili-api-python 的 login_v2 实现二维码登录、凭证持久化和自动加载。
"""

import asyncio
import io
import json
import os
import threading
import time
import traceback
from typing import Any

from bilibili_api.login_v2 import (
    Credential,
    QrCodeLogin,
    QrCodeLoginChannel,
    QrCodeLoginEvents,
)
from bilibili_api import user
from PIL import Image

from path_utils import get_data_path


def _run_async(coro):
    """在同步上下文中运行 asyncio 协程。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        result_holder: dict[str, Any] = {}

        def _runner():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                result_holder["result"] = new_loop.run_until_complete(coro)
            except Exception as e:
                result_holder["error"] = e
            finally:
                new_loop.close()

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
        if "error" in result_holder:
            raise result_holder["error"]
        return result_holder.get("result")
    else:
        return asyncio.run(coro)


class LoginManager:
    """B站登录管理器，负责二维码登录、Cookie 持久化和凭证管理。"""

    # 登录超时（秒）
    QR_TIMEOUT = 180
    # 轮询间隔（秒）
    POLL_INTERVAL = 2.0

    def __init__(self, cookie_file: str | None = None):
        """初始化登录管理器。

        Args:
            cookie_file: Cookie 持久化文件路径。默认为程序目录下的 cookie.json。
        """
        self._cookie_file = cookie_file or get_data_path("cookie.json")
        self._credential: Credential | None = None
        self._qr_login: QrCodeLogin | None = None
        self._logged_in = False
        self._nickname: str = ""

    # ══════════════════════════════════════════════════════════════════════
    #  公开属性
    # ══════════════════════════════════════════════════════════════════════

    @property
    def is_logged_in(self) -> bool:
        """是否已登录。"""
        return self._logged_in

    @property
    def nickname(self) -> str:
        """已登录用户的昵称。"""
        return self._nickname

    def get_credential(self) -> Credential | None:
        """获取当前有效的登录凭证。

        Returns:
            Credential | None: 登录凭证，未登录返回 None。
        """
        return self._credential if self._logged_in else None

    # ══════════════════════════════════════════════════════════════════════
    #  Cookie 持久化
    # ══════════════════════════════════════════════════════════════════════

    def load_cookies(self) -> bool:
        """从本地文件加载 Cookie 并验证有效性。

        Returns:
            bool: 加载且验证成功返回 True。
        """
        if not os.path.exists(self._cookie_file):
            return False

        try:
            with open(self._cookie_file, "r", encoding="utf-8") as f:
                cookies = json.load(f)

            if not isinstance(cookies, dict) or not cookies.get("SESSDATA"):
                return False

            credential = Credential.from_cookies(cookies)

            # 验证凭证是否有效（尝试获取用户信息）
            try:
                u = user.User(credential=credential)
                info = _run_async(u.get_self_info())
                self._credential = credential
                self._logged_in = True
                self._nickname = info.get("name", "未知用户")
                return True
            except Exception:
                # 凭证过期，尝试刷新
                try:
                    if credential.check_refresh():
                        self._save_cookies(credential)
                        u = user.User(credential=credential)
                        info = _run_async(u.get_self_info())
                        self._credential = credential
                        self._logged_in = True
                        self._nickname = info.get("name", "未知用户")
                        return True
                except Exception:
                    pass
                return False

        except (json.JSONDecodeError, IOError, Exception):
            return False

    def _save_cookies(self, credential: Credential) -> None:
        """将凭证保存到本地文件。

        Args:
            credential: 登录凭证。
        """
        try:
            cookies = credential.get_cookies()
            with open(self._cookie_file, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
        except IOError as e:
            print(f"保存 Cookie 失败: {e}")

    def clear_cookies(self) -> None:
        """清除登录状态和本地 Cookie 文件。"""
        self._credential = None
        self._logged_in = False
        self._nickname = ""
        if os.path.exists(self._cookie_file):
            try:
                os.remove(self._cookie_file)
            except OSError:
                pass

    # ══════════════════════════════════════════════════════════════════════
    #  二维码登录流程
    # ══════════════════════════════════════════════════════════════════════

    def start_login(self) -> bool:
        """开始登录流程：生成二维码。

        Returns:
            bool: 生成成功返回 True，失败返回 False。
        """
        try:
            self._qr_login = QrCodeLogin(platform=QrCodeLoginChannel.WEB)
            _run_async(self._qr_login.generate_qrcode())
            return True
        except Exception as e:
            traceback.print_exc()
            self._qr_login = None
            return False

    def get_qr_image(self):
        """获取二维码图片（PIL Image 对象）。

        Returns:
            PIL.Image.Image | None: 二维码图片，失败返回 None。
        """
        if self._qr_login is None:
            return None
        try:
            qr_picture = self._qr_login.get_qrcode_picture()
            # Picture.content 是 bytes，用 PIL 解码
            return Image.open(io.BytesIO(qr_picture.content))
        except Exception:
            return None

    def check_login_state(self) -> str:
        """轮询检查登录状态。

        Returns:
            str: 状态字符串，可选值：
                'waiting'  - 等待扫码
                'scanned'  - 已扫码，等待确认
                'confirmed' - 已确认登录
                'done'     - 登录完成（凭证已可用）
                'timeout'  - 二维码过期
                'error'    - 检查失败
        """
        if self._qr_login is None:
            return "error"

        try:
            event = _run_async(self._qr_login.check_state())
            event_map = {
                QrCodeLoginEvents.SCAN: "scanned",
                QrCodeLoginEvents.CONF: "confirmed",
                QrCodeLoginEvents.TIMEOUT: "timeout",
                QrCodeLoginEvents.DONE: "done",
            }
            return event_map.get(event, "waiting")
        except Exception:
            return "error"

    def finish_login(self) -> bool:
        """登录完成后获取并保存凭证。

        Returns:
            bool: 成功返回 True。
        """
        if self._qr_login is None:
            return False

        try:
            credential = self._qr_login.get_credential()
            if credential is None or not credential.has_sessdata():
                return False

            # 获取用户信息
            try:
                u = user.User(credential=credential)
                info = _run_async(u.get_self_info())
                self._nickname = info.get("name", "未知用户")
            except Exception:
                self._nickname = "未知用户"

            self._credential = credential
            self._logged_in = True
            self._save_cookies(credential)
            self._qr_login = None
            return True

        except Exception as e:
            traceback.print_exc()
            return False
