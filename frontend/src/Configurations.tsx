import {useState} from 'react';
import {Alert, Button, Card, Checkbox, Form, Input, InputNumber, Modal, Select, Space, Table, Tag, message} from 'antd';
import {post, api, qs, Configuration, Resource} from './api';
import {attempt, ErrorBox, useRemote} from './shared';
import {FirewallFields} from './FirewallFields';
export function Configurations({kind, profile, epoch, refresh}: {kind: string; profile: string; epoch: number; refresh: () => void}) {
  const [form] = Form.useForm(), [simulation, setSimulation] = useState('');
  const dynamic = Form.useWatch('dynamic', form);
  const {data, error} = useRemote<{items: Configuration[]}>('/configurations?'+qs({kind, profile}), epoch);
  const resources = useRemote<{items: Resource[]}>('/resources?'+qs({profiles: profile, limit: '500'}), epoch);
  const scripts = useRemote<{items: Configuration[]}>('/configurations?kind=script', epoch);
  const connections = useRemote<{items: Configuration[]}>('/configurations?'+qs({kind: 'connection', profile}), epoch);
  const labels: Record<string, string> = {bootstrap: '初始化模板', script: '脚本库', connection: 'SSH 连接', rule: '监控与自动化规则'};
  const initial = {enabled: true, mode: 'notify', actions: ['notify'], metric: 'total', window: '1h', threshold: 1,
    consecutive: 2, cooldown: 3600, daily_limit: 3, timezone: 'UTC', port: 22, address: 'public', timeout: 120,
    root_login: 'disabled', password_auth: false};
  return <><Card title={labels[kind]}><ErrorBox error={error}/><Table rowKey="id" dataSource={data?.items} pagination={{pageSize: 10}} columns={[
    {title: '名称', dataIndex: 'name'}, {title: '版本', dataIndex: 'version'}, {title: 'ID', dataIndex: 'id'},
    {title: '状态', render: (_, x) => kind === 'rule' ? <Tag>{x.payload.enabled ? '启用' : '暂停'}</Tag> : '已保存'},
    {title: '操作', render: (_, x) => <Space wrap><Button onClick={() => {
      const p = {...x.payload};
      if(kind==='script')p.parameters_json=JSON.stringify(p.parameters??{});
      if(kind==='rule')p.script_parameters_json=JSON.stringify(p.script_parameters??{});
      if(kind==='rule'&&p.selector){const selector=p.selector as {profiles:string[];region:string;service:string;tags:Record<string,string>};p.dynamic=true;p.selector_region=selector.region;p.selector_service=selector.service;[p.tag_key,p.tag_value]=Object.entries(selector.tags)[0];}
      if (kind === 'rule') p.targets = (p.targets as Array<{profile: string; region: string; service: string; instance: string}>).map(t => resources.data?.items.find(r => r.profile === t.profile && r.region === t.region && r.service === t.service && r.id === t.instance)?.resource_key).filter(Boolean);
      form.resetFields(); form.setFieldsValue({...p, name: x.name});
      message.info('已复制到表单，保存会新增版本，不改变正在执行的任务。');
    }}>复制/新版本</Button>{kind === 'rule' && <><Button onClick={() => void attempt(async () => {await post('/configurations/'+x.id+'/enabled?enabled='+!x.payload.enabled); refresh();})}>{x.payload.enabled ? '暂停' : '启用'}</Button><Button onClick={() => void attempt(async () => setSimulation(JSON.stringify(await post('/rules/'+x.id+'/simulate'), null, 2)))}>历史模拟</Button></>}<Button danger onClick={() => Modal.confirm({title: '删除此配置版本？', onOk: () => attempt(async () => {await api('/configurations/'+x.id, {method: 'DELETE'}); refresh();})})}>删除</Button></Space>},
  ]}/></Card><Card title={'新增 '+labels[kind]+' 版本'} className="section"><Form form={form} layout="vertical" initialValues={initial} onFinish={values => void attempt(async () => {
    const {name, credential_configured: _configured, ...payload} = values;
    if(kind==='script'&&values.parameters_json)payload.parameters=JSON.parse(values.parameters_json);
    if(kind==='rule'&&values.script_parameters_json)payload.script_parameters=JSON.parse(values.script_parameters_json);
    if (kind === 'rule' && values.dynamic) {payload.targets=[];payload.selector={profiles:[profile],region:values.selector_region??'all',service:values.selector_service??'all',tags:{[values.tag_key]:values.tag_value}};}
    else if (kind === 'rule') {payload.selector=null;payload.targets = values.targets.map((id: string) => {
      const r = resources.data?.items.find(x => x.resource_key === id);
      if (!r) throw new Error('目标资源已经变化，请刷新后重选');
      return {profile: r.profile, region: r.region, service: r.service, instance: r.id};
    });}
    const allowed: Record<string, string[]> = {bootstrap: ['username','ssh_public_key','root_login','password_auth','user_data','firewall_rules'],
      connection: ['resource_key','username','port','address','fingerprint','private_key','password','sudo'], script: ['body','parameters','timeout','idempotent'],
      rule: ['targets','selector','aggregation','metric','window','period','threshold','recovery','consecutive','cooldown','daily_limit','timezone','enabled','mode','actions','script_id','script_parameters','connection_id','maintenance_until']};
    const cleaned = Object.fromEntries(Object.entries(payload).filter(([key, value]) => allowed[kind].includes(key) && value !== undefined && value !== ''));
    await post('/configurations', {kind, name, payload: cleaned});
    form.resetFields(); message.success('新版本已保存'); refresh();
  })}>
    <Form.Item name="name" label="名称" rules={[{required: true}]}><Input/></Form.Item>
    {kind === 'bootstrap' && <><Alert type="info" message="模板可启用 root 密码登录。每次创建实例时填写 root 初始密码。"/><Form.Item name="username" label="额外普通用户"><Input/></Form.Item><Form.Item name="ssh_public_key" label="SSH 公钥，每行一条"><Input.TextArea rows={3}/></Form.Item><Form.Item name="root_login" label="root SSH 策略"><Select onChange={value => {if (value === 'password') form.setFieldValue('password_auth', true);}}
      options={[{value: 'disabled', label: '禁止'}, {value: 'key', label: '仅密钥'}, {value: 'password', label: '允许密码（创建时填写）'}]}/></Form.Item><Form.Item name="password_auth" valuePropName="checked"><Checkbox>SSH 密码认证</Checkbox></Form.Item><Form.Item name="user_data" label="首次启动 Bash 脚本"><Input.TextArea rows={8}/></Form.Item><FirewallFields name="firewall_rules"/></>}
    {kind === 'script' && <><Form.Item name="body" label="Bash 脚本" rules={[{required: true}]}><Input.TextArea rows={10}/></Form.Item><Form.Item name="parameters_json" label="默认参数（环境变量 JSON，可选）"><Input.TextArea placeholder={'{"APP_PORT":"8080"}'}/></Form.Item><Form.Item name="timeout" label="超时秒数"><InputNumber min={1} max={1800}/></Form.Item><Form.Item name="idempotent" valuePropName="checked"><Checkbox>此脚本可重复执行</Checkbox></Form.Item><Alert type="info" message="参数以环境变量传入，可使用 $APP_PORT 等变量。AWSCC_INSTANCE、AWSCC_REGION、AWSCC_PROFILE 自动提供。超时或断线后不会自动重试。"/></>}
    {kind === 'connection' && <><Alert type="info" message="目标地址仅取自 AWS 实例信息。请通过可信渠道核对主机 SHA256 指纹后填写，指纹变化将阻止执行。"/><Form.Item name="resource_key" label="绑定实例（用于批量脚本自动选择此实例最新连接版本）"><Select allowClear options={resources.data?.items.map(r=>({value:r.resource_key,label:`${r.profile}/${r.region}/${r.name}`}))}/></Form.Item><Form.Item name="username" label="SSH 用户" rules={[{required: true}]}><Input/></Form.Item><Form.Item name="port" label="端口"><InputNumber min={1} max={65535}/></Form.Item><Form.Item name="address" label="连接地址"><Select options={[{value: 'public', label: 'AWS 公网 IP'}, {value: 'private', label: 'AWS 私网 IP'}]}/></Form.Item><Form.Item name="fingerprint" label="主机指纹 SHA256:..." rules={[{required: true}]}><Input/></Form.Item><Form.Item name="private_key" label="SSH 私钥（加密保存，不回显）"><Input.TextArea rows={5} autoComplete="off"/></Form.Item><Form.Item name="password" label="SSH 密码（可选，加密保存，不回显）"><Input.Password autoComplete="new-password"/></Form.Item><Form.Item name="sudo" valuePropName="checked"><Checkbox>使用 sudo -n 执行（需要免密 sudo）</Checkbox></Form.Item></>}
    {kind === 'rule' && <><Form.Item name="dynamic" valuePropName="checked"><Checkbox>根据 AWS 标签动态选择实例</Checkbox></Form.Item>{dynamic?<><Form.Item name="tag_key" label="标签名称" rules={[{required:true}]}><Input placeholder="environment"/></Form.Item><Form.Item name="tag_value" label="标签值" rules={[{required:true}]}><Input placeholder="production"/></Form.Item><Form.Item name="selector_region" label="限制区域" initialValue="all"><Select options={['all',...new Set(resources.data?.items.map(r=>r.region))].map(value=>({value,label:value}))}/></Form.Item><Form.Item name="selector_service" label="限制服务" initialValue="all"><Select options={['all','ec2','lightsail'].map(value=>({value,label:value}))}/></Form.Item><Alert type="warning" message="规则将持续匹配新加入的标签实例，每条规则最多 100 台。自动执行模式也适用于未来匹配的实例；触发时会冻结目标清单。"/></>:<Form.Item name="targets" label="监控实例" rules={[{required: true}]}><Select mode="multiple" showSearch optionFilterProp="label" options={resources.data?.items.map(r => ({value: r.resource_key, label: `${r.profile} / ${r.region} / ${r.name}`}))}/></Form.Item>}
      <Form.Item name="aggregation" label="阈值作用范围" initialValue="instance"><Select options={[{value:'instance',label:'每个实例独立判断'},{value:'group',label:'选中实例合计判断，触发后对整个集合执行动作'}]}/></Form.Item>
      <Form.Item name="metric" label="指标"><Select options={[['total','入站加出站 GiB'],['inbound','入站 GiB'],['outbound','出站 GiB'],['rate_mbps','平均总速率 Mbps']].map(([value,label]) => ({value,label}))}/></Form.Item><Form.Item name="window" label="统计窗口"><Select options={[['5m','5 分钟'],['1h','1 小时'],['day','自然日'],['month','自然月']].map(([value,label]) => ({value,label}))}/></Form.Item>
      <Form.Item name="period" label="采样粒度" initialValue={300}><Select options={[{value:300,label:'5 分钟（EC2 / Lightsail）'},{value:60,label:'1 分钟（已开启详细监控的 EC2）'}]}/></Form.Item><Form.Item name="timezone" label="日/月边界时区"><Input placeholder="UTC 或 Asia/Shanghai"/></Form.Item><Form.Item name="threshold" label="触发阈值"><InputNumber min={0.001}/></Form.Item><Form.Item name="recovery" label="恢复阈值（默认触发值的 90%）"><InputNumber min={0}/></Form.Item><Form.Item name="consecutive" label="连续超限次数"><InputNumber min={1} max={100}/></Form.Item><Form.Item name="cooldown" label="冷却秒数"><InputNumber min={300}/></Form.Item><Form.Item name="daily_limit" label="每日最多触发次数"><InputNumber min={1} max={100}/></Form.Item>
      <Form.Item name="mode" label="执行模式"><Select options={[{value:'notify',label:'仅通知'}, {value:'execute',label:'自动执行以下动作'}]}/></Form.Item><Form.Item name="actions" label="动作顺序（按选择顺序执行）" rules={[{required:true}]}><Select mode="multiple" options={[{value:'notify',label:'通知'}, {value:'script',label:'SSH 脚本'}, {value:'stop',label:'停止实例'}]}/></Form.Item><Form.Item name="script_id" label="脚本版本"><Select allowClear options={scripts.data?.items.map(x => ({value:x.id,label:`${x.name} v${x.version}`}))}/></Form.Item><Form.Item name="script_parameters_json" label="脚本参数覆盖（环境变量 JSON）"><Input.TextArea placeholder={'{"APP_PORT":"8080"}'}/></Form.Item><Form.Item name="connection_id" label="SSH 连接版本（留空按实例绑定自动选择）"><Select allowClear options={connections.data?.items.map(x => ({value:x.id,label:`${x.name} v${x.version}`}))}/></Form.Item><Form.Item name="maintenance_until" label="维护截止时间（带时区，可选）"><Input placeholder="2026-12-01T08:00:00+08:00"/></Form.Item><Form.Item name="enabled" valuePropName="checked"><Checkbox>启用规则</Checkbox></Form.Item><Alert type="warning" message="自动执行会停止实例或运行脚本。脚本准备失败或超时仍会继续后续停机；缺失/过期流量数据不会触发修改。持续超限只触发一次，恢复或周期重置后重新布防。"/></>}
    <Button type="primary" htmlType="submit" className="section">保存新版本</Button>
  </Form></Card>{simulation && <Card title="模拟结果" className="section" extra={<Button onClick={() => setSimulation('')}>关闭</Button>}><pre className="detail">{simulation}</pre></Card>}</>;
}
