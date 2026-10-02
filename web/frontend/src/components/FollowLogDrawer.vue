<script setup lang="ts">
import { nextTick, ref, watch, onBeforeUnmount } from 'vue'
import type { KbLogLine } from '../api'

/**
 * 执行日志抽屉（共用组件）：增量轮询「seq 游标 + 只回增量」的日志接口，
 * 自动跟随尾部 + 上滚暂停 + 回到底部（业界 build log / 日志终端通行做法）。
 *
 * 抽取自设置页「知识库批次执行日志」抽屉——kb 批次日志（/api/kb/logs）与
 * RAG 重建日志（/api/kb/rag/logs）共用同一交互形态，禁止各复制一份跟随逻辑。
 * 数据契约由 props.load 提供：调用方负责把各自接口响应映射成统一形态
 * （progressText / progressPct 为 null 时隐藏进度区）。
 */
const props = defineProps<{
  /** 抽屉标题 */
  title: string
  /** 取增量日志：after 游标 → 运行态 + 新行 + 最新 seq + 进度（文本/百分比，可为 null） */
  load: (after: number) => Promise<{
    running: boolean
    lines: KbLogLine[]
    last_seq: number
    progressText: string | null
    progressPct: number | null
  }>
  /** 空日志时的占位说明 */
  emptyText: string
  /** 空闲时底部提示的补充说明 */
  idleHint?: string
  /** 抽屉宽度（移动端调用方传 '100%'） */
  size?: string
}>()

const open = defineModel<boolean>({ required: true })

const POLL_MS = 2000
const lines = ref<KbLogLine[]>([])
const lastSeq = ref(0)
const running = ref(false)
const progressText = ref<string | null>(null)
const progressPct = ref<number | null>(null)
const pollError = ref('')
const follow = ref(true) // 自动跟随尾部：用户上滚即暂停，点「回到底部」恢复
let timer: number | null = null
const box = ref<HTMLElement | null>(null)

async function poll() {
  if (document.hidden) return
  try {
    const r = await props.load(lastSeq.value)
    pollError.value = ''
    running.value = r.running
    progressText.value = r.progressText
    progressPct.value = r.progressPct
    if (r.lines.length) {
      lines.value.push(...r.lines)
      // 与后端环形缓冲同限幅：只保留最近 800 行
      if (lines.value.length > 800) lines.value.splice(0, lines.value.length - 800)
      lastSeq.value = r.last_seq
      await nextTick()
      if (follow.value) scrollBottom()
    }
  } catch {
    // 轮询失败可见但不打断（下一 tick 自动重试）：单机接口，失败多为瞬时
    pollError.value = '日志刷新失败，将自动重试'
  }
}

function scrollBottom() {
  const el = box.value
  if (el) el.scrollTop = el.scrollHeight
}

function onScroll() {
  const el = box.value
  if (!el) return
  // 距底部 < 40px 视为在底部 → 跟随；上滚即暂停跟随（业界日志终端惯例）
  follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
}

function startPolling() {
  if (timer !== null) return
  void poll()
  timer = window.setInterval(() => void poll(), POLL_MS)
}
function stopPolling() {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

watch(open, (v) => {
  if (v) {
    startPolling()
  } else {
    stopPolling()
    // 关闭即清空现场：下次打开从当前最新位置开始（不回放整段历史）
    lines.value = []
    lastSeq.value = 0
    follow.value = true
  }
})

onBeforeUnmount(stopPolling)
</script>

<template>
  <el-drawer v-model="open" :title="title" :size="size ?? '560px'" :append-to-body="true">
    <div class="kb-log-head">
      <el-tag size="small" :type="running ? 'primary' : 'info'">
        {{ running ? '执行中' : '空闲' }}
      </el-tag>
      <template v-if="progressText && progressPct !== null">
        <span class="text-muted">{{ progressText }}</span>
        <el-progress :percentage="progressPct" :stroke-width="8" class="kb-log-bar" />
      </template>
    </div>
    <div ref="box" class="kb-log-box" @scroll.passive="onScroll">
      <div v-if="!lines.length" class="text-muted kb-log-empty">{{ emptyText }}</div>
      <div v-for="l in lines" :key="l.seq" class="kb-log-line">{{ l.text }}</div>
    </div>
    <div class="kb-log-foot text-muted">
      <span v-if="!follow" class="kb-log-jump" @click="follow = true; scrollBottom()">↓ 回到底部（已暂停跟随）</span>
      <span v-else-if="pollError">{{ pollError }}</span>
      <span v-else>自动跟随最新输出{{ idleHint ?? '' }}</span>
    </div>
  </el-drawer>
</template>

<style scoped>
/* 样式 = 设置页「知识库批次执行日志」抽屉原样式原样迁移（getComputedStyle 同源，禁止两份漂移） */
.kb-log-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
  /* 文字行锁单行：阶段文案 + 进度条同行展示不换行 */
  white-space: nowrap;
  min-width: 0;
}

.kb-log-bar {
  flex: 1;
  min-width: 0;
}

.kb-log-box {
  height: calc(100% - 72px);
  overflow-y: auto;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-fill-color-darker, #1e1e1e);
  color: var(--el-text-color-regular, #d4d4d4);
  font-family: Consolas, 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.6;
  padding: 8px 10px;
}

.kb-log-line {
  word-break: break-all;
  /* 单行不强制 nowrap：日志含长标题，允许折行保证完整性 */
}

.kb-log-empty {
  padding: 16px 0;
  text-align: center;
  color: inherit;
  opacity: 0.7;
}

.kb-log-foot {
  margin-top: 8px;
  font-size: 12px;
  text-align: center;
}

.kb-log-jump {
  color: var(--el-color-primary);
  cursor: pointer;
  font-weight: 600;
}
</style>
