from datetime import datetime, timedelta, timezone
from unittest.mock import call

import pytest
from botocore.exceptions import ClientError

from controlplane import resources
from test_tasks import enqueue, ready, request


OLD = '203.0.113.10'
NEW = '203.0.113.11'
TEMPORARY = '203.0.113.12'


def ec2_instance(aws):
    instance = aws.describe_instances.return_value['Reservations'][0]['Instances'][0]
    instance['NetworkInterfaces'] = [{'NetworkInterfaceId': 'eni-primary', 'Attachment': {'DeviceIndex': 0}}]
    network = {'Association': {'PublicIp': OLD}, 'Attachment': {'InstanceId': instance['InstanceId'], 'DeviceIndex': 0}}
    aws.describe_network_interfaces.return_value = {'NetworkInterfaces': [network]}
    return instance, network


def test_ec2_rotation_uses_dynamic_assignment_and_updates_inventory(control, monkeypatch):
    web, client, headers, aws = control
    instance, network = ec2_instance(aws)

    def assign(NetworkInterfaceId, AssociatePublicIpAddress):
        assert NetworkInterfaceId == 'eni-primary'
        network['Association'] = {'PublicIp': NEW} if AssociatePublicIpAddress else {}

    aws.modify_network_interface_attribute.side_effect = assign
    item = {'profile': 'default', 'region': 'us-east-1', 'service': 'ec2', 'name': 'node',
            'id': instance['InstanceId'], 'state': 'running', 'ip': OLD}
    monkeypatch.setattr(web, 'instances', lambda **kwargs: {'items': [item], 'errors': []})
    resources.refresh_profile(web, 'default')

    job = request('rotate_ip', service='ec2')
    preview = client.post('/api/tasks/preview', headers=headers, json=job.model_dump()).json()
    assert preview['items'][0]['before']['public'] == OLD
    job.preview_id, job.confirm = preview['preview_id'], preview['confirmation']
    task = client.post('/api/tasks', headers=headers, json=job.model_dump()).json()
    aws.modify_network_interface_attribute.assert_not_called()

    for _ in range(4):
        ready(web, task['id'])
        assert web.control_jobs.run_once()

    result = web.control_jobs.detail(task['id'])['items'][0]
    assert result['status'] == 'succeeded'
    assert (result['result']['old_ip'], result['result']['new_ip']) == (OLD, NEW)
    assert aws.modify_network_interface_attribute.call_args_list == [
        call(NetworkInterfaceId='eni-primary', AssociatePublicIpAddress=False),
        call(NetworkInterfaceId='eni-primary', AssociatePublicIpAddress=True),
    ]
    aws.allocate_address.assert_not_called()
    assert client.get('/api/resources?profiles=default').json()['items'][0]['ip'] == NEW
    item['ip'] = '203.0.113.13'
    resources.refresh_profile(web, 'default')
    assert client.get('/api/resources?profiles=default').json()['items'][0]['ip'] == item['ip']


def lightsail_instance(aws, *, attach_error='', returned_ip=NEW):
    instance = aws.get_instance.return_value['instance']
    instance.update(publicIpAddress=OLD, isStaticIp=False)
    addresses = {}
    operations = {}

    def static_ip(staticIpName):
        if staticIpName not in addresses:
            raise ClientError({'Error': {'Code': 'NotFoundException'}}, 'GetStaticIp')
        return {'staticIp': addresses[staticIpName].copy()}

    def allocate(staticIpName):
        addresses[staticIpName] = {'name': staticIpName, 'ipAddress': TEMPORARY, 'arn': 'arn:static-ip', 'isAttached': False}
        return operation('allocate')

    def attach(staticIpName, instanceName):
        if attach_error == 'denied':
            raise ClientError({'Error': {'Code': 'AccessDeniedException'}, 'ResponseMetadata': {'HTTPStatusCode': 403}}, 'AttachStaticIp')
        addresses[staticIpName].update(isAttached=True, attachedTo=instanceName)
        instance.update(publicIpAddress=TEMPORARY, isStaticIp=True)
        if attach_error == 'timeout':
            raise TimeoutError
        return operation('attach')

    def detach(staticIpName):
        addresses[staticIpName].update(isAttached=False, attachedTo='')
        instance.update(publicIpAddress=returned_ip, isStaticIp=False)
        return operation('detach')

    def release(staticIpName):
        assert addresses[staticIpName]['isAttached'] is False
        del addresses[staticIpName]
        return operation('release')

    def operation(name):
        operations[name] = {'id': name, 'status': 'Succeeded', 'isTerminal': True}
        return {'operations': [{'id': name}]}

    aws.get_static_ip.side_effect = static_ip
    aws.allocate_static_ip.side_effect = allocate
    aws.attach_static_ip.side_effect = attach
    aws.detach_static_ip.side_effect = detach
    aws.release_static_ip.side_effect = release
    aws.get_operation.side_effect = lambda operationId: {'operation': operations[operationId]}
    return addresses


