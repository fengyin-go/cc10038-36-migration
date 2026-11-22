import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import DeliveryView from '@/views/DeliveryView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径必须先于 /:moduleKey，否则会被动态参数吞掉。
    { path: '/delivery', name: 'delivery', component: DeliveryView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
