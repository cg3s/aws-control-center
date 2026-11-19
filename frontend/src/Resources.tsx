import {useEffect, useState} from 'react';
import {Alert, Button, Card, Descriptions, Drawer, Form, Input, Select, Space, Spin, Table, Tag, message} from 'antd';
import {post, qs, Filters, Resource, Target, gib} from './api';
import {ErrorBox, attempt, useRemote, useTaskSubmission} from './shared';
type Traffic = {inbound: number; outbound: number; rate_mbps: number; complete: boolean; covered_buckets: number; expected_buckets: number; points: Array<{at: number; inbound: number; outbound: number}>};

export function TrafficView({target, epoch = 0}: {target: Target; epoch?: number}) {
  const [window, setWindow] = useState('1h'), [period,setPeriod] = useState(300);
  const {data, error, loading} = useRemote<Traffic>('/traffic?' + qs({profile: target.profile, region: target.region, service: target.service, instance: target.instance, window,period:String(period)}), epoch);
  const points = data?.points ?? [], max = Math.max(1, ...points.flatMap(p => [p.inbound, p.outbound]));
  return <Space direction="vertical" style={{width: '100%'}}><Select value={window} onChange={setWindow} options={[['5m', '最近 5 分钟'], ['1h', '最近 1 小时'], ['24h', '最近 1 天'], ['7d', '最近 7 天'], ['30d', '最近 30 天'], ['day', '今日 UTC'], ['month', '当月 UTC']].map(([value, label]) => ({value, label}))}/>
    {target.service==='ec2'&&<Select value={period} onChange={setPeriod} options={[{value:300,label:'5 分钟'},{value:60,label:'1 分钟（需已启用详细监控）'}]}/>}<Alert type="info" message="网络采样预留 10 分钟数据延迟；并非账单流量或套餐剩余额度。监控规则启用后会持续后台采集。1 分钟初次回填限最近 14 天。"/>
    <ErrorBox error={error}/>{loading && !data && <Spin tip="正在读取流量"><div style={{minHeight: 96}}/></Spin>}{data && <><Descriptions column={{xs: 1, sm: 2, md: 2, lg: 2, xl: 2, xxl: 2}} items={[
      {key: 'in', label: '入站', children: gib(data.inbound)}, {key: 'out', label: '出站', children: gib(data.outbound)},
      {key: 'rate', label: '平均总速率', children: data.rate_mbps.toFixed(3) + ' Mbps'},
      {key: 'quality', label: '完整性', children: `${data.covered_buckets}/${data.expected_buckets} ${data.complete ? '完整' : '有缺失'}`},
    ]}/><svg viewBox="0 0 800 160" width="100%" role="img" aria-label="流量趋势，蓝色入站、黄色出站">{(['inbound', 'outbound'] as const).map((metric, index) => <polyline key={metric} fill="none" stroke={index ? '#f7b667' : '#50c6f5'} strokeWidth="2" points={points.map((p, i) => `${(i+.5)*800/Math.max(1, points.length)},${150-(p[metric] ?? 0)/max*135}`).join(' ')}/>)}</svg>
    <Table size="small" loading={loading} rowKey="at" pagination={{pageSize: 12}} scroll={{x: 'max-content'}} dataSource={points} columns={[{title: 'UTC', dataIndex: 'at', render: v => new Date(v*1000).toISOString()}, {title: '入站', dataIndex: 'inbound', render: gib}, {title: '出站', dataIndex: 'outbound', render: gib}]}/></>}
  </Space>;
}

