<template>
  <section class="page" data-testid="delivery-page">
    <header class="page-head">
      <div>
        <h2>版本化日志交付</h2>
        <p class="page-desc">
          存量按交付编号迁移为版本化清单；部署期新旧读取结构并存，可切换、暂停、回滚并逐份核实。
        </p>
      </div>
      <div class="page-actions">
        <label class="read-filter">
          <span>视图</span>
          <input
            v-model="viewFilter"
            data-testid="view-filter"
            placeholder="全部视图"
            @keyup.enter="reload"
          />
        </label>
        <button class="btn" data-testid="reload-deliveries" type="button" @click="reload">
          刷新交付
        </button>
        <button class="btn primary" data-testid="migrate-all" type="button" @click="migrateAll">
          迁移存量
        </button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="delivery-error">{{ errorMessage }}</p>
    <p v-if="notice" class="notice-text" data-testid="delivery-notice">{{ notice }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">交付总数</span>
        <strong class="stat-value" data-testid="delivery-total">{{ items.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已封存清单</span>
        <strong class="stat-value" data-testid="sealed-count">{{ sealedCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待迁移/受阻</span>
        <strong class="stat-value" data-testid="pending-count">{{ pendingCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">暂停迁移</span>
        <strong class="stat-value" data-testid="paused-count">{{ pausedCount }}</strong>
      </article>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>交付编号</th>
          <th>视图名称</th>
          <th>交付版本</th>
          <th>迁移状态</th>
          <th>旧结构文件</th>
          <th>版本化清单</th>
          <th>读取版本</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in items"
          :key="item.delivery_no"
          :data-testid="`delivery-row-${item.delivery_no}`"
          :data-delivery-no="item.delivery_no"
        >
          <td>{{ item.delivery_no }}</td>
          <td>{{ item.view_name }}</td>
          <td>v{{ item.delivery_version }}</td>
          <td :data-testid="`migration-status-${item.delivery_no}`">
            {{ item.migration_status || '—' }}
          </td>
          <td class="mono">
            <span :title="item.legacy.file_path || ''">{{ item.legacy.file_name }}</span>
            <span class="digest">{{ shortDigest(item.legacy.digest) }}</span>
          </td>
          <td class="mono">
            <template v-if="item.manifest">
              {{ item.manifest.sealed ? '已封存' : item.manifest.status }}
              <span class="digest">{{ shortDigest(item.manifest.digest) }}</span>
              <span class="digest">分组：{{ (item.manifest.grouping_basis || []).join('、') || '—' }}</span>
            </template>
            <template v-else>未迁移</template>
          </td>
          <td data-testid="`row-readmode-${item.delivery_no}`">{{ item.read_mode || '—' }}</td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :data-testid="`migrate-${item.delivery_no}`"
              @click="migrateOne(item.delivery_no)"
            >
              迁移
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`pause-${item.delivery_no}`"
              @click="pause(item.delivery_no)"
            >
              暂停迁移
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`resume-${item.delivery_no}`"
              @click="resume(item.delivery_no)"
            >
              恢复迁移
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`read-old-${item.delivery_no}`"
              @click="readAs(item, 1)"
            >
              读旧交付
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`read-new-${item.delivery_no}`"
              @click="readAs(item, 2)"
            >
              读版本化清单
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`verify-${item.delivery_no}`"
              @click="verify(item.delivery_no)"
            >
              核实
            </button>
            <button
              class="link"
              type="button"
              :data-testid="`rollback-${item.delivery_no}`"
              @click="rollback(item)"
            >
              回滚
            </button>
          </td>
        </tr>
        <tr v-if="!items.length">
          <td colspan="8" class="empty-state">暂无交付记录，可先执行导出或迁移存量</td>
        </tr>
      </tbody>
    </table>

    <section v-if="snapshot" class="detail-panel" data-testid="snapshot-panel">
      <header class="detail-head">
        <h3>
          下载快照：{{ snapshot.delivery_no }} ·
          {{ snapshot.read_version === 1 ? '旧读取结构' : '版本化清单' }}
        </h3>
        <span class="digest">快照摘要 {{ shortDigest(snapshot.snapshot_digest) }}</span>
        <button class="btn ghost" type="button" @click="snapshot = null">关闭</button>
      </header>
      <p v-if="!snapshot.ok" class="error-text" data-testid="snapshot-error">
        {{ snapshot.message || snapshot.code }}
      </p>
      <template v-else>
        <dl class="detail-grid">
          <div>
            <dt>视图 / 版本</dt>
            <dd>{{ snapshot.view_name }} / v{{ snapshot.delivery_version }}</dd>
          </div>
          <div>
            <dt>结构</dt>
            <dd>{{ snapshot.structure }}</dd>
          </div>
          <div v-if="snapshot.read_version === 2">
            <dt>来源文件</dt>
            <dd class="mono">
              {{ snapshot.payload.source.file_id || '—' }}
              <span class="digest">{{ shortDigest(snapshot.payload.source.digest) }}</span>
            </dd>
          </div>
          <div v-if="snapshot.read_version === 2">
            <dt>来源确认方式</dt>
            <dd>{{ snapshot.payload.source.resolution }}</dd>
          </div>
          <div v-if="snapshot.read_version === 2">
            <dt>分组依据 / 分组数</dt>
            <dd>
              {{ (snapshot.payload.grouping.basis || []).join('、') || '—' }} /
              {{ Object.keys(snapshot.payload.grouping.groups || {}).length }}
            </dd>
          </div>
        </dl>
        <pre class="snapshot-content" data-testid="snapshot-content">{{ snapshot.content }}</pre>
        <table
          v-if="snapshot.read_version === 2 && snapshot.payload.fields.length"
          class="data-table"
          data-testid="manifest-fields"
        >
          <thead>
            <tr>
              <th>原始记录ID</th>
              <th>分组键</th>
              <th>字段</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in snapshot.payload.fields" :key="row.raw_record_id">
              <td>{{ row.raw_record_id }}</td>
              <td>{{ row.group_key }}</td>
              <td class="mono">
                <span v-for="(value, key) in row.values" :key="key" class="field-chip">
                  {{ key }}={{ value }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </template>
    </section>

    <section v-if="verification" class="detail-panel" data-testid="verify-panel">
      <header class="detail-head">
        <h3>交付核实：{{ verification.delivery_no }}</h3>
        <button class="btn ghost" type="button" @click="verification = null">关闭</button>
      </header>
      <dl class="detail-grid">
        <div>
          <dt>视图 / 读取版本</dt>
          <dd>
            {{ verification.view_name }} /
            {{ verification.read_mode?.['读取版本'] ?? '—' }}
          </dd>
        </div>
        <div>
          <dt>旧结构文件</dt>
          <dd class="mono">
            {{ verification.legacy.file_name || '—' }}
            <span class="digest">{{ shortDigest(verification.legacy.digest) }}</span>
          </dd>
        </div>
        <div>
          <dt>旧文件可按原编号还原</dt>
          <dd>{{ verification.legacy.restorable ? '是' : '否' }}</dd>
        </div>
        <div>
          <dt>清单状态 / 分组依据</dt>
          <dd>
            {{ verification.manifest?.status || '—' }} /
            {{ (verification.manifest?.grouping_basis || []).join('、') || '—' }}
          </dd>
        </div>
        <div>
          <dt>来源文件 / 血缘</dt>
          <dd class="mono">
            {{ verification.manifest?.source_file_id || '—' }} /
            {{ verification.manifest?.lineage_id ?? '—' }}
          </dd>
        </div>
        <div>
          <dt>回写来源清单</dt>
          <dd>
            {{ verification.source_manifest_rows }} 行 ·
            {{ verification.source_manifest_fields.join('、') || '—' }}
          </dd>
        </div>
      </dl>
      <table class="data-table" data-testid="verify-files">
        <thead>
          <tr><th>文件角色</th><th>归属</th><th>路径</th><th>摘要</th></tr>
        </thead>
        <tbody>
          <tr v-for="file in verification.files" :key="file.file_id">
            <td>{{ file.role }}</td>
            <td>{{ file.ownership }}</td>
            <td class="mono">{{ file.path }}</td>
            <td class="mono digest">{{ shortDigest(file.digest) }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  type DeliveryItem,
  type Snapshot,
  type VerifyResult,
  downloadDelivery,
  listDeliveries,
  migrateDeliveries,
  pauseMigration,
  resumeMigration,
  rollbackDelivery,
  verifyDelivery,
} from '@/api/delivery'

const items = ref<DeliveryItem[]>([])
const viewFilter = ref('')
const errorMessage = ref('')
const notice = ref('')
const snapshot = ref<Snapshot | null>(null)
const verification = ref<VerifyResult | null>(null)

const sealedCount = computed(
  () => items.value.filter((item) => item.manifest?.sealed).length,
)
const pendingCount = computed(
  () =>
    items.value.filter(
      (item) => !item.manifest?.sealed && item.migration_status !== '已暂停',
    ).length,
)
const pausedCount = computed(
  () => items.value.filter((item) => item.migration_status === '已暂停').length,
)

function shortDigest(digest?: string | null): string {
  return digest ? digest.slice(0, 12) : '—'
}

function fail(error: unknown, fallback: string) {
  errorMessage.value = error instanceof Error ? error.message : fallback
}

async function reload() {
  errorMessage.value = ''
  try {
    const result = await listDeliveries(viewFilter.value.trim() || undefined)
    items.value = result.items
  } catch (error) {
    fail(error, '交付清单读取失败')
  }
}

async function migrateAll() {
  notice.value = ''
  try {
    const result = await migrateDeliveries({ resume: true })
    notice.value = `迁移${result.status}：已处理 ${result.processed.length} 份，受阻 ${result.blocked.length} 份`
    await reload()
  } catch (error) {
    fail(error, '存量迁移失败')
  }
}

async function migrateOne(deliveryNo: string) {
  notice.value = ''
  try {
    const result = await migrateDeliveries({ delivery_nos: [deliveryNo], resume: true })
    notice.value = `交付 ${deliveryNo} 迁移${result.status}`
    await reload()
  } catch (error) {
    fail(error, '交付迁移失败')
  }
}

async function pause(deliveryNo: string) {
  notice.value = ''
  try {
    const result = await pauseMigration(deliveryNo)
    notice.value = result.message
    await reload()
  } catch (error) {
    fail(error, '暂停迁移失败')
  }
}

async function resume(deliveryNo: string) {
  notice.value = ''
  try {
    const result = await resumeMigration(deliveryNo)
    notice.value = result.message
    await reload()
  } catch (error) {
    fail(error, '恢复迁移失败')
  }
}

async function readAs(item: DeliveryItem, readVersion: 1 | 2) {
  notice.value = ''
  try {
    snapshot.value = await downloadDelivery({
      delivery_no: item.delivery_no,
      read_version: readVersion,
    })
  } catch (error) {
    fail(error, '下载交付失败')
  }
}

async function verify(deliveryNo: string) {
  notice.value = ''
  try {
    verification.value = await verifyDelivery(deliveryNo)
  } catch (error) {
    fail(error, '交付核实失败')
  }
}

async function rollback(item: DeliveryItem) {
  notice.value = ''
  try {
    const result = await rollbackDelivery({ delivery_no: item.delivery_no })
    notice.value = result.message
    await reload()
  } catch (error) {
    fail(error, '回滚失败')
  }
}

onMounted(reload)
</script>

<style scoped>
.read-filter {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}
.notice-text {
  color: #0f766e;
  font-size: 13px;
}
.mono {
  font-family: 'SFMono-Regular', Consolas, monospace;
  font-size: 12px;
}
.digest {
  display: block;
  color: var(--muted);
  font-size: 11px;
}
.detail-panel {
  margin-top: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
}
.detail-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}
.detail-head h3 {
  margin: 0;
  font-size: 15px;
}
.detail-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px 16px;
  margin: 12px 0;
}
.detail-grid dt {
  font-size: 12px;
  color: var(--muted);
}
.detail-grid dd {
  margin: 2px 0 0;
  font-size: 13px;
}
.snapshot-content {
  background: #0f172a;
  color: #e2e8f0;
  padding: 10px 12px;
  border-radius: 6px;
  overflow-x: auto;
  font-size: 12px;
}
.field-chip {
  display: inline-block;
  margin: 0 6px 4px 0;
  padding: 1px 6px;
  background: #eef2ff;
  border-radius: 4px;
}
</style>
