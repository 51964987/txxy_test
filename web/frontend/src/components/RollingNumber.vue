<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(defineProps<{ value: number; duration?: number }>(), {
  duration: 700,
})

const display = ref(props.value)
let raf = 0
let cur = props.value

function animateTo(target: number) {
  cancelAnimationFrame(raf)
  const from = cur
  if (from === target) {
    display.value = target
    cur = target
    return
  }
  const start = performance.now()
  const dur = props.duration

  const step = (now: number) => {
    const p = Math.min(1, (now - start) / dur)
    // easeOutCubic：先快后慢，视觉友好
    const eased = 1 - Math.pow(1 - p, 3)
    cur = from + (target - from) * eased
    display.value = Math.round(cur)
    if (p < 1) raf = requestAnimationFrame(step)
    else cur = target
  }
  raf = requestAnimationFrame(step)
}

watch(() => props.value, (v) => animateTo(v))

onMounted(() => {
  cur = props.value
  display.value = props.value
})

onBeforeUnmount(() => cancelAnimationFrame(raf))
</script>

<template>
  <span>{{ display.toLocaleString() }}</span>
</template>
