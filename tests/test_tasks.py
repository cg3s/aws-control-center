import json
import time
from unittest.mock import patch
import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from controlplane.models import JobRequest
from controlplane import resources


def request(kind='stop', service='lightsail', **options):
    return JobRequest(request_key='request-key-12345', items=[{'kind': kind,
        'target': {'profile': 'default', 'region': 'us-east-1', 'service': service,
                   'instance': 'node' if service == 'lightsail' else 'i-0123456789abcdef0'}, 'options': options}])


def enqueue(web, data):
    preview = web.control_jobs.preview(data, 'tester')
    data.preview_id, data.confirm = preview['preview_id'], preview['confirmation']
    return web.control_jobs.submit(data, 'tester')


def ready(web, identifier):
    with web.db() as c:
        c.execute('UPDATE v2_items SET lease_until=NULL WHERE job_id=?', (identifier,))


def test_preview_confirmation_idempotency_and_eventual_state(control):
    web, c, h, aws = control
    data = request()
    assert c.post('/api/tasks/preview', json=data.model_dump()).status_code == 403
    preview = c.post('/api/tasks/preview', json=data.model_dump(), headers=h).json()
    data.preview_id = preview['preview_id']
    assert c.post('/api/tasks', json=data.model_dump(), headers=h).status_code == 400
    data.confirm = 'EXECUTE'
    first = c.post('/api/tasks', json=data.model_dump(), headers=h)
    assert first.status_code == 200, first.text
    identifier = first.json()['id']
    assert c.post('/api/tasks', json=data.model_dump(), headers=h).json()['id'] == identifier
    assert web.control_jobs.run_once()
    aws.stop_instance.assert_called_once_with(instanceName='node')
    assert web.control_jobs.detail(identifier)['items'][0]['status'] == 'submitted'
    aws.get_instance.return_value['instance']['state']['name'] = 'stopped'
    ready(web, identifier)
    assert web.control_jobs.run_once()
    assert web.control_jobs.detail(identifier)['status'] == 'succeeded'
    assert aws.stop_instance.call_count == 1


@pytest.mark.parametrize('change', ['items', 'expired', 'actor'])
def test_preview_is_bound_and_expires(control, change):
    web, _, _, _ = control
    data = request()
    preview = web.control_jobs.preview(data, 'tester')
    data.preview_id, data.confirm = preview['preview_id'], 'EXECUTE'
    actor = 'other' if change == 'actor' else 'tester'
    if change == 'items':
        data.items[0].target.instance = 'other'
    if change == 'expired':
        with patch('controlplane.jobs.time.time', return_value=time.time()+1000):
            with pytest.raises(HTTPException): web.control_jobs.submit(data, actor)
        return
    with pytest.raises(HTTPException): web.control_jobs.submit(data, actor)


def test_duplicate_and_oversize_batches_rejected(control):
    web, _, _, _ = control
    data = request()
    data.items.append(data.items[0])
    with pytest.raises(HTTPException): web.control_jobs.preview(data, 'tester')


def test_ambiguous_write_never_retried(control):
    web, _, _, aws = control
    task = enqueue(web, request())
    aws.stop_instance.side_effect = TimeoutError()
    assert web.control_jobs.run_once()
    assert web.control_jobs.detail(task['id'])['status'] == 'unknown'
    assert not web.control_jobs.run_once()
    assert aws.stop_instance.call_count == 1


def test_worker_crash_requires_verification(control):
    web, _, _, aws = control
    task = enqueue(web, request())
    with web.db() as c:
        c.execute("UPDATE v2_items SET status='running',lease_until=0 WHERE job_id=?", (task['id'],))
    assert not web.control_jobs.run_once()
    assert web.control_jobs.detail(task['id'])['status'] == 'unknown'
    aws.stop_instance.assert_not_called()


def test_generation_and_identity_changes_block_mutations(control):
    web, _, _, aws = control
    task = enqueue(web, request())
    aws.get_instance.return_value['instance']['createdAt'] = 'replacement'
    web.control_jobs.run_once()
    assert web.control_jobs.detail(task['id'])['status'] == 'failed'
    aws.stop_instance.assert_not_called()


def test_cancel_only_unsubmitted(control):
    web, _, _, aws = control
    task = enqueue(web, request())
    assert web.control_jobs.cancel(task['id'], 'tester')['status'] == 'canceled'
    assert not web.control_jobs.run_once()
    aws.stop_instance.assert_not_called()


def test_delete_protection_and_confirmation(control):
    web, c, h, aws = control
    aws.describe_instance_attribute.return_value = {'DisableApiTermination': {'Value': True}}
    assert c.post('/api/tasks/preview', json=request('delete', 'ec2').model_dump(), headers=h).status_code == 409
    aws.describe_instance_attribute.return_value = {'DisableApiTermination': {'Value': False}}
    task = enqueue(web, request('delete', 'ec2'))
    web.control_jobs.run_once()
    aws.terminate_instances.assert_called_once_with(InstanceIds=['i-0123456789abcdef0'])
    aws.describe_instances.side_effect = ClientError({'Error': {'Code': 'InvalidInstanceID.NotFound'}}, 'DescribeInstances')
    ready(web, task['id']); web.control_jobs.run_once()
    assert web.control_jobs.detail(task['id'])['status'] == 'succeeded'


