import { defineStore } from 'pinia'

/** 全局应用状态（当前用途：数据总览等页面的手动刷新版本号） */
export const useAppStore = defineStore('app', {
  state: () => ({
    refreshKey: 0,
    lastUpdated: '' as string,
  }),
  actions: {
    bumpRefresh() {
      this.refreshKey += 1
    },
    setLastUpdated(v: string) {
      this.lastUpdated = v
    },
  },
})
