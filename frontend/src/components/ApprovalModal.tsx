import { useState } from 'react'

export interface PendingApproval {
  action: string
  // 后端 prepare_order 挂上来的是比价表那一行：字段叫 name（不是 title），
  // title 留着兼容旧快照/单测。
  product?: { product_id?: string; name?: string; title?: string; platform?: string } | null
  price?: number | null
  requested_at?: string
  [key: string]: unknown
}

interface Props {
  pending: PendingApproval
  submitting: boolean
  onDecide: (approved: boolean, reason?: string) => void
}

const ACTION_TEXT: Record<string, string> = {
  confirm_order: '确认下单',
}

// 语义从「审批」改成「下单前确认」：站内不收款，确认后跳到平台买。
export default function ApprovalModal({ pending, submitting, onDecide }: Props) {
  const [rejecting, setRejecting] = useState(false)
  const [reason, setReason] = useState('')

  const product = pending.product ?? null
  const productName = product?.name ?? product?.title ?? product?.product_id ?? '未选出具体商品'

  return (
    <div className="modal-overlay">
      <div className="modal-card">
        <div className="modal-label">下单前再确认一下</div>
        <h3 className="modal-title">{ACTION_TEXT[pending.action] ?? pending.action}</h3>

        <div className="modal-info-box">
          <div>
            <span className="modal-info-label">商品：</span>
            <span className="modal-info-value">{productName}</span>
          </div>
          <div>
            <span className="modal-info-label">金额：</span>
            <span className="modal-info-value">
              {pending.price === null || pending.price === undefined
                ? '价格未验证'
                : `¥ ${pending.price}`}
            </span>
          </div>
          {pending.requested_at && (
            <div style={{ color: 'var(--text-muted)', fontSize: 12, marginTop: 4 }}>
              发起时间：{new Date(pending.requested_at).toLocaleString()}
            </div>
          )}
        </div>

        {rejecting && (
          <div style={{ marginTop: 14 }}>
            <textarea
              className="modal-textarea"
              rows={2}
              placeholder="不买的原因（可选），会告诉 AI"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </div>
        )}

        <div className="modal-actions">
          {rejecting ? (
            <>
              <button className="btn" onClick={() => setRejecting(false)} disabled={submitting}>
                再想想
              </button>
              <button
                className="btn btn-danger"
                onClick={() => onDecide(false, reason.trim() || undefined)}
                disabled={submitting}
              >
                {submitting ? '处理中…' : '确认取消'}
              </button>
            </>
          ) : (
            <>
              <button
                className="btn btn-danger"
                onClick={() => setRejecting(true)}
                disabled={submitting}
              >
                取消
              </button>
              <button
                className="btn btn-primary"
                onClick={() => onDecide(true)}
                disabled={submitting}
              >
                {submitting ? '处理中…' : '确认购买'}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  )
}