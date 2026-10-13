<template>
  <div class="min-h-screen flex flex-col bg-bg-main">
    <AppHeader />
    <main class="flex-1">
      <HeroSection
        @parse="handleParse"
        :loading="loading"
        :compact="!!videoData"
        :showSlogan="!videoData || demoMode"
      />
      <!-- 视频信息 + AI 总结：左右双栏同屏布局 -->
      <section v-if="videoData" class="py-4 sm:py-6 bg-white">
        <div class="max-w-7xl mx-auto px-4 sm:px-6">
          <div class="flex flex-col lg:flex-row gap-6">
            <!-- 左栏：视频信息 -->
            <div class="w-full lg:w-2/5 lg:flex-shrink-0">
              <VideoResult
                :video="videoData"
                :downloading="downloading"
                :summarizing="summarizing"
                :progress="progress"
                @download="handleDownload"
                @download-audio="handleDownloadAudio"
                @summarize="handleSummarize"
              />
            </div>
            <!-- 右栏：AI 总结 -->
            <div class="w-full lg:w-3/5 min-w-0">
              <VideoSummary
                :videoUrl="currentUrl"
                :videoTitle="videoData.title"
                :key="summaryKey"
                @loading-change="handleSummarizeLoadingChange"
              />
            </div>
          </div>
        </div>
      </section>
      <FeatureSection />
      <HowToSection />
      <ComparisonSection />
      <PlatformSection />
    </main>
    <AppFooter />
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import AppHeader from './components/AppHeader.vue'
import HeroSection from './components/HeroSection.vue'
import VideoResult from './components/VideoResult.vue'
import VideoSummary from './components/VideoSummary.vue'
import FeatureSection from './components/FeatureSection.vue'
import HowToSection from './components/HowToSection.vue'
import ComparisonSection from './components/ComparisonSection.vue'
import PlatformSection from './components/PlatformSection.vue'
import AppFooter from './components/AppFooter.vue'
import { parseVideo, downloadWithProgress, triggerFileDownload } from './api/video.js'

const demoMode = ref(false)
let enterCount = 0
let enterTimer = null

function onKeyDown(e) {
  if (e.key === 'Enter' && !e.target.matches('input, textarea, [contenteditable]')) {
    enterCount++
    clearTimeout(enterTimer)
    if (enterCount >= 3) {
      demoMode.value = !demoMode.value
      enterCount = 0
    } else {
      enterTimer = setTimeout(() => { enterCount = 0 }, 800)
    }
  }
}

onMounted(() => {
  document.addEventListener('keydown', onKeyDown)
})
onBeforeUnmount(() => { document.removeEventListener('keydown', onKeyDown) })

// ===== 视频功能 =====
const loading = ref(false)
const downloading = ref(false)
const videoData = ref(null)
const currentUrl = ref('')
const summaryKey = ref(0)
const summarizing = ref(false)
// 下载进度：{ status, percent, downloaded, total, speed, eta, error }
const progress = ref(null)

function handleSummarize() {
  summaryKey.value++
}

function handleSummarizeLoadingChange(isLoading) {
  summarizing.value = isLoading
}

async function handleParse(url) {
  loading.value = true
  videoData.value = null
  progress.value = null
  currentUrl.value = url
  try {
    const res = await parseVideo(url)
    if (res.success) {
      videoData.value = res.data
      summaryKey.value++
    } else {
      alert('解析失败：' + (res.error || '未知错误'))
    }
  } catch (err) {
    const msg = err.response?.data?.detail?.error || err.response?.data?.detail || err.message
    alert('解析失败：' + msg)
  } finally {
    loading.value = false
  }
}

function cleanErrorMessage(msg) {
  // 剥离 yt-dlp 错误信息中的 ANSI 终端色码
  return msg.replace(/\x1b\[[0-9;]*m/g, '')
}

function handleProgressEvent(p) {
  progress.value = { ...progress.value, ...p }
}

async function runDownload(downloadFn) {
  downloading.value = true
  progress.value = { status: 'starting', percent: null, downloaded: 0, total: 0, speed: 0, eta: 0, error: '' }
  try {
    await downloadFn()
  } catch (err) {
    alert('下载失败：' + cleanErrorMessage(err.message || '请稍后重试'))
  } finally {
    downloading.value = false
  }
}

function handleDownload(formatId) {
  runDownload(() => downloadWithProgress(currentUrl.value, { formatId }, {
    onProgress: handleProgressEvent,
    onDone: (data) => triggerFileDownload(data.file_url, data.filename),
  }))
}

function handleDownloadAudio() {
  runDownload(() => downloadWithProgress(currentUrl.value, { audioOnly: true }, {
    onProgress: handleProgressEvent,
    onDone: (data) => triggerFileDownload(data.file_url, data.filename),
  }))
}
</script>
