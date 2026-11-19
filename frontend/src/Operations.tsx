import {useState} from 'react';
import {Alert, Button, Card, Form, Input, Modal, Select, Space, Statistic, Table, Checkbox, message} from 'antd';
import {api, post, qs, Filters} from './api';
import {attempt, ErrorBox, useRemote} from './shared';

export function Cost({profile, epoch}: {profile: string; epoch: number}) {
  const result = useRemote<{month: string; total: number; errors?: unknown[]; accounts?: {profile:string;total:number}[]; items: {name: string; amount: number}[]}>('/cost?'+qs({profile}), epoch);
  return <Card title="当月成本分析"><Alert type="info" message="Cost Explorer UnblendedCost，可能延迟，缓存 5 分钟；费用按当前账户查询。"/><ErrorBox error={result.error}/>{!!result.data?.errors?.length&&<Alert type="warning" message="部分账户费用读取失败" description={JSON.stringify(result.data.errors)}/>}<Statistic title={result.data?.month ?? '当月 USD'} value={result.data?.total ?? '—'} precision={2}/><Table rowKey="name" loading={result.loading} dataSource={result.data?.items} columns={[{title:'AWS 服务',dataIndex:'name'}, {title:'费用 USD',dataIndex:'amount',render:v => Number(v).toFixed(4)}]}/>{result.data?.accounts&&<Table rowKey="profile" dataSource={result.data.accounts} columns={[{title:'账户',dataIndex:'profile'},{title:'费用 USD',dataIndex:'total'}]}/>}</Card>;
}
type Setting = {profile:string;group_name:string;enabled:boolean;account_id?:string;errors?:string};
function AccountSettings({active, epoch, refresh}: {active: string | null; epoch: number; refresh: () => Promise<void>}) {
  const {data, error} = useRemote<{items: Setting[]}>('/account-settings', epoch);
  const [activating, setActivating] = useState<string>();
  const activate = (alias: string) => void attempt(async () => {
    setActivating(alias);
    try {
      await post('/key-accounts/activate', {alias});
      await refresh();
    } finally {
      setActivating(undefined);
    }
  });
  return <Card title="账户分组与启用状态" className="section">
    <ErrorBox error={error}/>
    <Table rowKey="profile" dataSource={data?.items} columns={[
      {title: '账户', dataIndex: 'profile'},
      {title: 'AWS Account ID', dataIndex: 'account_id'},
      {title: '分组 / 启用', render: (_, account) => <Form
        key={`${account.profile}/${account.group_name}/${account.enabled}`}
        layout="inline" initialValues={{group_name: account.group_name, enabled: !!account.enabled}}
        onFinish={values => void attempt(async () => {
          await post('/account-settings', {profile: account.profile, ...values});
          await refresh();
        })}>
        <Form.Item name="group_name"><Input placeholder="账户分组"/></Form.Item>
        <Form.Item name="enabled" valuePropName="checked"><Checkbox>启用</Checkbox></Form.Item>
        <Button htmlType="submit">保存</Button>
      </Form>},
      {title: '最近查询错误', dataIndex: 'errors'},
      {title: '当前账户', render: (_, account) => <Button
        loading={activating === account.profile} disabled={!account.enabled || !!activating || active === account.profile}
        onClick={() => activate(account.profile)}>{active === account.profile ? '已激活' : '激活'}</Button>},
    ]}/>
  </Card>;
}
type Key = {alias: string; aws_account_id: string; access_key_tail: string};
type Role = {alias: string; source_profile: string; role_arn: string};
export function Accounts({profiles, epoch, refresh, active}: {
  profiles: string[];
  epoch: number;
  refresh: () => Promise<void>;
  active: string | null;
}) {
  const keys = useRemote<{items: Key[]}>('/key-accounts', epoch), roles = useRemote<{items: Role[]}>('/accounts', epoch);
  const [keyForm] = Form.useForm(), [roleForm] = Form.useForm();
  const remove = (path: string) => Modal.confirm({title:'移除管理凭据？不会删除 AWS 账户或 IAM Key/Role。', onOk: () => attempt(async () => {await api(path,{method:'DELETE'}); await refresh();})});
  return <><AccountSettings active={active} epoch={epoch} refresh={refresh}/><Card title="AWS Access Key 账户" className="section"><ErrorBox error={keys.error}/><Table rowKey="alias" dataSource={keys.data?.items} columns={[
    {title:'别名',dataIndex:'alias'}, {title:'AWS Account ID',dataIndex:'aws_account_id'}, {title:'Access Key',dataIndex:'access_key_tail',render:v => '••••'+v},
    {title:'操作',render:(_,x) => <Space><Button onClick={() => void attempt(async () => {const r=await post<{aws_account_id:string}>('/key-accounts/'+x.alias+'/verify'); message.success('STS 验证通过：'+r.aws_account_id);})}>验证</Button><Button danger onClick={() => remove('/key-accounts/'+x.alias)}>移除</Button></Space>},
  ]}/><Form form={keyForm} layout="vertical" onFinish={v => void attempt(async () => {await post('/key-accounts',v); keyForm.resetFields(); await refresh();})}><Form.Item name="alias" label="别名" rules={[{required:true}]}><Input/></Form.Item><Form.Item name="access_key_id" label="Access Key ID" rules={[{required:true}]}><Input autoComplete="off"/></Form.Item><Form.Item name="secret_access_key" label="Secret Access Key" rules={[{required:true}]}><Input.Password autoComplete="new-password"/></Form.Item><Button type="primary" htmlType="submit">验证并加密保存</Button></Form></Card>
    <Card title="AssumeRole 账户" className="section"><ErrorBox error={roles.error}/><Table rowKey="alias" dataSource={roles.data?.items} columns={[{title:'别名',dataIndex:'alias'}, {title:'来源',dataIndex:'source_profile'}, {title:'Role ARN',dataIndex:'role_arn'}, {title:'操作',render:(_,x) => <Button danger onClick={() => remove('/accounts/'+x.alias)}>移除</Button>}]}/><Form form={roleForm} layout="vertical" onFinish={v => void attempt(async () => {await post('/accounts',v); roleForm.resetFields(); await refresh();})}><Form.Item name="alias" label="别名" rules={[{required:true}]}><Input/></Form.Item><Form.Item name="source_profile" label="来源 Profile / Access Key 账户" rules={[{required:true}]}><Select options={profiles.filter(p => !roles.data?.items.some(r => r.alias===p)).map(value => ({value,label:value}))}/></Form.Item><Form.Item name="role_arn" label="IAM Role ARN" rules={[{required:true}]}><Input/></Form.Item><Form.Item name="external_id" label="External ID"><Input.Password/></Form.Item><Button type="primary" htmlType="submit">保存角色</Button></Form></Card></>;
}

