import { createApp } from 'vue'
import 'element-plus/es/components/message/style/css'
import 'katex/dist/katex.min.css'

import App from './App.vue'
import pinia from './pinia'
import router from './router'
import './styles.css'
import { installSessionLifecycle } from './utils/sessionLifecycle'
import { installViewportHeight } from './utils/viewportHeight'

const app = createApp(App)
app.use(pinia)
app.use(router)
installSessionLifecycle(pinia, router)
installViewportHeight()
app.mount('#app')
