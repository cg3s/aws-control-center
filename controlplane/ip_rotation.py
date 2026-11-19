"""Replace dynamic public IPv4 addresses without restarting instances."""
import time
from datetime import datetime, timezone
from ipaddress import IPv4Address

from botocore.exceptions import ClientError
from fastapi import HTTPException

from . import resources


def dynamic_ipv4(current):
    if current.get('is_static') is not False:
        return False
    try:
        IPv4Address(current.get('public', ''))
    except ValueError:
        return False
    return True


def validate(target, current):
    if current['state'] != 'running' or not dynamic_ipv4(current):
        raise HTTPException(409, f"{target['instance']} 必须正在运行并使用动态公网 IPv4 才能换址。")


def start(row, payload, current, result):
    target, before = payload['target'], payload['before']
    validate(target, current)
    if current['public'] != before['public'] or current.get('interface_id') != before.get('interface_id'):
        raise HTTPException(409, f"{target['instance']} 的网络配置已变化，请重新预览。")
    result['old_ip'] = current['public']
    if target['service'] == 'lightsail':
        result.update(rotation_stage='allocate', temporary_static_ip='awscc-rotate-' + row['id'])
    else:
        result['rotation_stage'] = 'unassign'


def advance(web, row, payload, result, send):
    target = payload['target']
    if 'rotation_stage' not in result:
        return 'unknown'
    if target['service'] == 'ec2':
        status = advance_ec2(web, target, payload['before']['interface_id'], result, send)
    else:
        status = advance_lightsail(web, target, result, send)
    if status == 'verifying' and time.time() - datetime.fromisoformat(row['attempted_at']).timestamp() > 900:
        result['note'] = '等待 AWS 确认超时，请检查任务结果并核验。'
        return 'unknown'
    return status


def verified(current, result):
    if current['state'] != 'running' or not dynamic_ipv4(current) or current['public'] in (result['old_ip'], result.get('temporary_ipv4')):
        return 'verifying'
    result.update(new_ip=current['public'], verified_at=datetime.now(timezone.utc).isoformat(), rotation_stage='complete')
    result.pop('note', None)
    return 'succeeded'


def advance_ec2(web, target, interface_id, result, send):
    current = resources.observe(web, target, network=True)
    if current['generation'] != target['generation'] or current.get('interface_id') != interface_id:
        raise HTTPException(409, 'EC2 主网卡已变化，请检查实例的公网 IPv4。')
    if current['is_static']:
        raise HTTPException(409, 'EC2 已绑定静态公网地址，请检查实例网络配置。')
    stage = result['rotation_stage']
    if stage == 'verify' or (stage == 'assign' and current['public'] not in ('', result['old_ip'])):
        return verified(current, result)
    if stage == 'unassign' and current['public']:
        validate(target, current)
        if current['public'] != result['old_ip']:
            raise HTTPException(409, 'EC2 公网 IPv4 在更换前已变化。')
        send('modify_network_interface_attribute', NetworkInterfaceId=interface_id, AssociatePublicIpAddress=False)
        result.update(rotation_stage='assign', unassigned_at=time.time())
    elif stage == 'unassign' or not current['public'] or time.time() - result['unassigned_at'] > 120:
        # Restore auto-assignment even if AWS has not shown the removal after two minutes.
        send('modify_network_interface_attribute', NetworkInterfaceId=interface_id, AssociatePublicIpAddress=True)
        result['rotation_stage'] = 'verify'
        result.pop('unassigned_at', None)
    return 'verifying'


def lightsail_static_ip(api, name):
    try:
        return api.get_static_ip(staticIpName=name)['staticIp']
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'NotFoundException' and exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode') != 404:
            raise
        return None


def send_lightsail(send, result, action, next_stage, **kwargs):
    operations = send(action + '_static_ip', staticIpName=result['temporary_static_ip'], **kwargs).get('operations', [])
    result['rotation_stage'] = next_stage
    result['pending_operations'] = [operation['id'] for operation in operations]


