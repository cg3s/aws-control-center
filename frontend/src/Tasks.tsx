import {useState} from 'react';
import {Alert, Button, Card, Checkbox, Descriptions, Form, Input, InputNumber, Select, Space, Table, Tag} from 'antd';
import {api, post, qs, Filters, Target, Task, Configuration} from './api';
import {attempt, ErrorBox, useRemote, useTaskSubmission} from './shared';

type Catalog = {bundles?: {id: string; name: string; price_usd_per_month: number}[]; blueprints?: {id: string; name: string}[]; zones?: string[];
  subnets?: {SubnetId: string; VpcId: string; AvailabilityZone: string}[]; security_groups?: {GroupId: string; GroupName: string; VpcId: string}[];
  instance_types?: {InstanceType: string}[]; images?: {ImageId: string; Name: string}[]; key_pairs?: {KeyName: string}[]; launch_templates?: {LaunchTemplateId: string; LaunchTemplateName: string}[]};
type Values = {names: string; launch: object; tags_json?: string; [key: string]: unknown};
export function Create({filters, refresh, epoch}: {filters: Filters; refresh: () => void; epoch: number}) {
  const [service, setService] = useState('lightsail'), [mode, setMode] = useState('form'), [catalog, setCatalog] = useState<Catalog>(), [error, setError] = useState(''), [loading, setLoading] = useState(false);
  const [form] = Form.useForm();
  const templates = useRemote<{items: Configuration[]}>('/configurations?kind=bootstrap', epoch);
  const jobs = useTaskSubmission(() => {form.resetFields([['launch', 'root_password']]); refresh();});
  const specific = filters.region !== 'all';
  const subnet = Form.useWatch('subnet_id', form), vpc = catalog?.subnets?.find(x => x.SubnetId === subnet)?.VpcId;
  const load = async () => {
    setError(''); setLoading(true); setCatalog(undefined);
    try {setCatalog(await api<Catalog>('/catalog?' + qs({profile: filters.profile, region: filters.region, service})));}
    catch (e) {setError(String(e));} finally {setLoading(false);}
  };
  return <Card title="创建实例与 DIY 初始化"><Alert type="info" message="选择具体区域后加载选项。启动模板版本必须固定；root 配置适用于 Linux。"/><Space className="section"><Select value={service} onChange={s => {setService(s); setCatalog(undefined); form.resetFields();}} options={['lightsail', 'ec2'].map(value => ({value, label: value}))}/><Button disabled={!specific} loading={loading} onClick={() => void load()}>加载区域选项</Button></Space><ErrorBox error={error}/>
    <Form form={form} layout="vertical" className="section" initialValues={{disk_gib: 20, public_ip: false, launch: {root_login: 'disabled', password_auth: false}}}
      onFinish={(values: Values) => void attempt(async () => {
        const {names, tags_json, ...options} = values;
        const tags = tags_json ? JSON.parse(tags_json) as Record<string, string> : {};
        const targets = names.split(/[\s,，]+/).filter(Boolean).map(instance => ({profile: filters.profile, region: filters.region, service, instance} as Target));
        if (targets.length > 100) throw new Error('每批最多 100 台');
        await jobs.submit(targets.map(target => ({kind: 'create', target, options: {...options, tags}})));
      })}>
      <Form.Item name="names" label="实例名称（逗号或换行分隔，最多 100 台）" rules={[{required: true}]}><Input.TextArea rows={2}/></Form.Item>
      {service === 'lightsail' ? <><Form.Item name="bundle_id" label="套餐" rules={[{required: true}]}><Select options={catalog?.bundles?.map(x => ({value: x.id, label: `${x.name} · $${x.price_usd_per_month}/月`}))}/></Form.Item><Form.Item name="blueprint_id" label="Linux 镜像" rules={[{required: true}]}><Select showSearch optionFilterProp="label" options={catalog?.blueprints?.map(x => ({value: x.id, label: x.name + ' · ' + x.id}))}/></Form.Item><Form.Item name="availability_zone" label="可用区"><Select allowClear options={catalog?.zones?.map(value => ({value, label: value}))}/></Form.Item></> : <>
        <Form.Item label="创建模式"><Select value={mode} onChange={v => {setMode(v); form.resetFields(['launch_template_id', 'launch_template_version', 'image_id', 'instance_type', 'subnet_id', 'security_group_ids']);}} options={[{value: 'form', label: '参数表单'}, {value: 'template', label: 'AWS Launch Template'}]}/></Form.Item>
        {mode === 'template' ? <><Form.Item name="launch_template_id" label="启动模板" rules={[{required: true}]}><Select options={catalog?.launch_templates?.map(x => ({value: x.LaunchTemplateId, label: x.LaunchTemplateName}))}/></Form.Item><Form.Item name="launch_template_version" label="固定版本号" rules={[{required: true}]}><Input placeholder="1"/></Form.Item></> : <>
          <Form.Item name="image_id" label="Linux AMI ID（可输入公开 AMI）" rules={[{required: true}]}><Input placeholder="ami-..." list="owned-amis"/></Form.Item><datalist id="owned-amis">{catalog?.images?.map(x => <option key={x.ImageId} value={x.ImageId}>{x.Name}</option>)}</datalist>
          <Form.Item name="instance_type" label="实例规格" rules={[{required: true}]}><Select showSearch options={catalog?.instance_types?.map(x => ({value: x.InstanceType, label: x.InstanceType}))}/></Form.Item>
          <Form.Item name="subnet_id" label="现有子网 / VPC" rules={[{required: true}]}><Select onChange={() => form.resetFields(['security_group_ids'])} options={catalog?.subnets?.map(x => ({value: x.SubnetId, label: `${x.SubnetId} · ${x.VpcId} · ${x.AvailabilityZone}`}))}/></Form.Item>
          <Form.Item name="security_group_ids" label="安全组" rules={[{required: true}]}><Select mode="multiple" options={catalog?.security_groups?.filter(x => x.VpcId === vpc).map(x => ({value: x.GroupId, label: `${x.GroupName} · ${x.GroupId}`}))}/></Form.Item>
          <Form.Item name="disk_gib" label="根磁盘 GiB"><InputNumber min={8} max={16384}/></Form.Item><Form.Item name="instance_profile_arn" label="实例 IAM Profile ARN（可选）"><Input/></Form.Item>
        </>}
        {mode==='form'?<Form.Item name="public_ip" valuePropName="checked"><Checkbox>分配公网 IPv4</Checkbox></Form.Item>:<Alert type="info" message="网络与磁盘沿用固定模板版本；下方初始化配置替换模板 UserData。"/>}
      </>}
      <Form.Item name="tags_json" label="实例标签 JSON（可选，也可用于自动化规则匹配）"><Input.TextArea placeholder={'{"environment":"dev"}'}/></Form.Item><Form.Item name="key_pair_name" label="AWS 区域密钥对名称（可选）"><Input/></Form.Item>
      <Form.Item name="bootstrap_id" label="初始化模板（选择后可继续修改下方设置）"><Select allowClear onChange={id=>{const template=templates.data?.items.find(x=>x.id===id);if(template)form.setFieldsValue({launch:{...template.payload,root_password:form.getFieldValue(['launch','root_password'])}});}} options={templates.data?.items.map(x => ({value: x.id, label: `${x.name} v${x.version}`}))}/></Form.Item>
      <Form.Item name={['launch', 'username']} label="额外普通用户（可选）"><Input/></Form.Item><Form.Item name={['launch', 'ssh_public_key']} label="SSH 公钥，每行一条"><Input.TextArea rows={3}/></Form.Item>
      <Form.Item name={['launch', 'root_password']} label="root 初始密码（至少 16 位，可选）" rules={[{min: 16}]}><Input.Password autoComplete="new-password"/></Form.Item>
      <Form.Item name={['launch', 'root_login']} label="root SSH 登录"><Select options={[{value: 'disabled', label: '禁止'}, {value: 'key', label: '仅密钥'}, {value: 'password', label: '允许密码'}]}/></Form.Item>
      <Form.Item name={['launch', 'password_auth']} valuePropName="checked"><Checkbox>启用 SSH 密码认证</Checkbox></Form.Item><Form.Item name={['launch', 'user_data']} label="首次启动 Bash 脚本"><Input.TextArea rows={7} placeholder={'#!/bin/bash\nset -e\n'}/></Form.Item>
      <Button type="primary" htmlType="submit" disabled={!specific || !catalog} loading={jobs.busy}>预览目标与配置</Button>
    </Form>{jobs.modal}</Card>;
}

