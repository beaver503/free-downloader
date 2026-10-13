"""
抖音视频解析与下载模块
基于公开 API，无需 Cookie 和登录
原理：短链接重定向 → 提取 video_id → 公开 API 获取元数据 → 无水印播放地址
"""

import base64
import json
import hashlib
import os
import re
import shutil
import subprocess
import time
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests

logger = logging.getLogger("douyin")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Referer": "https://www.douyin.com/",
}

MOBILE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
        "Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.douyin.com/",
}

_URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def is_douyin_url(url: str) -> bool:
    """判断是否为抖音链接"""
    douyin_domains = [
        "douyin.com", "iesdouyin.com", "v.douyin.com",
        "www.douyin.com", "m.douyin.com",
    ]
    try:
        host = urlparse(url).netloc.lower()
        return any(d in host for d in douyin_domains)
    except Exception:
        return False


class DouyinParser:
    """抖音视频解析器，无需 Cookie"""

    API_URL = "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/"

    def __init__(self, download_dir: str = "downloads"):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = (10, 30)
        self.max_retries = 3

    def parse(self, url: str) -> dict:
        """解析抖音视频信息，返回统一格式"""
        share_url = self._extract_url(url)
        resolved_url = self._resolve_redirect(share_url)
        video_id = self._extract_video_id(resolved_url)

        item_info = self._fetch_item_info(video_id, resolved_url)
        return self._build_result(item_info, video_id)

    def download(self, url: str, mode: str = "video", on_progress=None) -> dict:
        """下载抖音视频，返回文件路径；on_progress 用于上报下载进度"""
        share_url = self._extract_url(url)
        resolved_url = self._resolve_redirect(share_url)
        video_id = self._extract_video_id(resolved_url)

        item_info = self._fetch_item_info(video_id, resolved_url)
        media_url = self._get_media_url(item_info, mode)
        title = item_info.get("desc") or f"douyin_{video_id}"
        safe_title = re.sub(r'[\\/*?:"<>|\n\r\t#@]', "_", title).strip("_. ")[:60]
        safe_title = re.sub(r'_+', '_', safe_title)
        if not safe_title:
            safe_title = f"douyin_{video_id}"

        ext = ".mp4" if mode == "video" else ".mp3"
        filename = f"{safe_title}{ext}"
        filepath = self.download_dir / filename

        self._download_file(media_url, filepath, on_progress=on_progress)

        return {
            "filepath": str(filepath),
            "filename": filename,
            "title": title,
            "ext": ext.lstrip("."),
        }

    def download_audio(self, url: str, on_progress=None) -> dict:
        """下载视频真实原声并转为 mp3：ffmpeg 抽取音轨，失败时回退音乐轨"""
        try:
            from downloader import _find_ffmpeg_path
            ffmpeg_dir = _find_ffmpeg_path()
        except Exception:
            ffmpeg_dir = None

        ffmpeg_bin = shutil.which("ffmpeg", path=ffmpeg_dir) if ffmpeg_dir else None
        if not ffmpeg_bin:
            return self.download(url, mode="audio", on_progress=on_progress)

        video_result = self.download(url, mode="video", on_progress=on_progress)
        video_path = Path(video_result["filepath"])
        mp3_path = video_path.with_suffix(".mp3")

        if on_progress:
            on_progress({"status": "processing"})

        try:
            subprocess.run(
                [ffmpeg_bin, "-y", "-i", str(video_path), "-vn",
                 "-acodec", "libmp3lame", "-b:a", "192k", str(mp3_path)],
                check=True, capture_output=True, timeout=600,
            )
        except Exception:
            return self.download(url, mode="audio")

        video_path.unlink(missing_ok=True)

        return {
            "filepath": str(mp3_path),
            "filename": mp3_path.name,
            "title": video_result["title"],
            "ext": "mp3",
        }

    def _extract_url(self, text: str) -> str:
        match = _URL_PATTERN.search(text)
        if not match:
            raise ValueError("未找到有效的抖音链接")
        candidate = match.group(0).strip().strip('"').strip("'")
        return candidate.rstrip(").,;!?")

    def _resolve_redirect(self, share_url: str) -> str:
        """解析短链接重定向"""
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(
                    share_url, timeout=self.timeout,
                    allow_redirects=True, headers=DEFAULT_HEADERS,
                )
                resp.raise_for_status()
                return resp.url
            except requests.RequestException as e:
                if attempt == self.max_retries - 1:
                    raise ValueError(f"链接解析失败: {e}")
                time.sleep(1 * (2 ** attempt))
        raise ValueError("链接解析失败")

    def _extract_video_id(self, url: str) -> str:
        """从 URL 中提取视频 ID"""
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        for key in ("modal_id", "item_ids", "group_id", "aweme_id"):
            values = query.get(key)
            if values:
                match = re.search(r"(\d{8,24})", values[0])
                if match:
                    return match.group(1)

        for pattern in (r"/video/(\d{8,24})", r"/note/(\d{8,24})", r"/(\d{8,24})(?:/|$)"):
            match = re.search(pattern, parsed.path)
            if match:
                return match.group(1)

        fallback = re.search(r"(\d{15,24})", url)
        if fallback:
            return fallback.group(1)

        raise ValueError("无法从链接中提取视频ID")

    def _fetch_item_info(self, video_id: str, resolved_url: str) -> dict:
        """获取视频元数据，优先公开 API，失败则解析分享页"""
        try:
            return self._fetch_via_api(video_id)
        except Exception as e:
            logger.warning("公开API获取失败(%s)，尝试分享页解析", e)
            return self._fetch_via_share_page(video_id, resolved_url)

    def _fetch_via_api(self, video_id: str) -> dict:
        params = {"item_ids": video_id}
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(
                    self.API_URL, params=params, timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                items = data.get("item_list") or []
                if items:
                    return items[0]
                raise ValueError("API 返回空数据")
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(1 * (2 ** attempt))
        raise ValueError("API 请求失败")

    def _fetch_via_share_page(self, video_id: str, resolved_url: str) -> dict:
        """从分享页面 HTML 中解析视频信息"""
        parsed = urlparse(resolved_url)
        if "iesdouyin.com" in (parsed.netloc or ""):
            share_url = resolved_url
        else:
            share_url = f"https://www.iesdouyin.com/share/video/{video_id}/"

        resp = self.session.get(share_url, headers=MOBILE_HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        html = resp.text or ""

        if "Please wait..." in html and "wci=" in html and "cs=" in html:
            html = self._solve_waf_and_retry(html, share_url)

        router_data = self._extract_router_data(html)
        if not router_data:
            raise ValueError("无法从分享页提取数据")

        loader_data = router_data.get("loaderData", {})
        for node in loader_data.values():
            if not isinstance(node, dict):
                continue
            video_info_res = node.get("videoInfoRes", {})
            if not isinstance(video_info_res, dict):
                continue
            item_list = video_info_res.get("item_list", [])
            if item_list and isinstance(item_list[0], dict):
                return item_list[0]

        raise ValueError("分享页中未找到视频信息")

    def _solve_waf_and_retry(self, html: str, page_url: str) -> str:
        """解决抖音 WAF 反爬验证"""
        match = re.search(r'wci="([^"]+)"\s*,\s*cs="([^"]+)"', html)
        if not match:
            return html

        cookie_name, challenge_blob = match.groups()
        try:
            decoded = self._decode_b64(challenge_blob).decode("utf-8")
            challenge_data = json.loads(decoded)
            prefix = self._decode_b64(challenge_data["v"]["a"])
            expected = self._decode_b64(challenge_data["v"]["c"]).hex()
        except (KeyError, ValueError):
            return html

        for candidate in range(1_000_001):
            digest = hashlib.sha256(prefix + str(candidate).encode()).hexdigest()
            if digest == expected:
                challenge_data["d"] = base64.b64encode(
                    str(candidate).encode()
                ).decode()
                cookie_val = base64.b64encode(
                    json.dumps(challenge_data, separators=(",", ":")).encode()
                ).decode()
                domain = urlparse(page_url).hostname or "www.iesdouyin.com"
                self.session.cookies.set(cookie_name, cookie_val, domain=domain, path="/")
                resp = self.session.get(page_url, headers=MOBILE_HEADERS, timeout=self.timeout)
                return resp.text or ""

        return html

    @staticmethod
    def _decode_b64(value: str) -> bytes:
        normalized = value.replace("-", "+").replace("_", "/")
        normalized += "=" * (-len(normalized) % 4)
        return base64.b64decode(normalized)

    def _extract_router_data(self, html: str) -> dict:
        marker = "window._ROUTER_DATA = "
        start = html.find(marker)
        if start < 0:
            return {}

        idx = start + len(marker)
        while idx < len(html) and html[idx].isspace():
            idx += 1
        if idx >= len(html) or html[idx] != "{":
            return {}

        depth = 0
        in_str = False
        escaped = False
        for cursor in range(idx, len(html)):
            ch = html[cursor]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[idx: cursor + 1])
                    except ValueError:
                        return {}
        return {}

    def _get_media_url(self, item_info: dict, mode: str = "video") -> str:
        """提取无水印播放地址"""
        if mode == "video":
            play_urls = (
                item_info.get("video", {})
                .get("play_addr", {})
                .get("url_list", [])
            )
            if not play_urls:
                raise ValueError("未找到视频播放地址")
            return play_urls[0].replace("playwm", "play")

        if mode == "audio":
            music = item_info.get("music", {})
            audio_urls = music.get("play_url", {}).get("url_list", [])
            if not audio_urls:
                raise ValueError("未找到音频地址")
            return audio_urls[0]

        raise ValueError(f"不支持的模式: {mode}")

    def _build_result(self, item_info: dict, video_id: str) -> dict:
        """构建与 yt-dlp 解析结果兼容的统一格式"""
        title = item_info.get("desc") or f"抖音视频_{video_id}"
        author = item_info.get("author", {})
        stats = item_info.get("statistics", {})

        video_info = item_info.get("video", {})
        play_urls = video_info.get("play_addr", {}).get("url_list", [])
        cover_urls = video_info.get("cover", {}).get("url_list", [])
        duration = video_info.get("duration", 0)
        duration_sec = duration // 1000 if duration > 1000 else duration

        formats = []
        if play_urls:
            clean_url = play_urls[0].replace("playwm", "play")
            width = video_info.get("width", 0)
            height = video_info.get("height", 0)
            formats.append({
                "format_id": "douyin_nowm",
                "ext": "mp4",
                "resolution": f"{width}x{height}" if width and height else "原始",
                "height": height or 720,
                "filesize": None,
                "filesize_approx": None,
                "vcodec": "h264",
                "acodec": "aac",
                "has_audio": True,
                "label": f"无水印 MP4 ({height}p)" if height else "无水印 MP4 (原始画质)",
                "_direct_url": clean_url,
            })

        return {
            "id": video_id,
            "title": title,
            "thumbnail": cover_urls[0] if cover_urls else "",
            "duration": duration_sec,
            "duration_string": self._fmt_duration(duration_sec),
            "uploader": author.get("nickname", "抖音用户"),
            "platform": "抖音",
            "view_count": stats.get("play_count") or stats.get("digg_count"),
            "upload_date": "",
            "description": title[:200],
            "formats": formats,
            "subtitles": [],
            "automatic_captions": [],
        }

    @staticmethod
    def _fmt_duration(seconds: Optional[int]) -> str:
        if not seconds:
            return "00:00"
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _download_file(self, url: str, filepath: Path, on_progress=None, chunk_size: int = 64 * 1024):
        """下载文件到本地；on_progress 定期回调 {status, downloaded, total, speed, eta}"""
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(
                    url, stream=True, timeout=self.timeout, allow_redirects=True,
                )
                resp.raise_for_status()

                total = int(resp.headers.get("Content-Length") or 0)
                temp_path = filepath.with_suffix(filepath.suffix + ".part")
                downloaded = 0
                last_bytes, last_ts, last_emit = 0, time.monotonic(), 0.0
                with temp_path.open("wb") as f:
                    for chunk in resp.iter_content(chunk_size=chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if on_progress:
                            now = time.monotonic()
                            # 节流上报（约 0.2s 一次）；结尾补发一次确保到 100%
                            if now - last_emit >= 0.2 or (total and downloaded >= total):
                                speed = (downloaded - last_bytes) / max(now - last_ts, 1e-6)
                                eta = int((total - downloaded) / speed) if total and speed > 0 else 0
                                on_progress({
                                    "status": "downloading",
                                    "downloaded": downloaded,
                                    "total": total,
                                    "speed": speed,
                                    "eta": eta,
                                })
                                last_bytes, last_ts, last_emit = downloaded, now, now
                if on_progress:
                    on_progress({
                        "status": "downloading",
                        "downloaded": downloaded,
                        "total": total,
                        "speed": 0,
                        "eta": 0,
                    })
                temp_path.replace(filepath)
                return
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise ValueError(f"文件下载失败: {e}")
                time.sleep(1 * (2 ** attempt))
