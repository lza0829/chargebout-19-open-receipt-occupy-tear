import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Pulses() {
  const [list, setList] = useState([])
  const [id, setId] = useState('')
  const [kwh, setKwh] = useState('1.5')
  const [kw, setKw] = useState('7')
  const [msg, setMsg] = useState('')
  const load = () =>
    api.会话('充电中').then((rows) => {
      setList(rows)
      const ok = rows.find((s) => !s.evidence_conflict)
      setId(ok ? String(ok.id) : rows[0] ? String(rows[0].id) : '')
    })
  useEffect(() => {
    load()
  }, [])
  const selected = list.find((s) => String(s.id) === id)
  const torn = selected ? selected.evidence_conflict : false
  const submit = async () => {
    setMsg('')
    try {
      const r = await api.脉冲(Number(id), { 电量: Number(kwh), 功率: Number(kw) })
      setMsg(`累计电量 ${r.累计电量}，预估金额 ${r.预估金额}`)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>记脉冲</h2>
      <p className="lead">
        给充电中会话追加电量脉冲；入口以桩枪占用钉为准，证据撕裂的会话不得误亮，需先急停收口。
      </p>
      <div className="panel">
        <div className="row">
          <label>会话</label>
          <select value={id} onChange={(e) => setId(e.target.value)}>
            {list.map((s) => (
              <option key={s.id} value={s.id}>
                {s.slip_no} · {s.pile_name} · 已充 {s.kwh}
                {s.evidence_conflict ? ' · 证据撕裂' : ''}
              </option>
            ))}
          </select>
          <label>电量</label>
          <input value={kwh} onChange={(e) => setKwh(e.target.value)} style={{ width: 90 }} />
          <label>功率</label>
          <input value={kw} onChange={(e) => setKw(e.target.value)} style={{ width: 90 }} />
          <button type="button" className="primary" onClick={submit} disabled={torn}>
            写入脉冲
          </button>
        </div>
        {torn && (
          <p className="msg">
            该会话的桩/枪占用钉已失效（桩 {selected.pile_status} · 枪 {selected.gun_status}），
            脉冲入口已冻结；请到「超时中断」急停收口后再处理。
          </p>
        )}
        <p className={`msg ${msg.includes('累计') ? 'ok' : ''}`}>{msg}</p>
      </div>
    </div>
  )
}