export function Resources({filters, epoch, refresh}: {filters: Filters; epoch: number; refresh: () => void}) {
  const [search, setSearch] = useState(''), [selected, setSelected] = useState<React.Key[]>([]);
  const [trafficResource, setTrafficResource] = useState<Resource>();
  const [trafficOpen, setTrafficOpen] = useState(false);
  const [scriptId, setScriptId] = useState(''), [connectionId, setConnectionId] = useState(''), [parameters,setParameters] = useState('');
  const {data, error, loading} = useRemote<{items: Resource[]; errors: unknown[]; updated_at: object}>('/resources?' + qs({profiles: filters.profile, region: filters.region, service: filters.service, search, limit: '500'}), epoch);
  const scripts = useRemote<{items: {id: string; name: string; version: number}[]}>('/configurations?kind=script', epoch);
  const connections = useRemote<{items: {id: string; name: string; version: number}[]}>('/configurations?'+qs({kind: 'connection', profile: filters.profile}), epoch);
  const jobs = useTaskSubmission(() => {setSelected([]); refresh();});
  useEffect(() => {setSelected([]); setTrafficOpen(false);}, [filters.profile, filters.region, filters.service, search]);
  const openTraffic = (resource: Resource) => {
    setTrafficResource(resource);
    setTrafficOpen(true);
  };
  const target = (x: Resource): Target => ({profile: x.profile, region: x.region, service: x.service, instance: x.id, account_id: x.account_id, partition: x.partition});
  const chosen = () => (data?.items ?? []).filter(x => selected.includes(x.resource_key));
  const run = (kind: string, rows = chosen()) => void attempt(async()=>{await jobs.submit(rows.map(x => ({kind, target: target(x), script_id: scriptId, connection_id: connectionId,parameters:kind==='script'&&parameters?JSON.parse(parameters):{}})));});
  return <><Card title="实例管理" extra={<Button onClick={() => void attempt(async () => {await post('/resources/refresh'); message.success('已请求后台刷新');})}>刷新 AWS 清单</Button>}>
    <Space wrap className="filters"><Input.Search placeholder="搜索名称、IP 或标签" onSearch={setSearch} allowClear/>{['start', 'stop', 'reboot', 'delete'].map((kind, i) => <Button key={kind} danger={kind === 'delete'} disabled={!selected.length} loading={jobs.busy} onClick={() => run(kind)}>{['批量启动', '批量停止', '批量重启', '批量删除'][i]}</Button>)}</Space>
    <ErrorBox error={error}/>{!!data?.errors.length && <Alert type="warning" message="部分账户或区域查询失败" description={JSON.stringify(data.errors)}/>}
    <Table loading={loading} rowKey="resource_key" dataSource={data?.items} pagination={{pageSize: 20}} scroll={{x: 1300}}
      rowSelection={{selectedRowKeys: selected, onChange: keys => {if (keys.length <= 100) setSelected(keys); else message.warning('每批最多 100 台');}}} columns={[
        {title: '账户', dataIndex: 'profile'}, {title: '区域', dataIndex: 'region'}, {title: '服务', dataIndex: 'service'}, {title: '实例', dataIndex: 'name'},
        {title: '状态', dataIndex: 'state', render: v => <Tag>{v}</Tag>}, {title: 'IP', dataIndex: 'ip'}, {title: '规格', dataIndex: 'type'},
        {title: '操作', render: (_, x) => <Space wrap><Button size="small" onClick={() => openTraffic(x)}>流量</Button><Button size="small" onClick={() => run('stop', [x])}>停止</Button><Button size="small" danger onClick={() => run('delete', [x])}>删除</Button></Space>},
      ]}/><div className="muted">清单更新时间：{JSON.stringify(data?.updated_at ?? {})}</div>
    <Form layout="inline" className="section"><Form.Item label="SSH 连接"><Select style={{width: 220}} value={connectionId} onChange={setConnectionId} options={[{value:'',label:'按实例绑定自动选择'},...(connections.data?.items.map(x => ({value: x.id, label: `${x.name} v${x.version}`}))??[])]}/></Form.Item><Form.Item label="脚本"><Select style={{width: 200}} value={scriptId || undefined} onChange={setScriptId} options={scripts.data?.items.map(x => ({value: x.id, label: `${x.name} v${x.version}`}))}/></Form.Item><Form.Item label="参数 JSON"><Input value={parameters} onChange={e=>setParameters(e.target.value)} placeholder={'{"APP_PORT":"8080"}'}/></Form.Item><Button disabled={!selected.length || !scriptId} onClick={() => run('script')}>对选中实例执行脚本</Button><Button disabled={!selected.length} onClick={() => run('bootstrap_verify')}>核验初始化</Button></Form>
  </Card>
    <Drawer
      title={trafficResource ? `${trafficResource.name || trafficResource.id} · 流量详情` : '流量详情'}
      placement="right"
      width="min(720px, 100vw)"
      open={trafficOpen}
      onClose={() => setTrafficOpen(false)}
      destroyOnHidden
    >
      {trafficResource && <TrafficView key={trafficResource.resource_key} target={target(trafficResource)} epoch={epoch}/>}
    </Drawer>
    {jobs.modal}
  </>;
}

export function TrafficCenter({filters,epoch}:{filters:Filters;epoch:number}) {
 const [window,setWindow]=useState('1h');
 const {data,error,loading}=useRemote<{inbound:number;outbound:number;complete:boolean;items:(Traffic&{target:Target})[]}>('/traffic/summary?'+qs({profiles:filters.profile,region:filters.region,service:filters.service,window}),epoch);
 return <Card title="流量中心 / 实例排名"><Space><Select value={window} onChange={setWindow} options={[['1h','最近一小时'],['24h','最近一天'],['7d','最近七天'],['30d','最近三十天'],['day','今日 UTC'],['month','当月 UTC']].map(([value,label])=>({value,label}))}/><span>入站 {gib(data?.inbound??0)} · 出站 {gib(data?.outbound??0)}</span></Space><Alert className="section" type="info" message="汇总已配置监控的实例网络指标，同账户同实例去重；缺失区间不会当作零流量。"/><ErrorBox error={error}/><Table loading={loading} rowKey={x=>`${x.target.account_id}/${x.target.region}/${x.target.instance}/${x.target.generation}`} dataSource={data?.items} columns={[{title:'账户',render:(_,x)=>x.target.profile},{title:'区域',render:(_,x)=>x.target.region},{title:'实例',render:(_,x)=>x.target.instance},{title:'入站',dataIndex:'inbound',render:gib},{title:'出站',dataIndex:'outbound',render:gib},{title:'平均 Mbps',dataIndex:'rate_mbps',render:v=>v.toFixed(3)},{title:'完整性',render:(_,x)=>`${x.covered_buckets}/${x.expected_buckets}`}]} scroll={{x:900}}/></Card>;
}
