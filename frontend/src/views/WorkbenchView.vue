<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <section
      v-if="meta.key === 'dedup_engine'"
      class="sourced-board"
      data-testid="sourced-board"
    >
      <header class="sourced-head">
        <div>
          <h3>跨批去重与来源核对</h3>
          <p class="page-desc">
            每个商品当前可领取哪条报价、出自哪个采集批次；撤回来源后刷新本页即可核对。
          </p>
        </div>
        <div class="page-actions">
          <button class="btn" data-testid="board-refresh" type="button" @click="reloadBoard">
            刷新领取页
          </button>
          <button class="btn primary" data-testid="board-deliver" type="button" :disabled="boardBusy" @click="deliverRound">
            执行一轮交付
          </button>
        </div>
      </header>

      <div class="stat-row">
        <article v-for="item in boardStats" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>商品</th>
            <th>领取页状态</th>
            <th>当前可领取</th>
            <th>已交付</th>
            <th>来源批次</th>
            <th>说明</th>
            <th>回执 / 撤回来源</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in boardState.items"
            :key="item.product_key"
            :data-testid="`board-row-${item.product_key}`"
            :data-state="item.state"
          >
            <td>{{ item.product_key }}</td>
            <td>{{ item.state }}</td>
            <td data-testid="board-claimable">
              <template v-if="item.claimable_source">
                报价 {{ item.claimable_source.quote }}（{{ item.claimable_source.batch_id }}）
              </template>
              <template v-else>—</template>
            </td>
            <td>{{ item.delivered_quote ?? '—' }}</td>
            <td>{{ item.source_batches.join(' / ') }}</td>
            <td>{{ item.reason }}</td>
            <td class="row-actions">
              <button
                v-if="item.claimable_source && item.state === '可领取'"
                class="link"
                type="button"
                :data-testid="`board-claim-${item.product_key}`"
                @click="claimProduct(item.product_key)"
              >
                领取
              </button>
              <span v-if="item.active_receipt_id">{{ item.active_receipt_id }}</span>
            </td>
            <td class="row-actions">
              <button
                v-for="batchId in item.source_batches"
                :key="`${item.product_key}-${batchId}`"
                class="link"
                type="button"
                :data-testid="`board-withdraw-${item.product_key}-${batchId}`"
                @click="withdrawBatch(batchId)"
              >
                撤回 {{ batchId }}
              </button>
            </td>
          </tr>
          <tr v-if="!boardState.items.length">
            <td colspan="8" class="empty-state">
              暂无跨批数据集，先在「采集批次」接口登记批次后刷新
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="pendingWithdrawals.length" class="pending-withdrawals">
        <h4>进行中的撤回任务</h4>
        <div
          v-for="job in pendingWithdrawals"
          :key="job.job_id"
          class="pending-job"
          :data-testid="`withdrawal-${job.job_id}`"
        >
          <span>{{ job.job_id }} · 批次 {{ job.batch_id }} · 已处理 {{ job.processed }}/{{ job.total }}</span>
          <button class="btn" type="button" @click="resumeWithdrawal(job.job_id)">继续处理</button>
        </div>
      </div>

      <div class="receipt-list">
        <h4>历史回执</h4>
        <table class="data-table">
          <thead>
            <tr><th>回执</th><th>商品</th><th>发出的报价</th><th>来源批次</th><th>数据集版本</th><th>发出时间</th><th>当时发出的行</th></tr>
          </thead>
          <tbody>
            <tr v-for="receipt in receipts" :key="receipt.receipt_id" :data-testid="`receipt-${receipt.receipt_id}`">
              <td>{{ receipt.receipt_id }}</td>
              <td>{{ receipt.product_key }}</td>
              <td>{{ receipt.quote }}</td>
              <td>{{ receipt.provenance.map((item) => `${item.batch_id}/${item.provenance_id}`).join('，') }}</td>
              <td>{{ receipt.dataset_version }}</td>
              <td>{{ receipt.issued_at }}</td>
              <td>{{ JSON.stringify(receipt.row) }}</td>
            </tr>
            <tr v-if="!receipts.length">
              <td colspan="7" class="empty-state">还没有发出过交付回执</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  claimProduct as claimProductApi,
  createEntry,
  deliverRound as deliverRoundApi,
  downloadEntries,
  listEntries,
  listReceipts,
  loadBoard,
  moduleMeta,
  resumeWithdrawal as resumeWithdrawalApi,
  runAction as applyAction,
  withdrawBatch as withdrawBatchApi,
} from '@/api/backend-service'
import { fetchJson } from '@/api/client'
import type {
  BoardState,
  DeliveryReceipt,
  EntryRow,
  WithdrawalJob,
} from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const boardState = ref<BoardState>({
  items: [],
  total: 0,
  counts: { claimable: 0, delivered: 0, reviewing: 0, unavailable: 0 },
  dataset_version: 0,
  generated_at: '',
})
const receipts = ref<DeliveryReceipt[]>([])
const pendingWithdrawals = ref<WithdrawalJob[]>([])
const boardBusy = ref(false)
const boardStats = computed(() => [
  { label: '可领取商品', value: boardState.value.counts.claimable },
  { label: '已交付商品', value: boardState.value.counts.delivered },
  { label: '核对中商品', value: boardState.value.counts.reviewing },
  { label: '数据集版本', value: boardState.value.dataset_version },
])
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

async function reloadBoard() {
  errorMessage.value = ''
  try {
    const [board, receiptPayload, withdrawalPayload] = await Promise.all([
      loadBoard(),
      listReceipts(),
      fetchJson<{ items: WithdrawalJob[] }>('/api/dedup/withdrawals'),
    ])
    boardState.value = board
    receipts.value = receiptPayload.items
    pendingWithdrawals.value = withdrawalPayload.items.filter((job) => job.status === '撤回中')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '领取页读取失败'
  }
}

async function claimProduct(productKey: string) {
  boardBusy.value = true
  errorMessage.value = ''
  try {
    const result = await claimProductApi(productKey)
    if (!result.ok) throw new Error(result.message)
    await reloadBoard()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '领取失败'
  } finally {
    boardBusy.value = false
  }
}

async function deliverRound() {
  boardBusy.value = true
  errorMessage.value = ''
  try {
    const result = await deliverRoundApi()
    if (!result.ok) throw new Error(result.message)
    await reloadBoard()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '交付失败'
  } finally {
    boardBusy.value = false
  }
}

async function withdrawBatch(batchId: string) {
  boardBusy.value = true
  errorMessage.value = ''
  try {
    const result = await withdrawBatchApi(batchId)
    if (!result.ok) throw new Error(result.message)
    await reloadBoard()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '撤回失败'
  } finally {
    boardBusy.value = false
  }
}

async function resumeWithdrawal(jobId: string) {
  boardBusy.value = true
  errorMessage.value = ''
  try {
    const result = await resumeWithdrawalApi(jobId)
    if (!result.ok) throw new Error(result.message)
    await reloadBoard()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '撤回任务续跑失败'
  } finally {
    boardBusy.value = false
  }
}

onMounted(async () => {
  await reload()
  if (meta.key === 'dedup_engine') await reloadBoard()
})
</script>
