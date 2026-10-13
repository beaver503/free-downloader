import os
import re
import shutil
import threading
import yt_dlp
from typing import Optional


class DownloadProgress:
    """线程安全的下载进度状态：下载线程写入，SSE 轮询读取

    多分段下载（yt-dlp 合并模式：先下视频流、再下音频流）时，
    已完成分段的字节数累加进 parts_done，百分比按累计字节数计算。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "starting"  # starting / downloading / processing / finished / error
        self.parts_done = 0       # 已完成分段的累计字节数
        self.part_downloaded = 0  # 当前分段已下载字节
        self.part_total = 0       # 当前分段总字节
        self.part_key = None      # 当前分段标识（format_id / filename）
        self.part_finished = False
        self.speed = 0.0
        self.eta = 0
        self.error = ""
        self.result = None

    def snapshot(self) -> dict:
        with self.lock:
            total = self.parts_done + self.part_total
            downloaded = self.parts_done + self.part_downloaded
            percent = round(downloaded / total * 100, 1) if total > 0 else None
            # 合并/转码完成前不允许显示 100%，避免"假完成"
            if percent is not None and self.status != "finished":
                percent = min(percent, 99.9)
            return {
                "status": self.status,
                "percent": percent,
                "downloaded": downloaded,
                "total": total,
                "speed": self.speed or 0,
                "eta": self.eta or 0,
                "error": self.error,
            }

    # ---- yt-dlp 回调 ----
    def on_ytdlp_progress(self, d: dict):
        if d.get("status") == "downloading":
            info = d.get("info_dict") or {}
            key = info.get("format_id") or d.get("filename") or ""
            with self.lock:
                if key and key != self.part_key:
                    # 切换到新分段（如视频流下完开始下音频流）
                    if self.part_key is not None and not self.part_finished:
                        self.parts_done += self.part_total
                    self.part_key = key
                    self.part_downloaded = 0
                    self.part_total = 0
                    self.part_finished = False
                downloaded = d.get("downloaded_bytes") or 0
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                frag_count = d.get("fragment_count")
                if frag_count:
                    # HLS/DASH 分片流：按分片序号估算分段进度
                    per_frag = (downloaded / total) if total else 0
                    frac = min(((d.get("fragment_index") or 0) + per_frag) / frag_count, 1.0)
                    if total:
                        self.part_total = max(self.part_total, int(total * frag_count))
                        self.part_downloaded = int(self.part_total * frac)
                else:
                    self.part_downloaded = downloaded
                    self.part_total = max(self.part_total, total)
                self.speed = d.get("speed") or self.speed
                self.eta = d.get("eta") or 0
                self.status = "downloading"
        elif d.get("status") == "finished":
            with self.lock:
                self.parts_done += self.part_total or (d.get("downloaded_bytes") or 0)
                self.part_downloaded = 0
                self.part_total = 0
                self.part_finished = True
                self.speed = 0
                self.eta = 0
                self.status = "processing"

    def on_ytdlp_postprocess(self, d: dict):
        if d.get("status") == "started":
            with self.lock:
                self.status = "processing"

    # ---- 抖音下载回调 ----
    def on_douyin_event(self, ev: dict):
        with self.lock:
            if ev.get("status") == "downloading":
                self.status = "downloading"
                self.part_downloaded = ev.get("downloaded") or 0
                self.part_total = max(self.part_total, ev.get("total") or 0)
                self.speed = ev.get("speed") or 0
                self.eta = ev.get("eta") or 0
            elif ev.get("status") == "processing":
                self.parts_done += max(self.part_downloaded, self.part_total)
                self.part_downloaded = 0
                self.part_total = 0
                self.status = "processing"

    def mark_finished(self, result: dict):
        with self.lock:
            self.status = "finished"
            self.speed = 0
            self.eta = 0
            self.result = result

    def mark_error(self, message: str):
        with self.lock:
            self.status = "error"
            self.error = message


def _find_ffmpeg_path() -> Optional[str]:
    """查找 ffmpeg 可执行文件路径"""
    if shutil.which("ffmpeg"):
        return os.path.dirname(shutil.which("ffmpeg"))
    try:
        import static_ffmpeg
        paths = static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()
        return os.path.dirname(paths[0])
    except Exception:
        return None


class VideoDownloader:
    """yt-dlp 封装层，提供视频解析、下载、直链获取能力"""

    DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")

    def __init__(self):
        os.makedirs(self.DOWNLOAD_DIR, exist_ok=True)
        self.ffmpeg_path = _find_ffmpeg_path()
        self.has_ffmpeg = self.ffmpeg_path is not None

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        return re.sub(r'[\\/*?:"<>|]', "_", name)

    @staticmethod
    def _format_filesize(size: Optional[int]) -> str:
        if not size:
            return "未知大小"
        if size < 1024 * 1024:
            return f"{size / 1024:.0f}KB"
        if size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f}MB"
        return f"{size / (1024 * 1024 * 1024):.2f}GB"

    @staticmethod
    def _format_duration(seconds: Optional[int]) -> str:
        if not seconds:
            return "00:00"
        hours, remainder = divmod(int(seconds), 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def parse_video(self, url: str) -> dict:
        """解析视频信息，不下载文件"""
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise ValueError("无法解析该链接")

        formats = self._extract_formats(info)
        platform = info.get("extractor", info.get("extractor_key", "Unknown"))

        return {
            "id": info.get("id", ""),
            "title": info.get("title", "未知标题"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "duration_string": self._format_duration(info.get("duration")),
            "uploader": info.get("uploader", info.get("channel", "未知")),
            "platform": platform,
            "view_count": info.get("view_count"),
            "upload_date": info.get("upload_date", ""),
            "description": (info.get("description") or "")[:200],
            "formats": formats,
            "subtitles": list(info.get("subtitles", {}).keys()),
            "automatic_captions": list(info.get("automatic_captions", {}).keys())[:5],
        }

    def _extract_formats(self, info: dict) -> list:
        """从 yt-dlp info 中提取并整理可用格式"""
        raw_formats = info.get("formats", [])
        if not raw_formats:
            return []

        seen = set()
        results = []

        for f in raw_formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            height = f.get("height")
            ext = f.get("ext", "mp4")

            # 仅纯音频格式会显式标记 vcodec="none"；缺失（如 generic 直链）视为未知编码的视频
            has_video = vcodec != "none"
            has_audio = acodec != "none"

            if not has_video:
                continue

            resolution = f"{f.get('width', '?')}x{height}" if height else "未知"
            filesize = f.get("filesize") or f.get("filesize_approx")
            size_label = self._format_filesize(filesize)
            quality = f"{height}p" if height else ext.upper()

            if has_audio:
                label = f"{quality} {ext.upper()} ({size_label})"
                key = (height, ext, "av")
            else:
                label = f"{quality} {ext.upper()} (仅视频, {size_label})"
                key = (height, ext, "v")

            if key in seen:
                continue
            seen.add(key)

            results.append({
                "format_id": f.get("format_id", ""),
                "ext": ext,
                "resolution": resolution,
                "height": height or 0,
                "filesize": filesize,
                "filesize_approx": filesize,
                "vcodec": vcodec,
                "acodec": acodec if has_audio else None,
                "has_audio": has_audio,
                "label": label,
            })

        results.sort(key=lambda x: x["height"], reverse=True)

        if not any(r["has_audio"] for r in results) and results:
            best_video = results[0]
            merged = {
                **best_video,
                "format_id": f"bestvideo+bestaudio/best",
                "label": f"{best_video['height']}p 最佳 (视频+音频合并)",
                "has_audio": True,
                "acodec": "merged",
            }
            results.insert(0, merged)

        return results[:15]

    def download_video(self, url: str, format_id: str, progress: Optional[DownloadProgress] = None) -> dict:
        """下载视频到服务器临时目录，返回文件路径和元数据"""
        if not self.has_ffmpeg and "+" in format_id:
            format_id = "best"

        ydl_opts = {
            "format": format_id,
            "outtmpl": os.path.join(self.DOWNLOAD_DIR, "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

        if self.has_ffmpeg:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_path
            ydl_opts["merge_output_format"] = "mp4"

        if progress is not None:
            ydl_opts["progress_hooks"] = [progress.on_ytdlp_progress]
            ydl_opts["postprocessor_hooks"] = [progress.on_ytdlp_postprocess]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if not info:
            raise ValueError("下载失败")

        title = self._sanitize_filename(info.get("title", "video"))
        ext = info.get("ext", "mp4")
        filename = f"{title}.{ext}"
        filepath = os.path.join(self.DOWNLOAD_DIR, filename)

        if not os.path.exists(filepath):
            prepared = ydl.prepare_filename(info)
            if os.path.exists(prepared):
                filepath = prepared
                filename = os.path.basename(prepared)
            else:
                for f in os.listdir(self.DOWNLOAD_DIR):
                    if title in f:
                        filepath = os.path.join(self.DOWNLOAD_DIR, f)
                        filename = f
                        break

        return {
            "filepath": filepath,
            "filename": filename,
            "title": info.get("title", "video"),
            "ext": ext,
        }

    def download_audio(self, url: str, progress: Optional[DownloadProgress] = None) -> dict:
        """仅下载音频轨，有 ffmpeg 时转为 mp3"""
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(self.DOWNLOAD_DIR, "%(title)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

        if self.has_ffmpeg:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_path
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]

        if progress is not None:
            ydl_opts["progress_hooks"] = [progress.on_ytdlp_progress]
            ydl_opts["postprocessor_hooks"] = [progress.on_ytdlp_postprocess]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if not info:
            raise ValueError("下载失败")

        title = self._sanitize_filename(info.get("title", "audio"))
        filepath = info.get("filepath") or ""
        if not filepath or not os.path.exists(filepath):
            filepath = os.path.join(self.DOWNLOAD_DIR, f"{title}.mp3")
            if not os.path.exists(filepath):
                for f in os.listdir(self.DOWNLOAD_DIR):
                    if title in f:
                        filepath = os.path.join(self.DOWNLOAD_DIR, f)
                        break

        if not os.path.exists(filepath):
            raise ValueError("音频文件不存在")

        filename = os.path.basename(filepath)
        return {
            "filepath": filepath,
            "filename": filename,
            "title": info.get("title", "audio"),
            "ext": os.path.splitext(filename)[1].lstrip(".") or "mp3",
        }

    def get_direct_url(self, url: str, format_id: str) -> dict:
        """获取视频直链"""
        ydl_opts = {
            "format": format_id,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise ValueError("无法获取直链")

        direct_url = info.get("url")
        if not direct_url:
            requested = info.get("requested_formats")
            if requested and len(requested) > 0:
                direct_url = requested[0].get("url")

        if not direct_url:
            raise ValueError("该视频不支持直链下载，请使用服务端下载模式")

        return {
            "direct_url": direct_url,
            "ext": info.get("ext", "mp4"),
            "filesize": info.get("filesize") or info.get("filesize_approx"),
            "title": info.get("title", "video"),
        }
