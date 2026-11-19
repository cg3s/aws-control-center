import {Button, Form, Input, InputNumber, Select, Space, Typography} from 'antd';

export function FirewallFields({name}: {name: string | string[]}) {
  return <Form.List name={name}>
    {(fields, {add, remove}) => <>
      <Typography.Title level={5}>AWS 入站防火墙</Typography.Title>
      <Typography.Paragraph type="secondary">
        有规则时将替换新实例的默认入站规则。需要 SSH 时，请添加 TCP 22 和允许登录的来源地址。
      </Typography.Paragraph>
      {fields.map(({key, name: index}) => <Space key={key} wrap align="start">
        <Form.Item name={[index, 'protocol']} label="协议" rules={[{required: true}]}>
          <Select style={{width: 85}} options={[{value: 'tcp', label: 'TCP'}, {value: 'udp', label: 'UDP'}]}/>
        </Form.Item>
        <Form.Item name={[index, 'from_port']} label="起始端口" rules={[{required: true}]}>
          <InputNumber min={1} max={65535} precision={0}/>
        </Form.Item>
        <Form.Item name={[index, 'to_port']} label="结束端口" rules={[{required: true}]}>
          <InputNumber min={1} max={65535} precision={0}/>
        </Form.Item>
        <Form.Item name={[index, 'cidr']} label="来源地址或 CIDR" rules={[{required: true}]}>
          <Input placeholder="203.0.113.5/32"/>
        </Form.Item>
        <Form.Item label=" "><Button danger onClick={() => remove(index)}>移除</Button></Form.Item>
      </Space>)}
      <Form.Item><Button onClick={() => add({protocol: 'tcp', from_port: 22, to_port: 22})}
        disabled={fields.length >= 50}>添加入站规则</Button></Form.Item>
    </>}
  </Form.List>;
}
