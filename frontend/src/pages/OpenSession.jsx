import { useEffect, useState } from 'react'
import { api } from '../api'

// 空闲判定一律以服务端写入侧同一谓词重放出的 openable 为准，前端不另写 status==='空闲'。
const surface_panel = true
export default function OpenSession() {
  const [piles, setPiles] = useState([])
  const [guns, setGuns] = useState([])
  const [pileId, setPileId] = useState('')
  const [gunId, setGunId] = useState('')
  const [msg, setMsg] = useState('')
  const [warn, setWarn] = useState(false)
  const [receipt, setReceipt] = useState(null)

  const loadPiles = () =>
    api.桩位().then((list) => {
      setPiles(list)
      const ok = list.find((p) => p.openable)
      if (ok && !pileId) setPileId(String(ok.id))
    })

  useEffect(() => {
    loadPiles()
  }, [])

  useEffect(() => {
    if (!pileId) return
    api.枪线(Number(pileId)).then((list) => {
      setGuns(list)
      const ok = list.find((g) => g.openable)
      setGunId(ok ? String(ok.id) : list[0] ? String(list[0].id) : '')
    })
  }, [pileId])

  const selectedPile = piles.find((p) => String(p.id) === pileId)
  const willAbnormal = selectedPile ? !selectedPile.openable : false

  const submit = async () => {
    setMsg('')
    setReceipt(null)
    setWarn(false)
    try {
      const body = { 桩位编号: Number(pileId) }
      if (gunId) body.枪线编号 = Number(gunId)
      const r = await api.开会话(body)
      setReceipt(r)
      setWarn(r.是否异常开充)
      setMsg(
        r.是否异常开充
          ? `已异常开充 ${r.会话号}（开前桩态 ${r.桩位开前态}）：${r.异常原因}`
          : `已开 ${r.会话号}，锁定电价 ${r.锁定电价}（开前桩态 ${r.桩位开前态} → 占用）`,
      )
      await loadPiles()
      // 成功灯必须与桩枪占用钉、会话行、审计同命运；回执缺证据就不得显示已开充。
      if (!r.成功 || !r.四者同命运) {
        setWarn(true)
        setMsg(`回执缺少同刷证据，未点亮成功灯（${r.会话号 || '无单号'}）`)
      }
    } catch (e) {
      setWarn(true)
      setMsg(e.message)
    }
  }

  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>开会话</h2>
      <p className="lead">
        架构口径：可告警仍开充。空闲桩正常开充并破空闲；非空闲桩仍可开，但会话、回执、审计均标记异常并在总览暴露。
      </p>
      <div className="panel">
        <div className="row">
          <label>桩位</label>
          <select value={pileId} onChange={(e) => setPileId(e.target.value)}>
            {piles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.site_name} · {p.name} · {p.status}
                {p.openable ? '' : ' · 不可正常开充'}
              </option>
            ))}
          </select>
          <label>枪线</label>
          <select value={gunId} onChange={(e) => setGunId(e.target.value)}>
            <option value="">自动选枪</option>
            {guns.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name} · {g.status}
                {g.openable ? '' : ' · 非可用'}
              </option>
            ))}
          </select>
          <button type="button" className="primary" onClick={submit}>
            发起开充
          </button>
        </div>
        {willAbnormal && (
          <p className="msg">
            该桩当前为{selectedPile.status}，不属于可正常开充；继续提交将落一笔异常会话并计入总览异常占用桶。
          </p>
        )}
        <p className={`msg ${warn ? '' : 'ok'}`}>{msg}</p>
        {receipt && (
          <table>
            <tbody>
              <tr><th>会话号</th><td>{receipt.会话号}</td></tr>
              <tr><th>是否异常开充</th><td>{receipt.是否异常开充 ? '是' : '否'}</td></tr>
              <tr><th>告警</th><td>{receipt.告警 || '—'}</td></tr>
              <tr><th>桩位开前态</th><td>{receipt.桩位开前态}</td></tr>
              <tr><th>枪线开前态</th><td>{receipt.枪线开前态}</td></tr>
              <tr><th>桩位被破空闲</th><td>{receipt.桩位被破空闲 ? '是' : '否'}</td></tr>
              <tr><th>枪线被占用</th><td>{receipt.枪线被占用 ? '是' : '否'}</td></tr>
              <tr><th>四者同命运</th><td>{receipt.四者同命运 ? '是' : '否'}</td></tr>
              <tr>
                <th>占用钉/会话/审计</th>
                <td>
                  {receipt.证据
                    ? `桩 ${receipt.证据.桩位占用钉} · 枪 ${receipt.证据.枪线占用钉} · 会话 ${receipt.证据.会话行} · 审计 #${receipt.证据.审计行}`
                    : '—'}
                </td>
              </tr>
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
