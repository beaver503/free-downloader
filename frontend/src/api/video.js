import axios from 'axios'
import { consumeSSE } from './sse.js'

const api = axios.create({
  baseURL: '/api',
  timeout: 120000,
})

export async function parseVideo(url) {
  const { data } = await api.post('/parse', { url })
  return data
}

export async function getDirectUrl(url, formatId) {
  const { data } = await api.post('/direct-url', { url, format_id: formatId })
  return data
}

/**
 * 服务端下载（SSE 实时进度）
 * options: { formatId, audioOnly }
 * handlers: { onProgress(event), onDone({ download_id, file_url, filename }) }
 * HTTP 失败或收到 error 事件时抛出异常
 */
export async function downloadWithProgress(url, options = {}, handlers = {}) {
  const body = { url }
  if (options.formatId) body.format_id = options.formatId
  if (options.audioOnly) body.audio_only = true

  const response = await fetch('/api/download/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

  if (!response.ok) {
    let message = `请求失败: ${response.status}`
    try {
      const detail = (await response.json())?.detail
      if (detail) {
        message = typeof detail === 'string' ? detail : String(detail.error || JSON.stringify(detail))
      }
    } catch { /* 保留默认错误信息 */ }
    throw new Error(message)
  }

  await consumeSSE(response, {
    progress: (raw) => {
      try {
        handlers.onProgress?.(JSON.parse(raw))
      } catch { /* 忽略无法解析的进度事件 */ }
    },
    error: (raw) => {
      let message = raw
      try {
        message = JSON.parse(raw)?.message || message
      } catch { /* 保留原文 */ }
      throw new Error(message)
    },
    done: (raw) => {
      handlers.onDone?.(JSON.parse(raw))
    },
  })
}

/** 通过浏览器下载已完成文件（同源 GET，后端以 attachment 响应） */
export function triggerFileDownload(fileUrl, filename = '') {
  const a = document.createElement('a')
  a.href = fileUrl
  if (filename) a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
}
