import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Alert, Button, Card, ConfigProvider, Input, Layout, Menu, Select, Space, Statistic, Table, theme} from 'antd';
import type {MenuProps} from 'antd';
import {api, post, qs, setCsrf, Filters, regionLabel} from './api';
import {useRemote, ErrorBox, attempt} from './shared';
import {Resources, TrafficCenter} from './Resources';
import {Create, Tasks} from './Tasks';
import {Configurations} from './Configurations';
import {Accounts, Cost, Network, Audit, Events} from './Operations';
import './style.css';
const {Header, Sider, Content} = Layout;

const menuItems: MenuProps['items'] = [
  {key: 'overview', label: '总览'},
  {
    type: 'group', key: 'resources-group', label: '资源管理', children: [
      {key: 'resources', label: '实例管理'},
      {key: 'create', label: '创建与初始化'},
      {key: 'network', label: '网络与 IP'},
      {key: 'accounts', label: 'AWS 账户'},
    ],
  },
  {
    type: 'group', key: 'monitoring-group', label: '监控与自动化', children: [
      {key: 'traffic', label: '流量中心'},
      {key: 'rule', label: '规则配置'},
      {key: 'events', label: '触发与通知'},
    ],
  },
  {
    type: 'group', key: 'tools-group', label: '工具与模板', children: [
      {key: 'bootstrap', label: '初始化模板'},
      {key: 'script', label: '脚本库'},
      {key: 'connection', label: 'SSH 连接'},
    ],
  },
  {
    type: 'group', key: 'operations-group', label: '运维管理', children: [
      {key: 'tasks', label: '任务中心'},
      {key: 'cost', label: '成本分析'},
      {key: 'audit', label: '操作审计'},
    ],
  },
];