def test_lightsail_create_encryption_and_ownership(control, monkeypatch):
    web, _, _, aws = control
    monkeypatch.setattr(resources, 'catalog', lambda *a: {'bundles': [{'id': 'micro', 'price_usd_per_month': 5}],
                  'blueprints': [{'id': 'debian'}], 'zones': ['us-east-1a']})
    aws.get_instance.side_effect = ClientError({'Error': {'Code': 'NotFoundException'}}, 'GetInstance')
    data = request('create', bundle_id='micro', blueprint_id='debian', launch={'root_password': 'very-secret-root-password', 'user_data': 'echo bootstrap-secret'})
    task = enqueue(web, data)
    assert 'very-secret-root-password' not in json.dumps(task)
    assert 'bootstrap-secret' not in json.dumps(task)
    with web.db() as c:
        row = c.execute('SELECT payload FROM v2_items').fetchone()
        assert b'bootstrap-secret' not in row[0]
        assert 'very-secret-root-password' not in web.vault().decrypt(row[0]).decode()
    web.control_jobs.run_once()
    args = aws.create_instances.call_args.kwargs
    assert 'userData' in args and 'very-secret-root-password' not in args['userData']
    assert args['tags'][0]['key'] == 'awscc-task'
    aws.get_instance.side_effect = None
    aws.get_instance.return_value = {'instance': {'name': 'node', 'state': {'name': 'running'}, 'tags': args['tags']}}
    ready(web, task['id']); web.control_jobs.run_once()
    assert web.control_jobs.detail(task['id'])['status'] == 'succeeded'


def test_ec2_create_uses_fixed_client_token(control):
    web, _, _, aws = control
    aws.describe_images.return_value = {'Images': [{'ImageId': 'ami-1', 'State': 'available', 'Architecture': 'x86_64', 'RootDeviceName': '/dev/sda1', 'RootDeviceType': 'ebs'}]}
    aws.describe_instance_types.return_value = {'InstanceTypes': [{'ProcessorInfo': {'SupportedArchitectures': ['x86_64']}}]}
    aws.describe_subnets.return_value = {'Subnets': [{'VpcId': 'vpc-1'}]}
    aws.describe_security_groups.return_value = {'SecurityGroups': [{'VpcId': 'vpc-1'}]}
    aws.run_instances.return_value = {'Instances': [{'InstanceId': 'i-0123456789abcdef0'}]}
    data = request('create', image_id='ami-1', instance_type='t3.micro', subnet_id='subnet-1', security_group_ids=['sg-1'])
    data.items[0].target.service = 'ec2'
    task = enqueue(web, data)
    web.control_jobs.run_once()
    args = aws.run_instances.call_args.kwargs
    assert args['ClientToken'] == task['items'][0]['id']
    assert args['NetworkInterfaces'][0]['Groups'] == ['sg-1']
    assert args['MetadataOptions']['HttpTokens'] == 'required'
    ready(web, task['id']); web.control_jobs.run_once()
    assert web.control_jobs.detail(task['id'])['status'] == 'succeeded'


def test_automation_pause_blocks_queued_mutations(control):
    web, c, h, aws = control
    prepared = web.control_jobs.prepare(request().items[0])
    task = web.control_jobs.enqueue([prepared], 'event-test-key', 'event-test', 'automation')
    assert c.post('/api/automation/pause?paused=true', headers=h).status_code == 200
    assert not web.control_jobs.run_once()
    assert web.control_jobs.detail(task['id'])['status'] == 'queued'
    aws.stop_instance.assert_not_called()
    c.post('/api/automation/pause?paused=false', headers=h)
    web.control_jobs.run_once()
    aws.stop_instance.assert_called_once()


def test_idempotency_conflict_is_enforced_inside_transaction(control):
    web,_,_,_=control
    prepared=web.control_jobs.prepare(request().items[0])
    web.control_jobs.enqueue([prepared],'same-key','original','tester')
    with pytest.raises(HTTPException) as exc:
        web.control_jobs.enqueue([prepared],'same-key','different','tester')
    assert exc.value.status_code==409


def test_concurrent_workers_never_submit_same_item_twice(control):
    from concurrent.futures import ThreadPoolExecutor
    web,_,_,aws=control
    task=enqueue(web,request())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _:web.control_jobs.run_once(),range(4)))
    assert web.control_jobs.detail(task['id'])['items'][0]['status']=='submitted'
    aws.stop_instance.assert_called_once()


def test_aws_server_error_is_ambiguous_not_safe_to_retry(control):
    web,_,_,aws=control
    task=enqueue(web,request())
    aws.stop_instance.side_effect=ClientError({'Error':{'Code':'InternalError'},'ResponseMetadata':{'HTTPStatusCode':500}},'StopInstance')
    web.control_jobs.run_once()
    assert web.control_jobs.detail(task['id'])['status']=='unknown'
    assert not web.control_jobs.run_once()
    aws.stop_instance.assert_called_once()


@pytest.mark.parametrize('code,http,status',[('NotFoundException',400,200),('ServiceException',404,200),('ServiceException',400,502)])
def test_lightsail_not_found_formats_are_handled_precisely(control,monkeypatch,code,http,status):
    web,c,h,aws=control
    monkeypatch.setattr(resources,'catalog',lambda *args:{'bundles':[{'id':'micro','price_usd_per_month':5}],'blueprints':[{'id':'debian'}],'zones':['us-east-1a']})
    aws.get_instance.side_effect=ClientError({'Error':{'Code':code},'ResponseMetadata':{'HTTPStatusCode':http}},'GetInstance')
    data=request('create',bundle_id='micro',blueprint_id='debian')
    assert c.post('/api/tasks/preview',headers=h,json=data.model_dump()).status_code==status
    aws.create_instances.assert_not_called()


def test_duplicate_aliases_cannot_create_duplicate_resource_operations(control,monkeypatch):
    web,_,_,_=control
    monkeypatch.setattr(web,'base_profiles',lambda:['default','alias'])
    data=request()
    other=data.items[0].model_copy(deep=True)
    other.target.profile='alias'
    data.items.append(other)
    with pytest.raises(HTTPException) as exc:
        web.control_jobs.preview(data,'tester')
    assert exc.value.status_code==400
