import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Home() {
  const [o, setO] = useState(null)
  const [err, setErr] = useState('')
  const load = () =>
    api
      .总览()
      .then(setO)
      .catch((e) => setErr(e.message))
  useEffect(() => {
    load()
  }, [])
  if (!o) return <div className={surface_panel ? "page" : "page"}><p className="msg">{err || '加载中'}</p></div>
  const items = [
    ['桩位空闲（灯亮）', o.桩位空闲灯亮 ?? o.桩位空闲],
    ['桩位空闲（裸态）', o.桩位空闲],
    ['桩位占用', o.桩位占用],
    ['桩位故障', o.桩位故障],
    ['桩位维护', o.桩位维护],
    ['已开充（证据完整）', o['已开充(证据完整)'] ?? o.充电中],
    ['已开充证据撕裂', o.已开充证据撕裂 ?? 0],
    ['充电中（会话行）', o.充电中],
    ['结算中', o.结算中],
    ['已关账', o.已关账],
    ['已超时', o.已超时],
    ['已中断', o.已中断],
    ['在充电量', o.在充电量],
    ['本日关账金额', o.本日关账金额],
  ]
  const reconcile = o.对账 || {}
  const reconRows = [
    ['在充会话占空闲桩数', reconcile.在充会话占空闲桩数],
    ['无会话占用桩数', reconcile.无会话占用桩数],
    ['在充枪显示可用数', reconcile.在充枪显示可用数],
    ['审计通过无会话数', reconcile.审计通过无会话数],
    ['会话无通过审计数', reconcile.会话无通过审计数],
    ['桩枪证据撕裂数', reconcile.桩枪证据撕裂数],
  ]
  const buckets = o.异常占用分桶 || {}
  const gaps = o.开充缺口 || []
  return (
    <div className="page">
      <h2>总览</h2>
      <p className="lead">
        营业日 {o.营业日} · 充日{o.充日已封印 ? '已封印' : '未封印'}
      </p>
      <div className="grid">
        {items.map(([k, v]) => (
          <div className="stat" key={k}>
            <div className="k">{k}</div>
            <div className="v">{v}</div>
          </div>
        ))}
      </div>

      <div className="panel">
        <h3>异常占用（非空闲仍开充，只暴露不伪装空闲）</h3>
        <div className="grid">
          <div className="stat">
            <div className="k">异常占用在充数</div>
            <div className="v">{o.异常占用在充数 ?? 0}</div>
          </div>
          {Object.entries(buckets).map(([k, v]) => (
            <div className="stat" key={k}>
              <div className="k">异常·{k}</div>
              <div className="v">{v}</div>
            </div>
          ))}
          <div className="stat">
            <div className="k">本日开充通过</div>
            <div className="v">{o.本日开充通过 ?? 0}</div>
          </div>
          <div className="stat">
            <div className="k">本日开充拒绝</div>
            <div className="v">{o.本日开充拒绝 ?? 0}</div>
          </div>
        </div>
      </div>

      <div className="panel">
        <h3>
          六视图对账{' '}
          <span className={`tag ${reconcile.全部一致 ? '空闲' : '故障'}`}>
            {reconcile.全部一致 ? '全部一致' : '存在偏差'}
          </span>
        </h3>
        <table>
          <thead>
            <tr><th>不变量（写侧同一谓词重放）</th><th>偏差数</th></tr>
          </thead>
          <tbody>
            {reconRows.map(([k, v]) => (
              <tr key={k}>
                <td>{k}</td>
                <td>
                  <span className={`tag ${v === 0 ? '空闲' : '故障'}`}>{v}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3>
          开充缺口{' '}
          <span className={`tag ${(o.缺口数 ?? gaps.length) === 0 ? '空闲' : '故障'}`}>
            {o.缺口数 ?? gaps.length} 项
          </span>
        </h3>
        <p className="lead">
          桩/枪态与占用钉、会话行对不上的残留。缺口未清前不得亮「已开充」，只暴露不伪装。
        </p>
        {gaps.length === 0 ? (
          <p className="msg ok">无缺口</p>
        ) : (
          <table>
            <thead>
              <tr><th>对象</th><th>编号</th><th>名称</th><th>态</th><th>在充会话数</th><th>占用枪数</th><th>说明</th></tr>
            </thead>
            <tbody>
              {gaps.map((g) => (
                <tr key={`${g.对象}-${g.编号}`}>
                  <td>{g.对象}</td>
                  <td>{g.编号}</td>
                  <td>{g.名称}</td>
                  <td><span className="tag 故障">{g.态}</span></td>
                  <td>{g.在充会话数}</td>
                  <td>{g.占用枪数}</td>
                  <td>{g.说明}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <button type="button" onClick={load}>刷新</button>
      <p className="msg">{err}</p>
    </div>
  )
}