type Config = {profiles:string[];regions:string[];active_profile:string|null};
type Summary = {total:number;running:number;stopped:number;errors:unknown[];automation_paused:boolean;task_counts:Record<string,number>;workers:{name:string;seen_at:string;detail:string}[]};
function Overview({filters, epoch}: {filters: Filters; epoch: number}) {
 const {profile, region, service} = filters;
 const {data,error} = useRemote<Summary>('/dashboard/summary?' + qs({profiles: profile, region, service}), epoch);
 return <><ErrorBox error={error}/><div className="stats">{[['实例总数',data?.total],['运行中',data?.running],['已停止',data?.stopped],['待处理',data?.task_counts.queued]].map(([title,value]) => <Card key={String(title)}><Statistic title={String(title)} value={value ?? '—'}/></Card>)}</div>
 <Card title="后台服务状态" className="section"><Space><Button danger onClick={() => void attempt(async () => {await post('/automation/pause?paused=true'); window.dispatchEvent(new Event('refresh-data'));})}>全局暂停自动化</Button><Button onClick={() => void attempt(async () => {await post('/automation/pause?paused=false'); window.dispatchEvent(new Event('refresh-data'));})}>全局恢复自动化</Button><span>{data?.automation_paused?'自动化已暂停':'自动化正常'}</span></Space><Table rowKey="name" dataSource={data?.workers} columns={[{title:'服务',dataIndex:'name'}, {title:'最近心跳 UTC',dataIndex:'seen_at'}, {title:'状态',render:(_,x) => Date.now()-Date.parse(x.seen_at)>900000?'心跳过期':'在线'}]}/></Card>{!!data?.errors.length && <Alert type="warning" message="清单有部分错误或尚未采集" description={JSON.stringify(data.errors)}/>}</>;
}
function App() {
 const [config,setConfig] = useState<Config>(), [password,setPassword] = useState(''), [error,setError] = useState(''), [loading,setLoading] = useState(false);
 const [tab,setTab] = useState('overview'), [epoch,setEpoch] = useState(0);
 const [region, setRegion] = useState('all'), [service, setService] = useState('all');
 const refresh = () => setEpoch(x => x+1);
 useEffect(() => {const expired=()=>{setCsrf('');setConfig(undefined);setPassword('');};window.addEventListener('session-expired',expired);window.addEventListener('refresh-data',refresh);return()=>{window.removeEventListener('session-expired',expired);window.removeEventListener('refresh-data',refresh);};},[]);
 useEffect(() => {let active=true;void api<{csrf:string}>('/session').then(s=>{setCsrf(s.csrf);return api<Config>('/config');}).then(c=>{if(active)setConfig(c);}).catch(()=>{});return()=>{active=false;};},[]);
 useEffect(() => {if(!config)return;const id=window.setInterval(()=>{if(!document.hidden)refresh();},15000);return()=>clearInterval(id);},[!!config]);
 const accountRefresh = async () => {
   setConfig(await api<Config>('/config'));
   refresh();
 };
 const login=async()=>{setLoading(true);setError('');try{const s=await post<{csrf:string}>('/login',{password});setCsrf(s.csrf);setPassword('');setConfig(await api<Config>('/config'));}catch(e){setError(String(e));}finally{setLoading(false);}};
 if(!config)return <ConfigProvider theme={{algorithm:theme.darkAlgorithm}}><div className="login"><Card title="AWS Control Center"><Space direction="vertical" style={{width:'100%'}}><Input.Password aria-label="管理员密码" value={password} onChange={e=>setPassword(e.target.value)} onPressEnter={()=>void login()}/><ErrorBox error={error}/><Button type="primary" loading={loading} onClick={()=>void login()}>登录</Button></Space></Card></div></ConfigProvider>;
 const profile = config.active_profile;
 const filters: Filters | null = profile ? {profile, region, service} : null;
 const hasRegionFilter = ['overview', 'resources', 'traffic', 'create', 'network'].includes(tab);
 const hasServiceFilter = ['overview', 'resources', 'traffic'].includes(tab);
 return <ConfigProvider theme={{algorithm: theme.darkAlgorithm, token: {colorPrimary: '#6389ff'}}}>
   <Layout className="app">
     <Sider breakpoint="lg" collapsedWidth="0" width={230}>
       <div className="logo">☁ AWS Control Center</div>
       <Menu theme="dark" mode="inline" aria-label="控制台菜单" selectedKeys={[tab]} onClick={e => setTab(e.key)} items={menuItems}/>
     </Sider>
     <Layout>
       <Header className="topbar">
         <Space wrap><strong>多账户管理控制台</strong><span>{profile ? `当前账户 ${profile}` : '未激活账户'}</span></Space>
         <Space>
           <Button onClick={() => void attempt(accountRefresh)}>刷新</Button>
           <Button onClick={() => void attempt(async () => {await post('/logout');setCsrf('');setConfig(undefined);})}>退出</Button>
         </Space>
       </Header>
       <Content className="content">
         {tab === 'accounts' ? <Accounts profiles={config.profiles} active={profile} epoch={epoch} refresh={accountRefresh}/>
           : !filters ? <Alert type="info" showIcon message="请先激活 AWS 账户"
               action={<Button onClick={() => setTab('accounts')}>前往账户管理</Button>}/>
           : <React.Fragment key={filters.profile}>
               {hasRegionFilter && <Space wrap className="filters">
                 <Select aria-label="区域" style={{minWidth: 230}} value={region} onChange={setRegion}
                   options={[{value: 'all', label: '全部区域'}, ...config.regions.map(value => ({value, label: regionLabel(value)}))]}/>
                 {hasServiceFilter && <Select aria-label="服务" style={{minWidth: 140}} value={service} onChange={setService}
                   options={['all', 'ec2', 'lightsail'].map(value => ({value, label: value === 'all' ? '全部服务' : value}))}/>}
               </Space>}
               {tab === 'overview' && <Overview filters={filters} epoch={epoch}/>}
               {tab === 'traffic' && <TrafficCenter filters={filters} epoch={epoch}/>}
               {tab === 'resources' && <Resources filters={filters} epoch={epoch} refresh={refresh}/>}
               {tab === 'create' && <Create key={region} filters={filters} epoch={epoch} refresh={refresh}/>}
               {tab === 'tasks' && <Tasks profile={filters.profile} epoch={epoch} refresh={refresh}/>}
               {['rule', 'bootstrap', 'script', 'connection'].includes(tab) &&
                 <Configurations key={tab} kind={tab} profile={filters.profile} epoch={epoch} refresh={refresh}/>}
               {tab === 'network' && <Network key={region} filters={filters} epoch={epoch} refresh={refresh}/>}
               {tab === 'cost' && <Cost profile={filters.profile} epoch={epoch}/>}
               {tab === 'audit' && <Audit profile={filters.profile} epoch={epoch}/>}
               {tab === 'events' && <Events profile={filters.profile} epoch={epoch}/>}
             </React.Fragment>}
       </Content>
     </Layout>
   </Layout>
 </ConfigProvider>;
}
createRoot(document.getElementById('root')!).render(<React.StrictMode><App/></React.StrictMode>);
