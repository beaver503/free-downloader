import os
import json
import time
import uuid
import asyncio
import threading
from contextlib import asynccontextmanager
from urllib.parse import unquote

from dotenv import load_dotenv
load_dotenv()

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from downloader import VideoDownloader, DownloadProgress
from douyin import DouyinParser, is_douyin_url


downloader = VideoDownloader()
douyin_parser = DouyinParser(download_dir=downloader.DOWNLOAD_DIR)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    download_dir = downloader.DOWNLOAD_DIR
    if os.path.exists(download_dir):
        for f in os.listdir(download_dir):
            try:
                os.remove(os.path.join(download_dir, f))
            except OSError:
                pass


app = FastAPI(
    title="万能视频下载器 API",
    description="基于 yt-dlp 的万能视频下载服务，支持 1800+ 平台",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ParseRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str = "bestvideo+bestaudio/best"
    audio_only: bool = False


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "message": "万能视频下载器服务运行中"}


@app.post("/api/parse")
async def parse_video(req: ParseRequest):
    """解析视频信息（抖音走专用模块，其他走 yt-dlp）"""
    try:
        loop = asyncio.get_event_loop()
        if is_douyin_url(req.url):
            result = await loop.run_in_executor(None, douyin_parser.parse, req.url)
        else:
            result = await loop.run_in_executor(None, downloader.parse_video, req.url)
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail={
            "success": False,
            "error": f"解析失败: {str(e)}"
        })


@app.post("/api/download")
async def download_video(req: DownloadRequest):
    """服务端下载后提供文件下载（抖音走专用模块；audio_only 仅下载音频轨）"""
    try:
        loop = asyncio.get_event_loop()
        if is_douyin_url(req.url):
            if req.audio_only:
                result = await loop.run_in_executor(None, douyin_parser.download_audio, req.url)
            else:
                result = await loop.run_in_executor(None, douyin_parser.download, req.url)
        elif req.audio_only:
            result = await loop.run_in_executor(None, downloader.download_audio, req.url)
        else:
            result = await loop.run_in_executor(
                None, downloader.download_video, req.url, req.format_id
            )
        filepath = result["filepath"]
        if not os.path.exists(filepath):
            raise HTTPException(status_code=500, detail="下载的文件不存在")

        return FileResponse(
            path=filepath,
            filename=result["filename"],
            media_type="application/octet-stream",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail={
            "success": False,
            "error": f"下载失败: {str(e)}"
        })


@app.post("/api/direct-url")
async def get_direct_url(req: DownloadRequest):
    """获取视频直链"""
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, downloader.get_direct_url, req.url, req.format_id
        )
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=400, detail={
            "success": False,
            "error": f"获取直链失败: {str(e)}"
        })


# ===== 服务端下载（SSE 实时进度） =====

_download_states: dict = {}
_states_lock = threading.Lock()


def _pop_state(download_id: str):
    with _states_lock:
        _download_states.pop(download_id, None)


def _cleanup_stale_downloads(max_age: int = 3600):
    """清理超过 1 小时的临时下载文件"""
    now = time.time()
    try:
        for name in os.listdir(downloader.DOWNLOAD_DIR):
            path = os.path.join(downloader.DOWNLOAD_DIR, name)
            if os.path.isfile(path) and now - os.path.getmtime(path) > max_age:
                os.remove(path)
    except OSError:
        pass


@app.post("/api/download/stream", response_class=EventSourceResponse)
async def download_stream(req: DownloadRequest):
    """
    服务端下载（SSE 实时推送进度）
    事件类型:
      - progress → {status, percent, downloaded, total, speed, eta, error}
      - done     → {download_id, file_url, filename}（file_url 用于取回文件）
      - error    → {message}
    """
    try:
        download_id = uuid.uuid4().hex
        progress = DownloadProgress()
        with _states_lock:
            if len(_download_states) >= 500:
                _download_states.clear()
            _download_states[download_id] = progress

        _cleanup_stale_downloads()

        def run():
            try:
                if is_douyin_url(req.url):
                    if req.audio_only:
                        result = douyin_parser.download_audio(req.url, on_progress=progress.on_douyin_event)
                    else:
                        result = douyin_parser.download(req.url, on_progress=progress.on_douyin_event)
                elif req.audio_only:
                    result = downloader.download_audio(req.url, progress=progress)
                else:
                    result = downloader.download_video(req.url, req.format_id, progress=progress)
                progress.mark_finished(result)
            except Exception as e:
                progress.mark_error(str(e))

        asyncio.get_event_loop().run_in_executor(None, run)

        last_snapshot = None
        while True:
            snapshot = progress.snapshot()
            if snapshot != last_snapshot:
                last_snapshot = snapshot
                yield ServerSentEvent(
                    raw_data=json.dumps(snapshot, ensure_ascii=False),
                    event="progress",
                )
            if snapshot["status"] in ("finished", "error"):
                break
            await asyncio.sleep(0.3)

        if last_snapshot["status"] == "error":
            yield ServerSentEvent(
                raw_data=json.dumps({"message": last_snapshot["error"]}, ensure_ascii=False),
                event="error",
            )
        else:
            filename = progress.result["filename"] if progress.result else "video.mp4"
            yield ServerSentEvent(
                raw_data=json.dumps({
                    "download_id": download_id,
                    "file_url": f"/api/download/file/{download_id}",
                    "filename": filename,
                }, ensure_ascii=False),
                event="done",
            )
        # 状态记录延迟清理，给前端留出取回文件的时间
        asyncio.get_event_loop().call_later(3600, _pop_state, download_id)
    except Exception as e:
        yield ServerSentEvent(
            raw_data=json.dumps({"message": f"下载失败: {str(e)}"}, ensure_ascii=False),
            event="error",
        )


@app.get("/api/download/file/{download_id}")
async def get_download_file(download_id: str):
    """取回已完成下载的文件"""
    with _states_lock:
        progress = _download_states.get(download_id)
    if not progress or not progress.result:
        raise HTTPException(status_code=404, detail="下载任务不存在或尚未完成")
    result = progress.result
    if not os.path.exists(result["filepath"]):
        raise HTTPException(status_code=410, detail="文件已过期，请重新下载")
    return FileResponse(
        path=result["filepath"],
        filename=result["filename"],
        media_type="application/octet-stream",
    )


@app.get("/api/proxy/thumbnail")
async def proxy_thumbnail(url: str = Query(..., description="缩略图URL")):
    """代理获取视频缩略图，绕过防盗链"""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": url,
            })
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg")
            return StreamingResponse(
                iter([resp.content]),
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )
    except Exception:
        raise HTTPException(status_code=502, detail="缩略图加载失败")


# 挂载功能模块路由
from api_summarize import router as summarize_router

app.include_router(summarize_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
