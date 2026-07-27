"""
FFmpeg 工具模块
负责 FFmpeg 的检测、音视频合并、音频提取等功能。

检测优先级：
    1. 程序所在目录（兼容 PyInstaller 打包后的便携部署）
    2. 系统 PATH 环境变量
"""

import subprocess
import shutil
import os
import sys

from path_utils import get_base_dir


def _find_ffmpeg_in_local_dir() -> str:
    """在程序所在目录查找 ffmpeg.exe（Windows）或 ffmpeg。

    Returns:
        str: 找到的完整路径，未找到返回空字符串。
    """
    base = get_base_dir()
    names = ["ffmpeg.exe", "ffmpeg"]
    for name in names:
        candidate = os.path.join(base, name)
        if os.path.isfile(candidate):
            return candidate
    return ""


def check_ffmpeg() -> bool:
    """检测 FFmpeg 是否可用。

    检测顺序：程序目录 → 系统 PATH。

    Returns:
        bool: 可用返回 True。
    """
    local = _find_ffmpeg_in_local_dir()
    if local:
        return True
    return shutil.which("ffmpeg") is not None


def get_ffmpeg_exe() -> str:
    """获取 FFmpeg 可执行文件路径。

    Returns:
        str: 路径，未找到返回空字符串。
    """
    local = _find_ffmpeg_in_local_dir()
    if local:
        return local
    return shutil.which("ffmpeg") or ""


def get_ffmpeg_path() -> str:
    """获取 FFmpeg 的可执行路径（兼容旧 API）。

    Returns:
        str: FFmpeg 路径，未安装返回空字符串。
    """
    return get_ffmpeg_exe()


def get_ffmpeg_error_message() -> str:
    """获取 FFmpeg 未找到时的友好错误提示。

    Returns:
        str: 错误提示文本。
    """
    return (
        "未找到 FFmpeg 可执行文件。\n"
        "请将 ffmpeg.exe 放置在与本程序相同的文件夹下，\n"
        "或安装 FFmpeg 并配置系统环境变量后重试。"
    )


def merge_audio_video(
    video_path: str,
    audio_path: str,
    output_path: str,
    progress_callback=None,
) -> bool:
    """使用 FFmpeg 合并视频和音频轨道。

    Args:
        video_path: 视频文件路径（无音频轨）。
        audio_path: 音频文件路径。
        output_path: 合并后的输出文件路径。
        progress_callback: 可选，进度回调函数 callback(percent: float)。

    Returns:
        bool: 成功返回 True，失败返回 False。
    """
    if not check_ffmpeg():
        raise FileNotFoundError(get_ffmpeg_error_message())

    ffmpeg_exe = get_ffmpeg_exe()

    # 获取音频时长用于进度估算
    total_duration = _get_duration(audio_path, ffmpeg_exe)

    cmd = [
        ffmpeg_exe,
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-strict", "experimental",
        "-y",
        output_path,
    ]

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )

        for line in process.stderr:
            if progress_callback and total_duration > 0:
                percent = _parse_ffmpeg_progress(line, total_duration)
                if percent is not None:
                    progress_callback(min(percent, 100.0))

        process.wait()

        if process.returncode == 0 and os.path.exists(output_path):
            return True
        else:
            return False

    except Exception as e:
        raise RuntimeError(f"FFmpeg 合并失败: {e}")


def extract_audio_to_mp3(
    input_path: str,
    output_path: str,
    progress_callback=None,
) -> bool:
    """将音视频文件中的音频提取并转换为 MP3 格式。

    Args:
        input_path: 输入文件路径。
        output_path: 输出 MP3 文件路径。
        progress_callback: 可选，进度回调函数 callback(percent: float)。

    Returns:
        bool: 成功返回 True，失败返回 False。
    """
    if not check_ffmpeg():
        raise FileNotFoundError(get_ffmpeg_error_message())

    ffmpeg_exe = get_ffmpeg_exe()
    total_duration = _get_duration(input_path, ffmpeg_exe)

    cmd = [
        ffmpeg_exe,
        "-i", input_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-q:a", "2",
        "-y",
        output_path,
    ]

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
            errors="replace",
        )

        for line in process.stderr:
            if progress_callback and total_duration > 0:
                percent = _parse_ffmpeg_progress(line, total_duration)
                if percent is not None:
                    progress_callback(min(percent, 100.0))

        process.wait()

        if process.returncode == 0 and os.path.exists(output_path):
            return True
        else:
            return False

    except Exception as e:
        raise RuntimeError(f"音频提取失败: {e}")


def _get_duration(file_path: str, ffmpeg_exe: str = "ffmpeg") -> float:
    """获取媒体文件的时长（秒）。

    Args:
        file_path: 媒体文件路径。
        ffmpeg_exe: ffmpeg 可执行文件路径（用于 ffprobe）。

    Returns:
        float: 时长（秒），获取失败返回 0。
    """
    if not os.path.exists(file_path):
        return 0.0

    # ffprobe 通常与 ffmpeg 在同一目录
    if ffmpeg_exe and ffmpeg_exe != "ffmpeg":
        probe_dir = os.path.dirname(ffmpeg_exe)
        probe_exe = os.path.join(probe_dir, "ffprobe.exe")
        if not os.path.exists(probe_exe):
            probe_exe = shutil.which("ffprobe") or "ffprobe"
    else:
        probe_exe = shutil.which("ffprobe") or "ffprobe"

    cmd = [
        probe_exe,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        file_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _parse_ffmpeg_progress(line: str, total_duration: float) -> float | None:
    """从 FFmpeg 的 stderr 输出中解析当前进度百分比。

    Args:
        line: FFmpeg 输出的单行内容。
        total_duration: 媒体总时长（秒）。

    Returns:
        float | None: 进度百分比，无法解析时返回 None。
    """
    if "time=" not in line:
        return None

    try:
        time_str = line.split("time=")[1].split()[0].strip()
        # time 格式可能是 HH:MM:SS.MS
        parts = time_str.replace(",", ".").split(":")
        if len(parts) == 3:
            hours, minutes, seconds = parts
            current_time = float(hours) * 3600 + float(minutes) * 60 + float(seconds)
        else:
            return None

        if total_duration > 0:
            return (current_time / total_duration) * 100
        return None
    except (ValueError, IndexError):
        return None
