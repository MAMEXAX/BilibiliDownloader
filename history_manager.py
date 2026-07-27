"""
下载历史管理模块
负责记录和读取视频下载历史，以 JSON 文件存储。
"""

import json
import os
from datetime import datetime
from typing import Any

from path_utils import get_data_path


class DownloadHistory:
    """下载历史管理器，提供历史的增删查功能。"""

    def __init__(self, history_file: str | None = None):
        """初始化下载历史管理器。

        Args:
            history_file: 历史记录文件路径。默认为程序目录下的 download_history.json。
        """
        self._history_file = history_file or get_data_path("download_history.json")
        self._records: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        """从文件加载历史记录。"""
        if os.path.exists(self._history_file):
            try:
                with open(self._history_file, "r", encoding="utf-8") as f:
                    self._records = json.load(f)
            except (json.JSONDecodeError, IOError):
                self._records = []

    def _save(self) -> None:
        """将历史记录保存到文件。"""
        try:
            with open(self._history_file, "w", encoding="utf-8") as f:
                json.dump(self._records, f, ensure_ascii=False, indent=2)
        except IOError as e:
            print(f"保存历史记录失败: {e}")

    def add_record(self, url: str, title: str, quality: str, file_path: str) -> None:
        """添加一条下载记录。

        Args:
            url: 视频链接。
            title: 视频标题。
            quality: 下载清晰度。
            file_path: 保存的文件路径。
        """
        record = {
            "url": url,
            "title": title,
            "quality": quality,
            "file_path": file_path,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._records.insert(0, record)  # 最新的放在前面
        self._save()

    def get_all_records(self) -> list[dict[str, Any]]:
        """获取所有下载历史记录。

        Returns:
            list[dict]: 历史记录列表，按时间倒序排列。
        """
        return self._records

    def clear_all(self) -> None:
        """清空所有历史记录。"""
        self._records.clear()
        self._save()

    def search(self, keyword: str) -> list[dict[str, Any]]:
        """按关键词搜索历史记录。

        Args:
            keyword: 搜索关键词。

        Returns:
            list[dict]: 匹配的历史记录列表。
        """
        keyword_lower = keyword.lower()
        return [
            r for r in self._records
            if keyword_lower in r.get("title", "").lower()
            or keyword_lower in r.get("url", "").lower()
        ]