type Ip = {name: string; ip: string; attached_to: string; is_attached: boolean};
export function Network({filters, epoch, refresh}: {filters: Filters; epoch: number; refresh: () => void}) {
  const specific = filters.region !== 'all';
  const ips = useRemote<{items: Ip[]}>(specific ? '/lightsail/static-ips?'+qs({profile:filters.profile,region:filters.region}) : null, epoch);
  const history = useRemote<{items: Record<string, unknown>[]}>('/ip/history?'+qs({profile:filters.profile,region:filters.region==='all'?'':filters.region}), epoch);
  const [action, setAction] = useState('allocate'), [result, setResult] = useState('');
  return <><Card title="Lightsail 静态 IP"><Alert type="info" message="静态 IP 操作请选择具体区域。已绑定 IP 不能直接释放。"/>{specific && <><ErrorBox error={ips.error}/><Table rowKey="name" dataSource={ips.data?.items} columns={[{title:'名称',dataIndex:'name'}, {title:'IP',dataIndex:'ip'}, {title:'绑定实例',dataIndex:'attached_to'}]}/></>}<Form layout="vertical" onFinish={values => Modal.confirm({title:`确认 ${action} 静态 IP？`, onOk:() => attempt(async () => {await post('/lightsail/static-ip/action',{...values,profile:filters.profile,region:filters.region,action}); refresh();})})}><Form.Item label="操作"><Select value={action} onChange={setAction} options={[['allocate','分配'],['attach','绑定'],['detach','解绑'],['release','释放']].map(([value,label]) => ({value,label}))}/></Form.Item><Form.Item name="name" label="IP 资源名称" rules={[{required:true}]}><Input/></Form.Item>{action==='attach' && <Form.Item name="instance" label="目标实例" rules={[{required:true}]}><Input/></Form.Item>}{action==='release' && <Form.Item name="confirm" label="再次输入完整名称" rules={[{required:true}]}><Input/></Form.Item>}<Button disabled={!specific} htmlType="submit" danger={action==='release'}>提交</Button></Form></Card>
    <Card title="实例套餐与 TCP 检测" className="section"><Form layout="inline" onFinish={v => void attempt(async () => {
      const r = v.operation==='allowance' ? await api('/lightsail/allowance?'+qs({profile:filters.profile,region:filters.region,instance:v.instance})) : await post('/ip/check',{profile:filters.profile,region:filters.region,instance:v.instance,port:Number(v.operation)});
      setResult(JSON.stringify(r,null,2)); refresh();
    })}><Form.Item name="instance" rules={[{required:true}]}><Input placeholder="Lightsail 实例名称"/></Form.Item><Form.Item name="operation" initialValue="allowance"><Select style={{width:190}} options={[{value:'allowance',label:'查询套餐额度（非余额）'},...['22','80','443'].map(value => ({value,label:'TCP '+value}))]}/></Form.Item><Button disabled={!specific} htmlType="submit">查询 / 检测</Button></Form>{result && <pre className="detail">{result}</pre>}</Card>
    <Card title="TCP 检测历史" className="section"><ErrorBox error={history.error}/><Table rowKey="id" dataSource={history.data?.items} pagination={{pageSize:15}} scroll={{x:1000}} columns={['checked_at','profile','region','instance','address','port','reachable','latency_ms'].map(dataIndex => ({title:dataIndex,dataIndex,render:v => String(v ?? '')}))}/></Card></>;
}

