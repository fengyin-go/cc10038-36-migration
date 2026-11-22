/** 版本化日志交付清单接口：迁移、切换、快照下载、回滚与核实。 */
import { request } from './client'

export type DeliveryPreview = {
  delivery_no: string
  view_name: string
  delivery_version: number
  structure: string
  file_name?: string
  file_path?: string
  digest?: string | null
  status?: string
  sealed?: boolean
  source_file_id?: string | null
  grouping_basis?: string[]
  field_count?: number
}

export type DeliveryItem = {
  delivery_no: string
  view_name: string
  delivery_version: number
  legacy: DeliveryPreview
  manifest: DeliveryPreview | null
  migration_status?: string
  read_mode: number
}

export type DeliveryList = {
  ok: boolean
  total: number
  items: DeliveryItem[]
}

export type ManifestPayload = {
  manifest_version: number
  structure: string
  delivery_no: string
  view_name: string
  delivery_version: number
  source: {
    file_id: string | null
    path: string | null
    digest: string | null
    confirmed: boolean
    resolution: string
    record_count: number
  }
  grouping: { basis: string[]; groups: Record<string, { count: number }> }
  fields: { raw_record_id: string; values: Record<string, string>; group_key: string }[]
  file: { file_id: string; path: string; digest: string } | null
  lineage: { lineage_id: number | null; downstream: string }
}

export type Snapshot = {
  ok: boolean
  code?: string
  message?: string
  read_version: number
  structure: string
  delivery_no: string
  view_name: string
  delivery_version: number
  snapshot_digest: string
  content: string
  payload: ManifestPayload
}

export type VerifyResult = {
  ok: boolean
  delivery_no: string
  view_name: string
  read_mode: { 读取版本: number } | null
  legacy: {
    structure: string
    file_name: string | null
    digest: string | null
    restorable: boolean
    migration_status: string | null
  }
  manifest: {
    manifest_id: number
    status: string
    sealed: boolean
    digest: string
    source_file_id: string | null
    grouping_basis: string[]
    group_count: number
    field_count: number
    generated_file_id: string | null
    lineage_id: number | null
  } | null
  files: {
    file_id: string
    role: string
    path: string
    digest: string
    structure: string
    ownership: string
  }[]
  source_manifest_fields: string[]
  source_manifest_rows: number
  migrations: { migration_no: string; status: string }[]
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(path, init)
  const payload = (await response.json()) as T
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return payload
}

export async function listDeliveries(viewName?: string): Promise<DeliveryList> {
  const query = viewName ? `?view_name=${encodeURIComponent(viewName)}` : ''
  return json<DeliveryList>(`/api/delivery/deliveries${query}`)
}

export async function fetchSnapshot(
  deliveryNo: string,
  readVersion: 1 | 2,
): Promise<Snapshot> {
  return json<Snapshot>(
    `/api/delivery/manifest/${encodeURIComponent(deliveryNo)}?read_version=${readVersion}`,
  )
}

export async function downloadDelivery(params: {
  delivery_no?: string
  view_name?: string
  read_version?: 1 | 2
}): Promise<Snapshot> {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') query.set(key, String(value))
  }
  return json<Snapshot>(`/api/delivery/download?${query}`)
}

export async function migrateDeliveries(payload: {
  delivery_nos?: string[]
  resume?: boolean
  fail_after?: number | null
}): Promise<{ ok: boolean; status: string; processed: string[]; blocked: { delivery_no: string; 原因: string }[] }> {
  return json('/api/delivery/migrate', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function pauseMigration(deliveryNo: string): Promise<{ ok: boolean; message: string }> {
  return json('/api/delivery/migrate/pause', {
    method: 'POST',
    body: JSON.stringify({ delivery_no: deliveryNo }),
  })
}

export async function resumeMigration(deliveryNo: string): Promise<{ ok: boolean; message: string }> {
  return json('/api/delivery/migrate/resume', {
    method: 'POST',
    body: JSON.stringify({ delivery_no: deliveryNo }),
  })
}

export async function switchReadMode(
  viewName: string,
  readVersion: 1 | 2,
): Promise<{ ok: boolean; message: string }> {
  return json('/api/delivery/read-mode', {
    method: 'POST',
    body: JSON.stringify({ view_name: viewName, read_version: readVersion }),
  })
}

export async function rollbackDelivery(payload: {
  delivery_no?: string
  view_name?: string
}): Promise<{ ok: boolean; message: string }> {
  return json('/api/delivery/rollback', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function verifyDelivery(deliveryNo: string): Promise<VerifyResult> {
  return json<VerifyResult>(`/api/delivery/verify/${encodeURIComponent(deliveryNo)}`)
}

export async function restoreFile(
  deliveryNo: string,
  readVersion: 1 | 2,
): Promise<{ ok: boolean; content: string; role: string; digest: string }> {
  return json(
    `/api/delivery/restore/${encodeURIComponent(deliveryNo)}?read_version=${readVersion}`,
  )
}

export async function bindSource(payload: {
  delivery_no: string
  content?: string
  field_order?: string[]
  grouping_basis?: string[]
}): Promise<{ ok: boolean; message: string }> {
  return json('/api/delivery/bind-source', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}
