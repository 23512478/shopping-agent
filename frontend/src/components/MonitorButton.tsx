import { useState } from 'react'
import { createPortal } from 'react-dom'
import { authHeaders } from '../api'
import { Product, minPrice } from '../catalog'

interface Props {
  product: Product
}

// 商品卡片上的「盯价」入口：点开弹窗填目标价，确认就建一条盯价。
// 弹窗用 portal 挂到 body——卡片自己有 hover 位移和溢出裁剪，留在卡片里会被裁掉。
export default function MonitorButton({ product }: Props) {
  const [open, setOpen] = useState(false)
  const [price, setPrice] = useState('')
  const [err, setErr] = useState('')
  const [state, setState] = useState<'idle' | 'saving' | 'done'>('idle')

  const floor = minPrice(product)

  function start(e: React.MouseEvent) {
    // 卡片整块是可点的（点了跳详情），盯价按钮不能把这个点击带上去
    e.stopPropagation()
    // 不给默认值：默认填现价的话，一点确认就是个「已经达标」的目标价，
    // 后台判「现价 <= 阈值」第一轮查价就会推一条没意义的提醒
    setPrice('')
    setErr('')
    setState('idle')
    setOpen(true)
  }

  async function submit() {
    const text = price.trim()
    const threshold = text ? Number(text) : undefined
    if (text && (!Number.isFinite(threshold) || (threshold as number) <= 0)) {
      setErr('目标价得是个正数')
      return
    }
    setState('saving')
    try {
      const r = await fetch('/api/monitor', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ product_ids: [product.product_id], threshold }),
      })
      if (r.ok) {
        setState('done')
        return
      }
      const data = await r.json().catch(() => null)
      setErr(data?.detail || '没盯上，稍后再试一次')
      setState('idle')
    } catch {
      setErr('没盯上，稍后再试一次')
      setState('idle')
    }
  }

  return (
    <>
      <button className="btn pcard-monitor" aria-label={`盯价 ${product.name}`} onClick={start}>
        盯价
      </button>

      {open &&
        createPortal(
          <div
            className="monitor-modal-mask"
            onClick={(e) => {
              e.stopPropagation()
              setOpen(false)
            }}
          >
            <div
              className="monitor-modal"
              role="dialog"
              aria-label="设置盯价"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="monitor-modal-head">
                <h3>盯价</h3>
                <button className="monitor-modal-close" aria-label="关闭" onClick={() => setOpen(false)}>
                  ×
                </button>
              </div>

              <div className="monitor-modal-name" title={product.name}>
                {product.name}
              </div>
              <div className="monitor-modal-now">
                当前最低价 {floor != null ? `¥${floor}` : '暂无报价'}
              </div>

              {state === 'done' ? (
                <>
                  <p className="monitor-modal-ok">已开始盯价，有降价会在「盯价」页提醒你</p>
                  <div className="monitor-modal-actions">
                    <a className="btn btn-primary" href="/monitor">
                      去盯价页看看
                    </a>
                    <button className="btn" onClick={() => setOpen(false)}>
                      知道了
                    </button>
                  </div>
                </>
              ) : (
                <>
                  <label className="monitor-modal-label" htmlFor="monitor-threshold">
                    目标价（降到多少提醒你）
                  </label>
                  <input
                    id="monitor-threshold"
                    className="input-field"
                    inputMode="decimal"
                    value={price}
                    onChange={(e) => setPrice(e.target.value)}
                    placeholder={floor != null ? `要低于 ¥${floor}` : '可不填'}
                  />
                  <p className="monitor-modal-hint">
                    {floor != null ? `它现在最低 ¥${floor}。` : ''}
                    填个更低的价就是降到这个价叫你；填高了或不填，就按现在的价盯着，比现在便宜就叫你。
                  </p>
                  {err && <p className="monitor-modal-err">{err}</p>}
                  <div className="monitor-modal-actions">
                    <button className="btn" onClick={() => setOpen(false)}>
                      取消
                    </button>
                    <button className="btn btn-primary" onClick={submit} disabled={state === 'saving'}>
                      {state === 'saving' ? '提交中…' : '确认盯价'}
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>,
          document.body,
        )}
    </>
  )
}
