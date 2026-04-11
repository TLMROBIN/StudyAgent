import { createApp } from 'vue'
import 'element-plus/es/components/message/style/css'
import 'katex/dist/katex.min.css'

import App from './App.vue'
import pinia from './pinia'
import router from './router'
import './styles.css'
import { installSessionLifecycle } from './utils/sessionLifecycle'

const app = createApp(App)
app.use(pinia)
app.use(router)
installSessionLifecycle(pinia, router)
app.mount('#app')
