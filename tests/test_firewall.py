import pytest
from botocore.exceptions import ClientError
from test_tasks import enqueue, ready, request
from controlplane import firewall, resources


RULES = [
    {'protocol': 'tcp', 'from_port': 22, 'to_port': 22, 'cidr': '203.0.113.5/32'},
    {'protocol': 'udp', 'from_port': 2000, 'to_port': 2010, 'cidr': '2001:db8::/64'},
]


@pytest.mark.parametrize('rule', [{**RULES[0], 'cidr': 'not-a-cidr'}, {**RULES[0], 'to_port': 21}])
def test_invalid_firewall_rule_rejected_at_template_save(control, rule):
    _, client, headers, _ = control
    response = client.post('/api/configurations', headers=headers,
                           json={'kind': 'bootstrap', 'name': 'invalid', 'payload': {'firewall_rules': [rule]}})
    assert response.status_code == 400


def test_lightsail_firewall_from_template_is_applied_and_verified(control, monkeypatch):
    web, client, headers, aws = control
    template = client.post('/api/configurations', headers=headers,
                           json={'kind': 'bootstrap', 'name': 'firewall', 'payload': {'firewall_rules': RULES}}).json()
    monkeypatch.setattr(resources, 'catalog', lambda *args: {'bundles': [{'id': 'micro', 'price_usd_per_month': 5}],
                        'blueprints': [{'id': 'debian'}], 'zones': ['us-east-1a']})
    aws.get_instance.side_effect = ClientError({'Error': {'Code': 'NotFoundException'}}, 'GetInstance')
    job = request('create', bundle_id='micro', blueprint_id='debian', bootstrap_id=template['id'])
    preview = web.control_jobs.preview(job, 'tester')
    assert preview['items'][0]['firewall_rules'] == RULES
    aws.put_instance_public_ports.assert_not_called()
    task = enqueue(web, job)

    web.control_jobs.run_once()
    tags = aws.create_instances.call_args.kwargs['tags']
    aws.get_instance.side_effect = None
    aws.get_instance.return_value = {'instance': {'name': 'node', 'state': {'name': 'running'}, 'tags': tags}}
    aws.get_instance_port_states.return_value = {'portStates': []}
    aws.put_instance_public_ports.return_value = {'operation': {'id': 'op-firewall'}}
    ready(web, task['id'])
    web.control_jobs.run_once()
    port_infos = firewall.lightsail_ports(RULES)
    aws.put_instance_public_ports.assert_called_once_with(instanceName='node', portInfos=port_infos)
    assert web.control_jobs.detail(task['id'])['items'][0]['status'] == 'verifying'

    aws.get_instance_port_states.return_value = {'portStates': port_infos}
    ready(web, task['id'])
    web.control_jobs.run_once()
    finished = web.control_jobs.detail(task['id'])
    assert finished['status'] == 'succeeded' and finished['items'][0]['result']['firewall_status'] == 'applied'
    assert aws.put_instance_public_ports.call_count == 1


def test_ec2_firewall_group_is_attached_without_editing_existing_groups(control):
    web, _, _, aws = control
    aws.describe_images.return_value = {'Images': [{'State': 'available', 'Architecture': 'x86_64',
                                                   'RootDeviceName': '/dev/sda1', 'RootDeviceType': 'ebs'}]}
    aws.describe_instance_types.return_value = {'InstanceTypes': [{'ProcessorInfo': {'SupportedArchitectures': ['x86_64']}}]}
    aws.describe_subnets.return_value = {'Subnets': [{'VpcId': 'vpc-1'}]}
    aws.describe_security_groups.return_value = {'SecurityGroups': []}
    aws.create_security_group.return_value = {'GroupId': 'sg-managed'}
    aws.run_instances.return_value = {'Instances': [{'InstanceId': 'i-0123456789abcdef0'}]}
    job = request('create', image_id='ami-1', instance_type='t3.micro', subnet_id='subnet-1',
                  security_group_ids=['sg-existing'], launch={'firewall_rules': RULES})
    job.items[0].target.service = 'ec2'
    task = enqueue(web, job)
    aws.create_security_group.assert_not_called()
    web.control_jobs.run_once()
    assert aws.create_security_group.call_args.kwargs['VpcId'] == 'vpc-1'
    aws.authorize_security_group_ingress.assert_called_once_with(GroupId='sg-managed', IpPermissions=firewall.ec2_permissions(RULES))
    assert aws.run_instances.call_args.kwargs['NetworkInterfaces'][0]['Groups'] == ['sg-managed']
    assert web.control_jobs.detail(task['id'])['items'][0]['result']['firewall_group_id'] == 'sg-managed'


def test_lightsail_ambiguous_firewall_update_is_read_back_without_resending(control):
    _, _, _, aws = control
    result = {}
    aws.get_instance_port_states.return_value = {'portStates': []}
    aws.put_instance_public_ports.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        firewall.lightsail_state(aws, aws, 'node', RULES, result)
    assert result == {'firewall_status': 'unknown'}
    assert firewall.lightsail_state(aws, aws, 'node', RULES, result) == 'verifying'
    aws.get_instance_port_states.return_value = {'portStates': firewall.lightsail_ports(RULES)}
    assert firewall.lightsail_state(aws, aws, 'node', RULES, result) == 'succeeded'
    assert aws.put_instance_public_ports.call_count == 1
