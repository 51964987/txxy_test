import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
// 按需注册模板中 <el-icon><Xxx /></el-icon> 实际用到的图标（全量注册会显著增大主包体积）
import {
  CaretTop, ChatDotRound, Clock, Coin, Collection, DataLine, Document, Files,
  FolderOpened, Odometer, Star, TrendCharts, User,
} from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import './style.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

for (const [key, component] of Object.entries({
  CaretTop, ChatDotRound, Clock, Coin, Collection, DataLine, Document, Files,
  FolderOpened, Odometer, Star, TrendCharts, User,
})) {
  app.component(key, component)
}

app.mount('#app')