export function Tasks({profile, epoch, refresh}: {profile: string; epoch: number; refresh: () => void}) {
  const {data, error, loading} = useRemote<{items: Task[]}>('/tasks?'+qs({profile}), epoch);
  const [output, setOutput] = useState('');
  return <><Card title="任务中心"><ErrorBox error={error}/><Table loading={loading} rowKey="id" pagination={{pageSize: 15}} dataSource={data?.items} columns={[
    {title: '任务', dataIndex: 'id'}, {title: '状态', dataIndex: 'status', render: v => <Tag>{v}</Tag>}, {title: '来源', dataIndex: 'actor'}, {title: '创建时间', dataIndex: 'created_at'},
    {title: '操作', render: (_, x) => <Space><Button onClick={() => void attempt(async () => {await post('/tasks/'+x.id+'/verify?'+qs({profile})); refresh();})}>核验</Button><Button danger onClick={() => void attempt(async () => {await post('/tasks/'+x.id+'/cancel?'+qs({profile})); refresh();})}>取消未提交项</Button></Space>},
  ]} expandable={{expandedRowRender: x => <Table rowKey="id" size="small" pagination={false} dataSource={x.items} columns={[
    {title: '操作', dataIndex: 'kind'}, {title: '目标', render: (_, i) => `${i.target.profile}/${i.target.region}/${i.target.instance}`}, {title: '状态', dataIndex: 'status'}, {title: '错误', dataIndex: 'error'},
    {title: '结果', render: (_, i) => <Descriptions column={1} items={Object.entries(i.result).map(([k, v]) => ({key: k, label: k, children: String(v)}))}/>},
    {title: '输出', render: (_, i) => i.result.output_available ? <Button onClick={() => void attempt(async () => setOutput(JSON.stringify(await api('/task-items/'+i.id+'/output?'+qs({profile})), null, 2)))}>查看输出</Button> : null},
  ]}/>}}/></Card>{output && <Card title="脚本输出（最多各 32 KiB，可能包含脚本输出的秘密）" className="section" extra={<Button onClick={() => setOutput('')}>关闭</Button>}><pre className="detail">{output}</pre></Card>}</>;
}
