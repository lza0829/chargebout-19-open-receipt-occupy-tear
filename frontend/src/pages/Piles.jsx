import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Piles() {
  const [list, setList] = useState([])
  const [msg, setMsg] = useState('')
  const load = () => api.桩位().then(setList)
  useEffect(() => {
    load()
  }, [])
  const setStatus = async (id, 状态) => {
    try {
      await api.改桩态(id, 状态)
      setMsg(`桩位 ${id} → ${状态}`)
      load()
    } catch (e) {
      setMsg(e.message)
    }
  }
  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>桩位</h2>
      <p className="lead">
        空闲灯由服务端同一谓词重放：状态空闲且无在充会话；占用必须有会话行钉住。撕裂会直接标出。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>站点</th>
              <th>桩名</th>
              <th>额定功率</th>
              <th>费率</th>
              <th>状态</th>
              <th>空闲灯</th>
              <th>可开充</th>
              <th>在充会话</th>
              <th>占用枪</th>
              <th>证据</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {list.map((p) => (
              <tr key={p.id}>
                <td>{p.site_name}</td>
                <td>{p.name}</td>
                <td>{p.rated_kw}</td>
                <td>{p.rate_name}</td>
                <td>
                  <span className={`tag ${p.status}`}>{p.status}</span>
                </td>
                <td>
                  <span className={`tag ${p.idle_lamp ? '空闲' : '占用'}`}>
                    {p.idle_lamp ? '空闲灯亮' : '空闲灯灭'}
                  </span>
                </td>
                <td>
                  <span className={`tag ${p.openable ? '可用' : '维护'}`}>
                    {p.openable ? '可正常开充' : '仅异常开充'}
                  </span>
                </td>
                <td>{p.open_sessions}</td>
                <td>{p.busy_guns}</td>
                <td>
                  {p.evidence_conflict ? (
                    <span className="tag 故障">桩态/占用钉/会话行撕裂</span>
                  ) : (
                    <span className="tag 空闲">一致</span>
                  )}
                </td>
                <td className="row" style={{ border: 0 }}>
                  {['空闲', '维护', '故障'].map((s) => (
                    <button
                      key={s}
                      type="button"
                      disabled={p.evidence_conflict}
                      onClick={() => setStatus(p.id, s)}
                    >
                      {s}
                    </button>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="msg">{msg}</p>
      </div>
    </div>
  )
}
