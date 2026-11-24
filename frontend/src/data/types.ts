/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

/** 跨批去重来源：一条报价出自哪次采集。 */
export type QuoteSource = {
  quote: string | number
  batch_id: string
  provenance_id: string
  row_key: string
}

export type BoardItem = {
  product_key: string
  state: '可领取' | '已交付' | '核对中' | '无可领取'
  reason: string
  claimable_quote: string | number | null
  claimable_source: QuoteSource | null
  delivered_quote: string | number | null
  delivered_source:
    | (QuoteSource & { delivery_id: string; receipt_id: string })
    | null
  active_delivery_id: string | null
  active_receipt_id: string | null
  pending_withdrawal_id: string | null
  pending_withdrawal_batch: string | null
  source_batches: string[]
  source_count: number
  valid_source_count: number
  withdrawn_source_count: number
}

export type BoardState = {
  items: BoardItem[]
  total: number
  counts: { claimable: number; delivered: number; reviewing: number; unavailable: number }
  dataset_version: number
  generated_at: string
}

export type DeliveryReceipt = {
  receipt_id: string
  delivery_id: string
  round_id: string
  product_key: string
  quote: string | number
  row: Record<string, string | number>
  provenance: {
    provenance_id: string
    batch_id: string
    row_key: string
    row_no: number
  }[]
  dataset_version: number
  issued_at: string
}

export type WithdrawalJob = {
  job_id: string
  batch_id: string
  status: '撤回中' | '已完成'
  step_size: number
  total: number
  processed: number
  remaining: number
  products: string[]
  created_at: string
  updated_at: string
  completed_at: string | null
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
