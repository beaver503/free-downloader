/**
 * SSE 流式响应解析（AI 总结 / 下载进度共用）
 * 依据 SSE 规范：data: 行用 \n 拼接，空行触发事件派发
 */
export async function consumeSSE(response, callbacks) {
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let currentEvent = ''
  let dataLines = []
  let hasData = false

  function dispatch() {
    if (hasData && currentEvent) {
      const handler = callbacks[currentEvent]
      if (handler) handler(dataLines.join('\n'))
    }
    dataLines = []
    hasData = false
    currentEvent = ''
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (line === '') {
        dispatch()
        continue
      }

      if (line.startsWith(':')) continue

      const colonIdx = line.indexOf(':')
      if (colonIdx < 0) continue

      const field = line.slice(0, colonIdx)
      let val = line.slice(colonIdx + 1)
      if (val.startsWith(' ')) val = val.slice(1)

      if (field === 'event') {
        currentEvent = val
      } else if (field === 'data') {
        hasData = true
        dataLines.push(val)
      }
    }
  }
  dispatch()
}
