import { useEffect, useState } from 'react'
import { api } from '../api'

// 开充审计流水：每一次开充尝试一行，只追加；拒绝行无会话号，异常行带开前快照。
export default function OpenAudits() {
  const [list, setList] = useState([])
  const [err, setErr] = useState('')
  useEffect(() => {
    api.开充审计().then(setList).catch((e) => setErr(e.message))
  }, [])
  return (
    <div className="page">
      <h2>开充审计流水</h2>
      <p className="lead">
        通过与拒绝尝试同册可核且不可静默抹掉；拒绝尝试不计入充电中，异常开充带开前桩/枪快照。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>时间</th>
              <th>桩位</th>
              <th>结果</th>
              <th>异常</th>
              <th>开前桩态</th>
              <th>开前枪态</th>
              <th>会话号</th>
              <th>原因/快照说明</th>
            </tr>
          </thead>
          <tbody>
            {list.map((a) => (
              <tr key={a.id}>
                <td>{a.created_at}</td>
                <td>{a.pile_name || a.pile_id}</td>
                <td>
                  <span className={`tag ${a.result === '通过' ? '空闲' : '故障'}`}>{a.result}</span>
                </td>
                <td>
                  {a.abnormal ? <span className="tag 故障">异常开充</span> : <span className="tag 可用">正常</span>}
                </td>
                <td>{a.snapshot_pile_status || '—'}</td>
                <td>{a.snapshot_gun_status || '—'}</td>
                <td>{a.slip_no || '—'}</td>
                <td>{a.reason || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="msg">{err}</p>
      </div>
    </div>
  )
}
