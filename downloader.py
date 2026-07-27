"""
B站视频下载核心模块
负责视频信息解析、流媒体下载、音视频合并等功能。
"""

import asyncio
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

import requests
from bilibili_api import video as bvideo, Credential

from ffmpeg_utils import check_ffmpeg, merge_audio_video, extract_audio_to_mp3


# ── 清晰度 ID → 描述映射 ──────────────────────────────────────────────
QUALITY_MAP: dict[int, str] = {
    16: "360P 流畅",
    32: "480P 清晰",
    64: "720P 高清",
    74: "720P60 高清",
    80: "1080P 高清",
    112: "1080P+ 高码率",
    116: "1080P60 高清",
    120: "4K 超清",
}

# 仅音频的 ID 集合（清晰度越低越排在前面可同时做兜底）
AUDIO_QUALITY_IDS = [30280, 30232, 30216]


class DownloadCancelledError(Exception):
    """下载被用户取消时抛出的异常。"""
    pass


class _PauseAndResume(Exception):
    """内部异常：暂停后恢复，用于跳出当前 iter_content 循环并重新发起 Range 请求。"""
    pass


@dataclass
class VideoInfo:
    """视频元信息数据类。"""

    bvid: str = ""
    aid: str = ""
    title: str = ""
    cover_url: str = ""
    uploader: str = ""
    duration: str = ""
    description: str = ""


@dataclass
class StreamInfo:
    """视频/音频流数据类。"""

    quality_id: int = 0
    quality_desc: str = ""
    bandwidth: int = 0
    mime_type: str = ""
    codecs: str = ""
    url: str = ""


@dataclass
class ParsedVideoData:
    """解析后的完整视频数据。"""

    info: VideoInfo = field(default_factory=VideoInfo)
    video_streams: list[StreamInfo] = field(default_factory=list)
    audio_streams: list[StreamInfo] = field(default_factory=list)