def drive_rotation(web, identifier):
    for _ in range(10):
        item = web.control_jobs.detail(identifier)['items'][0]
        if item['status'] in ('succeeded', 'failed', 'unknown'):
            return item
        ready(web, identifier)
        assert web.control_jobs.run_once()
    pytest.fail('Rotation did not reach a terminal state')


@pytest.mark.parametrize('attach_error', ['', 'timeout', 'denied'])
def test_lightsail_rotation_waits_for_cleanup_and_recovers_ambiguous_attach(control, attach_error):
    web, _, _, aws = control
    addresses = lightsail_instance(aws, attach_error=attach_error)
    job = enqueue(web, request('rotate_ip'))
    result = drive_rotation(web, job['id'])
    if attach_error == 'timeout':
        assert result['status'] == 'unknown' and len(addresses) == 1
        assert aws.get_instance.return_value['instance']['isStaticIp'] is True
        web.control_jobs.verify(job['id'], 'tester')
        result = drive_rotation(web, job['id'])
    assert not addresses
    static_name = 'awscc-rotate-' + result['id']
    aws.allocate_static_ip.assert_called_once_with(staticIpName=static_name)
    aws.attach_static_ip.assert_called_once_with(staticIpName=static_name, instanceName='node')
    aws.release_static_ip.assert_called_once_with(staticIpName=static_name)
    if attach_error == 'denied':
        assert result['status'] == 'failed' and 'AccessDeniedException' in result['error']
        aws.detach_static_ip.assert_not_called()
    else:
        assert result['status'] == 'succeeded'
        assert (result['result']['old_ip'], result['result']['new_ip']) == (OLD, NEW)
        aws.detach_static_ip.assert_called_once_with(staticIpName=static_name)
    aws.stop_instance.assert_not_called()


def test_lightsail_unchanged_ip_is_never_reported_as_rotated(control):
    web, _, _, aws = control
    addresses = lightsail_instance(aws, returned_ip=OLD)
    job = enqueue(web, request('rotate_ip'))
    for _ in range(6):
        ready(web, job['id'])
        assert web.control_jobs.run_once()
    attempted = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    with web.db() as c:
        c.execute('UPDATE v2_items SET attempted_at=? WHERE job_id=?', (attempted, job['id']))
    result = drive_rotation(web, job['id'])
    assert not addresses and result['status'] == 'unknown' and 'new_ip' not in result['result']


def test_lightsail_batch_uses_one_temporary_ip_per_account_region(control):
    web, _, _, aws = control
    addresses = lightsail_instance(aws)
    instance = aws.get_instance.return_value['instance']
    aws.get_instance.side_effect = lambda instanceName: {'instance': instance | {'name': instanceName, 'arn': 'arn:' + instanceName}}
    job = request('rotate_ip')
    second = job.items[0].model_copy(deep=True)
    second.target.instance = 'second-node'
    job.items.append(second)
    task = enqueue(web, job)

    assert web.control_jobs.run_once()
    assert not web.control_jobs.run_once()
    ready(web, task['id'])
    assert web.control_jobs.run_once()
    assert len(addresses) == 1
    assert not web.control_jobs.run_once()
    assert [item['status'] for item in web.control_jobs.detail(task['id'])['items']] == ['verifying', 'queued']


@pytest.mark.parametrize('service', ['ec2', 'lightsail'])
def test_rotation_rejects_existing_static_ips_at_preview(control, service):
    _, client, headers, aws = control
    if service == 'ec2':
        _, network = ec2_instance(aws)
        network['Association']['AllocationId'] = 'eipalloc-existing'
    else:
        lightsail_instance(aws)
        aws.get_instance.return_value['instance']['isStaticIp'] = True
    response = client.post('/api/tasks/preview', headers=headers, json=request('rotate_ip', service=service).model_dump())
    assert response.status_code == 409
    aws.allocate_static_ip.assert_not_called()
    aws.modify_network_interface_attribute.assert_not_called()
