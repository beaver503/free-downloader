/**
 * AI 视频总结 API 封装
 * 使用原生 fetch + ReadableStream 处理 SSE 流式响应
 */
import { consumeSSE } from './sse.js'

export async function summarizeVideo(url, language = 'zh', callbacks = {}) {
  const response = await fetch('/api/summarize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, language }),
  })

  if (!response.ok) {
    throw new Error(`请求失败: ${response.status}`)
  }

  await consumeSSE(response, callbacks)
}

export async function chatWithVideo(url, question, subtitleText = '', callbacks = {}) {
  const response = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, question, subtitle_text: subtitleText }),
  })

  if (!response.ok) {
    throw new Error(`请求失败: ${response.status}`)
  }

  await consumeSSE(response, callbacks)
}