def _run_async(coro):
    """在同步代码中运行 asyncio 协程。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        # 已有运行中的事件循环 → 新建线程执行
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


def _extract_bv_or_av(url: str) -> tuple[str | None, str | None]:
    """从用户输入的链接中提取 BV 号和 AV 号。

    Args:
        url: 用户输入的URL或BV/AV号。

    Returns:
        tuple: (bvid, aid)，至少一个不为 None。
    """
    # 匹配纯 BV 号
    bv_match = re.search(r"BV[0-9A-Za-z]{10}", url)
    bvid = bv_match.group(0) if bv_match else None

    # 匹配纯 AV 号
    av_match = re.search(r"av(\d+)", url, re.IGNORECASE)
    aid = int(av_match.group(1)) if av_match else None

    if not bvid and not aid:
        # 尝试仅数字输入 → 视为 AV 号
        num_match = re.search(r"(\d+)", url)
        if num_match:
            aid = int(num_match.group(1))

    return bvid, str(aid) if aid else None


def _extract_cid_from_info(info: dict) -> int:
    """从 get_info() 返回的视频信息中提取 CID。

    info["pages"] 是分P列表，每个分P包含 {"cid": int, "page": int, "part": str}。
    默认取第1P（page_index=0）的 cid。

    Args:
        info: vid.get_info() 的返回值。

    Returns:
        int: CID，提取失败返回 0。
    """
    pages = info.get("pages")
    if not pages or not isinstance(pages, list) or len(pages) == 0:
        return 0
    first_page = pages[0]
    if isinstance(first_page, dict):
        return first_page.get("cid", 0)
    return 0


def _normalize_cid(raw) -> int:
    """将 get_cid() 的不同返回格式统一转为 int。

    bilibili-api 不同版本可能返回：
      - 整数 int
      - 字典 {"cid": 123, "page": 1}
      - 列表 [123] 或 [{"cid": 123}]

    Args:
        raw: get_cid() 的原始返回值。

    Returns:
        int: 标准化后的 CID，无法解析返回 0。
    """
    if isinstance(raw, int):
        return raw
    if isinstance(raw, dict):
        return raw.get("cid", 0)
    if isinstance(raw, list) and len(raw) > 0:
        item = raw[0]
        if isinstance(item, int):
            return item
        if isinstance(item, dict):
            return item.get("cid", 0)
    return 0


def parse_video_info(url: str, credential: Credential | None = None) -> ParsedVideoData:
    """解析视频信息，获取标题、封面、UP主、所有清晰度及流地址。

    Args:
        url: B站视频链接或BV/AV号。
        credential: 可选的登录凭证，登录后可获取更高清晰度。

    Returns:
        ParsedVideoData: 解析后的视频数据。

    Raises:
        ValueError: URL无效或视频不存在。
        RuntimeError: API调用失败。
    """
    bvid, aid = _extract_bv_or_av(url)
    if not bvid and not aid:
        raise ValueError(f"无法从输入中提取有效的 BV 或 AV 号: {url}")

    try:
        # 使用传入的 credential，否则用空凭证（只能获取低清流）
        if credential is None:
            credential = Credential()
        vid = bvideo.Video(
            bvid=bvid,
            aid=int(aid) if aid else None,
            credential=credential,
        )

        # ── 获取基本信息 ──
        info = _run_async(vid.get_info())

        video_info = VideoInfo(
            bvid=info.get("bvid", bvid or ""),
            aid=str(info.get("aid", aid or "")),
            title=info.get("title", "未知标题"),
            cover_url=info.get("pic", ""),
            uploader=info.get("owner", {}).get("name", "未知UP主"),
            duration=_format_duration(info.get("duration", 0)),
            description=info.get("desc", ""),
        )

        # ── 提取 CID（优先从 info.pages 获取，无需额外 API 调用） ──
        cid = _extract_cid_from_info(info)
        if not cid:
            # 兜底：调用 get_cid 方法
            try:
                cid_raw = _run_async(vid.get_cid(page_index=0))
                cid = _normalize_cid(cid_raw)
            except Exception:
                cid = 0

        if not cid:
            raise RuntimeError(
                f"无法获取视频 CID，视频可能不存在或已被删除。"
                f"\nBVID: {bvid}, AID: {aid}"
            )

        # ── 获取下载流信息 ──
        download_url_data = _run_async(vid.get_download_url(page_index=0))
        dash = download_url_data.get("dash", {})

        # ── 提取视频流 ──
        video_streams: list[StreamInfo] = []
        raw_video_streams = dash.get("video", [])
        for vs in raw_video_streams:
            qid = vs["id"]
            video_streams.append(StreamInfo(
                quality_id=qid,
                quality_desc=QUALITY_MAP.get(qid, f"未知清晰度({qid})"),
                bandwidth=vs.get("bandwidth", 0),
                mime_type=vs.get("mimeType", ""),
                codecs=vs.get("codecs", ""),
                url=vs.get("baseUrl", vs.get("base_url", "")),
            ))

        # 去重（按清晰度ID），保留最后一个
        seen: set[int] = set()
        deduped: list[StreamInfo] = []
        for s in video_streams:
            if s.quality_id not in seen:
                seen.add(s.quality_id)
                deduped.append(s)
        # 按清晰度升序排列
        deduped.sort(key=lambda x: x.quality_id)
        video_streams = deduped

        # ── 提取音频流 ──
        audio_streams: list[StreamInfo] = []
        raw_audio_streams = dash.get("audio", [])
        for as_ in raw_audio_streams:
            qid = as_["id"]
            audio_streams.append(StreamInfo(
                quality_id=qid,
                quality_desc=f"音频 {as_.get('bandwidth', 0) // 1000}kbps",
                bandwidth=as_.get("bandwidth", 0),
                mime_type=as_.get("mimeType", ""),
                codecs=as_.get("codecs", ""),
                url=as_.get("baseUrl", as_.get("base_url", "")),
            ))

        return ParsedVideoData(
            info=video_info,
            video_streams=video_streams,
            audio_streams=audio_streams,
        )

    except Exception as e:
        raise RuntimeError(f"视频解析失败: {e}") from e


def _format_duration(seconds: int) -> str:
    """将秒数转换为 HH:MM:SS 格式。"""
    if seconds <= 0:
        return "未知"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _download_file(
    url: str,
    save_path: str,
    progress_callback: Callable[[int, int, float, float], None] | None = None,
    headers: dict | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> None:
    """单线程下载文件，支持进度回调和取消检测。

    Args:
        url: 文件下载地址。
        save_path: 保存路径。
        progress_callback: 进度回调 callback(downloaded, total, speed, elapsed)。
        headers: 自定义请求头。
        cancel_check: 取消检测回调，返回 True 表示应取消下载。

    Raises:
        DownloadCancelledError: 用户取消下载。
        requests.RequestException: 网络请求失败。
    """
    default_headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://www.bilibili.com",
    }
    if headers:
        default_headers.update(headers)

    response = requests.get(url, headers=default_headers, stream=True, timeout=60)
    response.raise_for_status()

    total_size = int(response.headers.get("content-length", 0))
    downloaded = 0
    start_time = time.time()
    chunk_size = 1024 * 1024  # 1MB

    try:
        with open(save_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=chunk_size):
                # 检查取消标志
                if cancel_check and cancel_check():
                    raise DownloadCancelledError("下载已被用户取消")
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    elapsed = time.time() - start_time
                    speed = downloaded / elapsed if elapsed > 0 else 0
                    if progress_callback:
                        progress_callback(downloaded, total_size, speed, elapsed)
    except DownloadCancelledError:
        raise
    except Exception as e:
        if cancel_check and cancel_check():
            raise DownloadCancelledError("下载已被用户取消") from e
        raise
    finally:
        response.close()


class DownloadTask:
    """视频下载任务，在后台线程中完成下载和合并。"""

    def __init__(
        self,
        parsed_data: ParsedVideoData,
        selected_quality: int,
        output_dir: str = "./downloads",
        audio_only: bool = False,
    ):
        """初始化下载任务。

        Args:
            parsed_data: 解析后的视频数据。
            selected_quality: 用户选择的清晰度ID。
            output_dir: 输出目录。
            audio_only: 是否仅下载音频。
        """
        self.parsed_data = parsed_data
        self.selected_quality = selected_quality
        self.output_dir = output_dir
        self.audio_only = audio_only

        # 状态回调
        self._status_callback: Callable[[str], None] | None = None
        self._progress_callback: Callable[[float, str, str], None] | None = None
        # 完成回调 (success: bool, message: str, file_path: str)
        self._done_callback: Callable[[bool, str, str], None] | None = None

        self._thread: threading.Thread | None = None
        self._cancelled = False
        self._active_response: requests.Response | None = None  # 用于 cancel() 时强制关闭连接

        # ── 暂停/恢复相关 ──
        self._paused = False
        self._pause_event = threading.Event()
        self._pause_event.set()  # 初始为非暂停状态（set = 可继续运行）
        self._resume_offset: int = 0  # 断点续传偏移量（当前段已下载字节数）

    def set_status_callback(self, cb: Callable[[str], None]) -> None:
        self._status_callback = cb

    def set_progress_callback(self, cb: Callable[[float, str, str], None]) -> None:
        self._progress_callback = cb

    def set_done_callback(self, cb: Callable[[bool, str, str], None]) -> None:
        self._done_callback = cb

    def _report_status(self, msg: str) -> None:
        if self._status_callback:
            self._status_callback(msg)

    def _report_progress(self, percent: float, speed: str, remaining: str) -> None:
        if self._progress_callback:
            self._progress_callback(percent, speed, remaining)

    def _report_done(self, success: bool, msg: str, file_path: str = "") -> None:
        if self._done_callback:
            self._done_callback(success, msg, file_path)

    def start(self) -> None:
        """在后台线程中启动下载任务。"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """取消下载任务，立即终止网络请求并清理临时文件。"""
        self._cancelled = True
        # 如果正在暂停等待，先恢复再取消，避免线程永久阻塞
        self._pause_event.set()
        # 强制关闭当前活跃的 HTTP 连接，使 iter_content() 立即抛出异常
        if self._active_response is not None:
            try:
                self._active_response.close()
            except Exception:
                pass
            finally:
                self._active_response = None

    def pause(self) -> None:
        """暂停下载任务。清除 Event 使下载线程在 pause_event.wait() 处阻塞。"""
        self._paused = True
        self._pause_event.clear()

    def resume(self) -> None:
        """恢复下载任务。设置 Event 唤醒阻塞中的下载线程。

        如果之前因暂停而关闭了连接，会通过 _resume_offset 发送 Range 请求续传。
        """
        self._paused = False
        self._pause_event.set()

    def is_paused(self) -> bool:
        """查询是否处于暂停状态。"""
        return self._paused

    def _run(self) -> None:
        """下载任务主逻辑（在后台线程中执行）。"""
        video_file = ""
        audio_file = ""
        try:
            data = self.parsed_data

            # 确保输出目录存在
            os.makedirs(self.output_dir, exist_ok=True)

            # 清理文件名中的非法字符
            safe_title = re.sub(r'[\\/:*?"<>|]', "_", data.info.title)
            base_filename = f"{safe_title}_{data.info.bvid}"

            # ── 定位选中的视频流 ──
            selected_video = None
            for vs in data.video_streams:
                if vs.quality_id == self.selected_quality:
                    selected_video = vs
                    break

            if not selected_video and not self.audio_only:
                if data.video_streams:
                    selected_video = data.video_streams[-1]
                    self._report_status(f"未找到所选清晰度，自动选择 {selected_video.quality_desc}")
                else:
                    raise RuntimeError("没有可用的视频流")

            # ── 定位音频流 ──
            selected_audio = None
            if data.audio_streams:
                selected_audio = data.audio_streams[-1]

            if self.audio_only:
                # ── 仅下载音频 ──
                if not selected_audio:
                    raise RuntimeError("没有可用的音频流")

                audio_file = os.path.join(self.output_dir, f"{base_filename}_audio.m4s")
                mp3_file = os.path.join(self.output_dir, f"{base_filename}.mp3")

                self._report_status("正在下载音频...")
                self._do_download(selected_audio.url, audio_file, "音频")

                self._report_status("正在转换为 MP3...")
                if check_ffmpeg():
                    extract_audio_to_mp3(
                        audio_file, mp3_file,
                        progress_callback=lambda p: self._report_progress(p, "", "")
                    )
                    self._cleanup(audio_file)
                    self._report_done(True, f"下载完成！\n保存至: {mp3_file}", mp3_file)
                else:
                    self._report_done(True, f"下载完成（需 FFmpeg 转为 MP3）\n保存至: {audio_file}", audio_file)

            elif selected_video and selected_audio:
                # ── 下载视频和音频，然后合并 ──
                video_file = os.path.join(self.output_dir, f"{base_filename}_video.m4s")
                audio_file = os.path.join(self.output_dir, f"{base_filename}_audio.m4s")
                output_file = os.path.join(self.output_dir, f"{base_filename}.mp4")

                self._report_status("正在下载视频流...")
                self._do_download(selected_video.url, video_file, "视频")

                self._report_status("正在下载音频流...")
                self._do_download(selected_audio.url, audio_file, "音频")

                if check_ffmpeg():
                    self._report_status("正在合并音视频...")
                    success = merge_audio_video(
                        video_file, audio_file, output_file,
                        progress_callback=lambda p: self._report_progress(p, "", "")
                    )
                    self._cleanup(video_file, audio_file)
                    if success:
                        self._report_done(True, f"下载完成！\n保存至: {output_file}", output_file)
                    else:
                        self._report_done(False, "音视频合并失败", "")
                else:
                    self._report_done(True, f"下载完成（请手动合并）\n视频: {video_file}\n音频: {audio_file}", video_file)

            elif selected_video and not selected_audio:
                ext = ".mp4" if "mp4" in selected_video.mime_type else ".flv"
                output_file = os.path.join(self.output_dir, f"{base_filename}{ext}")

                self._report_status("正在下载视频...")
                self._do_download(selected_video.url, output_file, "视频")
                self._report_done(True, f"下载完成！\n保存至: {output_file}", output_file)

            else:
                raise RuntimeError("无可用的下载资源")

        except DownloadCancelledError:
            self._cleanup(video_file, audio_file)
            self._report_done(False, "下载已取消", "")

        except Exception as e:
            traceback.print_exc()
            self._cleanup(video_file, audio_file)
            self._report_done(False, f"下载失败: {e}", "")

    def _do_download(self, url: str, save_path: str, label: str) -> None:
        """执行单个文件下载，支持暂停/恢复和断点续传（HTTP Range）。

        - 暂停时：关闭当前连接，记录已下载字节数到 self._resume_offset。
        - 恢复时：通过 _resume_offset 发送 Range 请求继续下载。
        - 线程安全：通过 threading.Event 和 _cancelled 标志协调。
        """
        last_update_time = time.time()
        last_downloaded = self._resume_offset  # 恢复下载时沿用之前的偏移量

        def progress_cb(downloaded: int, total: int, speed: float, elapsed: float):
            nonlocal last_update_time, last_downloaded

            now = time.time()
            if now - last_update_time < 0.3:
                return
            last_update_time = now
            last_downloaded = downloaded

            if total > 0:
                percent = (downloaded / total) * 100
                remaining_bytes = total - downloaded
                remaining_time = remaining_bytes / speed if speed > 0 else 0
                speed_str = _format_speed(speed)
                remaining_str = _format_time(remaining_time)
            else:
                percent = 0
                speed_str = _format_speed(speed)
                remaining_str = "计算中"

            self._report_progress(percent, speed_str, remaining_str)

        # ── 下载循环：支持暂停后通过 Range 续传 ──
        resumed = self._resume_offset > 0  # 是否是恢复下载
        start_time = time.time()

        while True:
            # 构建请求头
            headers = {
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"),
                "Referer": "https://www.bilibili.com",
            }

            if self._resume_offset > 0:
                headers["Range"] = f"bytes={self._resume_offset}-"

            response = requests.get(url, headers=headers, stream=True, timeout=60)

            # 处理 Range 响应 (206 Partial Content) 和普通响应 (200)
            if response.status_code == 206:
                total_size = self._resume_offset + int(
                    response.headers.get("content-length", 0)
                )
            elif response.status_code == 200:
                if self._resume_offset > 0:
                    # 服务器不支持 Range，从头开始
                    self._resume_offset = 0
                    last_downloaded = 0
                    start_time = time.time()
                total_size = int(response.headers.get("content-length", 0))
            else:
                response.raise_for_status()
                total_size = 0  # unreachable

            self._active_response = response

            try:
                open_mode = "ab" if self._resume_offset > 0 else "wb"
                with open(save_path, open_mode) as f:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        # ── 取消检查 ──
                        if self._cancelled:
                            raise DownloadCancelledError("下载已被用户取消")

                        # ── 暂停等待 ──
                        if not self._pause_event.is_set():
                            self._report_status("下载已暂停")
                            self._pause_event.wait()  # 阻塞直到 resume() 调用
                            self._report_status(f"正在恢复下载{label}...")
                            # 暂停后恢复：关闭当前连接，下次循环用 Range 续传
                            self._resume_offset = last_downloaded
                            raise _PauseAndResume()

                        if chunk:
                            f.write(chunk)
                            self._resume_offset += len(chunk)
                            last_downloaded = self._resume_offset
                            elapsed = time.time() - start_time
                            speed = last_downloaded / elapsed if elapsed > 0 else 0
                            progress_cb(
                                last_downloaded, total_size, speed, elapsed
                            )

                # 正常完成 — 重置偏移量
                self._resume_offset = 0
                return

            except _PauseAndResume:
                # 暂停后恢复：循环回到开头用 Range 续传
                continue

            except DownloadCancelledError:
                raise

            except Exception as e:
                if self._cancelled:
                    raise DownloadCancelledError("下载已被用户取消") from e
                raise

            finally:
                self._active_response = None
                response.close()

    @staticmethod
    def _cleanup(*paths: str) -> None:
        """删除临时文件。"""
        for p in paths:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass


def _format_speed(speed: float) -> str:
    """格式化速度字符串。"""
    if speed >= 1024 * 1024:
        return f"{speed / (1024 * 1024):.1f} MB/s"
    elif speed >= 1024:
        return f"{speed / 1024:.1f} KB/s"
    else:
        return f"{speed:.1f} B/s"


def _format_time(seconds: float) -> str:
    """格式化时间字符串。"""
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60)}m"
    elif seconds >= 60:
        return f"{int(seconds // 60)}m{int(seconds % 60)}s"
    else:
        return f"{int(seconds)}s"
