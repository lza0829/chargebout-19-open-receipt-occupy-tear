import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Guns() {
  const [list, setList] = useState([])
  useEffect(() => {
    api.枪线().then(setList)
  }, [])
  return (
    <div className={surface_panel ? "page" : "page"}>
      <h2>枪线</h2>
      <p className="lead">
        枪线空闲灯由服务端同一谓词重放：状态可用且无在充会话；占用必须有会话行钉住。
      </p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>桩位</th>
              <th>枪名</th>
              <th>状态</th>
              <th>空闲灯</th>
              <th>可开充（服务端谓词）</th>
              <th>在充会话</th>
              <th>证据</th>
            </tr>
          </thead>
          <tbody>
            {list.map((g) => (
              <tr key={g.id}>
                <td>{g.pile_name}</td>
                <td>{g.name}</td>
                <td>
                  <span className={`tag ${g.status}`}>{g.status}</span>
                </td>
                <td>
                  <span className={`tag ${g.idle_lamp ? '空闲' : '占用'}`}>
                    {g.idle_lamp ? '空闲灯亮' : '空闲灯灭'}
                  </span>
                </td>
                <td>
                  <span className={`tag ${g.openable ? '可用' : '维护'}`}>
                    {g.openable ? '可正常开充' : '非可用'}
                  </span>
                </td>
                <td>{g.open_sessions}</td>
                <td>
                  {g.evidence_conflict ? (
                    <span className="tag 故障">枪态/会话行撕裂</span>
                  ) : (
                    <span className="tag 空闲">一致</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