export function Audit({profile, epoch}: {profile: string; epoch: number}) {
  const {data,error} = useRemote<{items: Record<string,unknown>[]}>('/audit?'+qs({profile, limit: '200'}), epoch);
  return <Card title="操作审计"><ErrorBox error={error}/><Table rowKey="id" scroll={{x:1200}} pagination={{pageSize:20}} dataSource={data?.items} columns={['created_at','category','profile','region','resource','operation','result','detail'].map(dataIndex => ({title:dataIndex,dataIndex}))}/></Card>;
}
export function Events({profile, epoch}: {profile: string; epoch: number}) {
  const {data,error} = useRemote<{items: Record<string,unknown>[]; deliveries: Record<string,unknown>[];historical_alerts:Record<string,unknown>[];historical_samples:Record<string,unknown>[]}>('/events?'+qs({profile}), epoch);
  return <><Card title="触发事件"><ErrorBox error={error}/><Table rowKey="id" dataSource={data?.items} columns={['created_at','rule_id','target','value','job_id','detail'].map(dataIndex => ({title:dataIndex,dataIndex}))} scroll={{x:1200}}/></Card><Card className="section" title="通知投递"><Table rowKey="id" dataSource={data?.deliveries} columns={['id','attempts','next_at','delivered'].map(dataIndex => ({title:dataIndex,dataIndex,render:v => String(v)}))}/></Card>{!!data?.historical_alerts.length&&<Card title="迁移前的告警记录" className="section"><Table rowKey="id" dataSource={data.historical_alerts} columns={['created_at','monitor_id','detail','delivered'].map(dataIndex=>({title:dataIndex,dataIndex}))}/></Card>}{!!data?.historical_samples.length&&<Card title="迁移前的一小时采样（不参与新累计）" className="section"><Table rowKey={x=>String(x.monitor_id)+'/'+x.measured_at} dataSource={data.historical_samples} columns={['measured_at','monitor_id','inbound','outbound','state','message'].map(dataIndex=>({title:dataIndex,dataIndex}))}/></Card>}</>;
}
