"""
路径工具模块
统一处理 Python 开发模式与 PyInstaller 打包模式下的文件路径差异。

- 开发模式：基于 __file__ 解析相对路径
- 打包模式：基于 sys._MEIPASS（临时解压目录）读取资源文件，
            基于 exe 所在目录写入文件（Cookie / 历史 / 下载）
"""

import os
import sys


def get_base_dir() -> str:
    """获取程序的基础目录（可写文件的存放位置）。

    PyInstaller 单文件模式下，返回 exe 所在目录；
    开发模式下，返回项目根目录（main.py 所在目录）。
    """
    if getattr(sys, "frozen", False):
        # 打包后的路径：exe 所在目录
        return os.path.dirname(sys.executable)
    else:
        # 开发模式：main.py 所在目录
        return os.path.dirname(os.path.abspath(__file__))


def get_resource_path(relative_path: str) -> str:
    """获取资源文件（只读、跟着 exe 一起打包的文件）的绝对路径。

    优先从 sys._MEIPASS 查找（打包后 PyInstaller 的解压临时目录）；
    开发模式下回退到项目根目录。

    Args:
        relative_path: 相对于项目根目录的资源路径，如 "assets/icon.png"

    Returns:
        资源的绝对路径。
    """
    base = getattr(sys, "_MEIPASS", get_base_dir())
    return os.path.join(base, relative_path)


def get_data_path(filename: str) -> str:
    """获取数据文件（可读写，如 Cookie、历史记录）的绝对路径。

    始终保存在 exe/脚本 所在目录下，与 PyInstaller 临时解压目录无关。
    """
    return os.path.join(get_base_dir(), filename)
