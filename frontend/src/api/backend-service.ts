import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  BoardState,
  DeliveryReceipt,
  EntryRow,
  OverviewResult,
  PageResult,
  WithdrawalJob,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 跨批去重与交付：领取页、交付回执和撤回任务都走同一组后端接口。 */
export type SourcedAction<T> = { ok: boolean; message: string; result?: T }

async function postSourced<T>(path: string, body: unknown): Promise<SourcedAction<T>> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    return { ok: false, message: String(payload.detail ?? `接口返回 ${response.status}`) }
  }
  return { ok: true, message: 'ok', result: payload as T }
}

export async function loadBoard(): Promise<BoardState> {
  const response = await request('/api/dedup/board')
  if (!response.ok) throw new Error('领取页读取失败')
  return await response.json()
}

export async function listReceipts(): Promise<{ items: DeliveryReceipt[]; total: number }> {
  const response = await request('/api/dedup/receipts')
  if (!response.ok) throw new Error('历史回执读取失败')
  return await response.json()
}

export function claimProduct(productKey: string, quote?: string | number) {
  return postSourced<{ receipt: DeliveryReceipt }>('/api/dedup/claim', {
    product_key: productKey,
    quote,
  })
}

export function deliverRound(limit?: number) {
  return postSourced<{ delivered_count: number; receipts: DeliveryReceipt[] }>(
    '/api/dedup/deliver',
    { limit },
  )
}

export function withdrawBatch(batchId: string, stepSize = 1) {
  return postSourced<{ job: WithdrawalJob }>('/api/dedup/withdrawals', {
    batch_id: batchId,
    step_size: stepSize,
  })
}

export function resumeWithdrawal(jobId: string, steps = 1) {
  return postSourced<{ job: WithdrawalJob }>(`/api/dedup/withdrawals/${jobId}/resume`, { steps })
}
