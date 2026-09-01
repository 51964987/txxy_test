import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
// 按需注册模板中 <el-icon><Xxx /></el-icon> 实际用到的图标（全量注册会显著增大主包体积）
import {
  Aim, ArrowRight, CaretTop, ChatDotRound, CircleCheck, CircleClose, Clock, Coin, Collection,
  DataLine, Delete, Document, Download, Expand, Files, Fold, Folder, FolderOpened, FullScreen,
  InfoFilled, List, Loading, Menu, Odometer, Refresh, Search, Star, Timer, TrendCharts, User,
} from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import { initWebVitals } from './utils/web-vitals'
import './style.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

for (const [key, component] of Object.entries({
  Aim, ArrowRight, CaretTop, ChatDotRound, CircleCheck, CircleClose, Clock, Coin, Collection,
  DataLine, Delete, Document, Download, Expand, Files, Fold, Folder, FolderOpened, FullScreen,
  InfoFilled, List, Loading, Menu, Odometer, Refresh, Search, Star, Timer, TrendCharts, User,
})) {
  app.component(key, component)
}

// 初始化轻量 Web Vitals 采集（console 输出，便于定位前端性能问题）
initWebVitals()

app.mount('#app')
