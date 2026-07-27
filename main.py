"""
BiliDownloader - B站视频下载工具
基于 Python 的跨平台 B 站视频下载桌面应用。

用法：
    python main.py

依赖安装：
    pip install -r requirements.txt

额外依赖：
    FFmpeg（需手动安装并添加到系统 PATH）
"""
import os
import sys
import certifi

# ── SSL 证书路径兼容（解决打包后 HTTPS 请求报"网络错误"）──
# certifi.where() 在 PyInstaller 打包后可能因为 importlib.resources 机制
# 找不到证书文件；此时退而使用 sys._MEIPASS 下打包进去的 certifi/cacert.pem
def _get_certifi_path() -> str:
    """获取 certifi 证书文件路径，兼容开发与打包环境。"""
    try:
        return certifi.where()
    except Exception:
        # 打包后 fallback：从 PyInstaller 的临时解压目录读取
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = os.path.join(meipass, "certifi", "cacert.pem")
            if os.path.isfile(bundled):
                return bundled
        raise RuntimeError("无法找到 SSL 证书文件 (cacert.pem)，请检查打包配置。")

_cert_path = _get_certifi_path()
os.environ['SSL_CERT_FILE'] = _cert_path
os.environ['REQUESTS_CA_BUNDLE'] = _cert_path

from path_utils import get_base_dir

# 确保工作目录为程序所在目录（开发模式用项目根目录，打包后为 exe 所在目录）
os.chdir(get_base_dir())

# ── 注册 bilibili_api 的 HTTP 客户端 ──
# bilibili_api 需要 curl_cffi / aiohttp / httpx 之一作为底层网络客户端。
# 正常情况 import bilibili_api 时自动注册，此处显式注册作为双保险。
import bilibili_api
from bilibili_api import select_client
try:
    select_client("aiohttp")
except Exception:
    # aiohttp 未安装或注册失败，尝试 httpx
    try:
        select_client("httpx")
    except Exception:
        # 仍失败则在后续真正发起请求时才会报错，给出明确提示
        pass

from gui import MainWindow


def main():
    """程序入口。"""
    try:
        app = MainWindow()
        app.run()
    except Exception as e:
        print(f"程序启动失败: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