def lightsail_operations(api, result):
    operations = [api.get_operation(operationId=op)['operation'] for op in result.get('pending_operations', [])]
    if any(not op.get('isTerminal') and op.get('status') not in ('Completed', 'Succeeded', 'Failed') for op in operations):
        return False
    result.pop('pending_operations', None)
    failed = next((op for op in operations if op.get('status') == 'Failed'), None)
    if failed:
        stage = result['rotation_stage']
        error = 'Lightsail AWS 操作失败：' + (failed.get('errorCode') or failed.get('operationType') or '未知错误')
        if stage in ('attach', 'detach'):
            result.update(rotation_stage='cleanup', rotation_error=error)
        else:
            result['rotation_stage'] = {'release': 'detach', 'verify': 'release', 'cleanup': 'cleanup'}[stage]
            raise HTTPException(409, error)
    return True


def advance_lightsail(web, target, result, send):
    api = web.client(target['profile'], 'lightsail', target['region'])
    if not lightsail_operations(api, result):
        return 'verifying'
    stage = result['rotation_stage']
    static_ip = lightsail_static_ip(api, result['temporary_static_ip'])
    attached = static_ip and (static_ip.get('isAttached') or static_ip.get('attachedTo'))
    if static_ip:
        arn = static_ip['arn']
        if result.setdefault('temporary_static_ip_arn', arn) != arn or (attached and static_ip.get('attachedTo') != target['instance']):
            raise HTTPException(409, 'Lightsail 临时地址已变化或绑定到其他实例，请检查后再核验。')
        result['temporary_ipv4'] = static_ip['ipAddress']

    if stage == 'allocate':
        if static_ip is None:
            current = observe_target(web, target)
            if not current or not dynamic_ipv4(current) or current['public'] != result['old_ip'] or current['state'] != 'running':
                result['rotation_error'] = 'Lightsail 公网 IPv4 或实例状态在申请前已变化。'
                return 'failed'
            send_lightsail(send, result, 'allocate', 'attach')
        else:
            result['rotation_stage'] = 'attach'
    elif stage == 'attach' and static_ip:
        current = observe_target(web, target)
        if attached:
            result['rotation_stage'] = 'detach'
        elif not current or not dynamic_ipv4(current) or current['public'] != result['old_ip'] or current['state'] != 'running':
            result.update(rotation_stage='cleanup', rotation_error='Lightsail 公网 IPv4 或实例状态在绑定前已变化。')
        else:
            try:
                send_lightsail(send, result, 'attach', 'detach', instanceName=target['instance'])
            except ClientError as exc:
                http_status = exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0)
                if http_status not in (400, 403, 404) or exc.response['Error']['Code'].startswith('RequestTimeout'):
                    raise
                result.update(rotation_stage='cleanup', rotation_error=web.aws_error(exc))
    elif stage == 'detach':
        if attached:
            current = observe_target(web, target)
            if current is None:
                raise HTTPException(409, 'Lightsail 实例已被替换，请检查临时静态 IPv4 的绑定。')
            if current['public'] == static_ip['ipAddress'] and current['is_static'] is True:
                if result.get('temporary_attachment_seen'):
                    send_lightsail(send, result, 'detach', 'release')
                else:
                    # Persist the observed attachment for recovery if the detach response is lost.
                    result['temporary_attachment_seen'] = True
        elif result.get('temporary_attachment_seen'):
            result['rotation_stage'] = 'release'
    elif stage in ('release', 'cleanup'):
        if static_ip is None:
            if stage == 'cleanup':
                return 'failed'
            result['rotation_stage'] = 'verify'
        elif not attached:
            send_lightsail(send, result, 'release', 'cleanup' if stage == 'cleanup' else 'verify')
        elif stage == 'cleanup' and observe_target(web, target):
            send_lightsail(send, result, 'detach', 'cleanup')
    elif stage == 'verify' and static_ip is None:
        current = observe_target(web, target)
        if current:
            return verified(current, result)
    return 'verifying'


def observe_target(web, target):
    try:
        current = resources.observe(web, target, network=True)
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'NotFoundException':
            return None
        raise
    return current if current['generation'] == target['generation'] else None
