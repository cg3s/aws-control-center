import {useEffect, useState, useRef} from 'react';
import {Alert, Button, Input, Modal, Space, Table, Typography, message} from 'antd';
import {api, post, JobItem, Task} from './api';

export async function attempt(work: () => Promise<unknown>) {
  try { await work(); } catch (e) { message.error(String(e)); }
}
export function useRemote<T>(path: string, epoch = 0) {
  const [data, setData] = useState<T>(), [error, setError] = useState(''), [loading, setLoading] = useState(false);
  const previousPath = useRef(path);
  useEffect(() => {
    let active = true; setLoading(true); setError('');
    if(previousPath.current!==path){setData(undefined);previousPath.current=path;}
    void api<T>(path).then(d => {if (active) setData(d);}).catch(e => {if (active) setError(String(e));}).finally(() => {if (active) setLoading(false);});
    return () => {active = false;};
  }, [path, epoch]);
  return {data, error, loading};
}
export function ErrorBox({error}: {error?: string}) { return error ? <Alert type="error" showIcon message={error} className="section"/> : null; }

type Preview = {preview_id: string; confirmation: string; items: Array<{kind: string; target: {profile: string; region: string; instance: string}; before?: unknown; estimate_usd_month?: number}>; note: string};
export function useTaskSubmission(done: () => void) {
  const [pending, setPending] = useState<{request: {request_key: string; items: JobItem[]}; preview: Preview}>(), [busy, setBusy] = useState(false), [confirm, setConfirm] = useState('');
  const submit = async (items: JobItem[]) => {
    if (!items.length) return;
    setBusy(true);
    try {
      const request = {request_key: crypto.randomUUID(), items};
      const preview = await post<Preview>('/tasks/preview', request);
      setPending({request, preview}); setConfirm('');
    } catch (e) {message.error(String(e));} finally {setBusy(false);}
  };
  const modal = <Modal open={!!pending} title="核对操作目标" width={850} confirmLoading={busy} maskClosable={false}
    onCancel={() => {if (!busy) setPending(undefined);}} okText="提交任务" okButtonProps={{danger: true, disabled: confirm !== pending?.preview.confirmation}}
    onOk={async () => {
      if (!pending || busy) return;
      setBusy(true);
      try {
        const task = await post<Task>('/tasks', {...pending.request, preview_id: pending.preview.preview_id, confirm});
        message.success('任务已入队：' + task.id); setPending(undefined); done();
      } catch (e) {message.error(String(e) + '。结果不明确时请保留此窗口并检查任务中心，重试将复用同一请求标识。');} finally {setBusy(false);}
    }}>
    <Alert type="warning" message="操作将修改 AWS 资源，并可能产生费用。删除不会自动释放独立 IP、磁盘或快照。"/>
    <Table size="small" pagination={{pageSize: 5}} rowKey={(_, i) => String(i)} dataSource={pending?.preview.items} columns={[
      {title: '操作', dataIndex: 'kind'}, {title: '账户', render: (_, x) => x.target.profile}, {title: '区域', render: (_, x) => x.target.region},
      {title: '实例', render: (_, x) => x.target.instance}, {title: '月费估算 USD', render: (_, x) => x.estimate_usd_month ?? '未估算'},
    ]} expandable={{expandedRowRender: x => <pre className="detail">{JSON.stringify(x.before, null, 2)}</pre>}}/>
    <Typography.Paragraph>{pending?.preview.note}</Typography.Paragraph>
    <Space direction="vertical"><Typography.Text>输入 {pending?.preview.confirmation} 确认以上固定目标清单</Typography.Text><Input value={confirm} onChange={e => setConfirm(e.target.value)}/></Space>
  </Modal>;
  return {submit, modal, busy};
}
export function Refresh({onClick}: {onClick: () => void}) {return <Button onClick={onClick}>刷新</Button>;}
