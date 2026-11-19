"""AWS ingress rules for new instances."""
import hashlib
import json
from threading import Lock

from botocore.exceptions import ClientError
from fastapi import HTTPException

_group_lock = Lock()


def grouped(rules):
    ranges = {}
    for rule in rules:
        port_range = rule['protocol'], rule['from_port'], rule['to_port']
        ranges.setdefault(port_range, set()).add(rule['cidr'])
    return sorted((protocol, start, end, sorted(cidrs)) for (protocol, start, end), cidrs in ranges.items())


def lightsail_ports(rules):
    return [{'protocol': protocol, 'fromPort': start, 'toPort': end,
             'cidrs': [cidr for cidr in cidrs if ':' not in cidr],
             'ipv6Cidrs': [cidr for cidr in cidrs if ':' in cidr]}
            for protocol, start, end, cidrs in grouped(rules)]


def ec2_permissions(rules):
    return [{'IpProtocol': protocol, 'FromPort': start, 'ToPort': end,
             'IpRanges': [{'CidrIp': cidr} for cidr in cidrs if ':' not in cidr],
             'Ipv6Ranges': [{'CidrIpv6': cidr} for cidr in cidrs if ':' in cidr]}
            for protocol, start, end, cidrs in grouped(rules)]


def ec2_group(read_api, write_api, vpc_id, rules):
    desired = {(rule['protocol'], rule['from_port'], rule['to_port'], rule['cidr']) for rule in rules}
    identifier = hashlib.sha256(json.dumps(sorted(desired)).encode()).hexdigest()[:24]
    name = 'awscc-firewall-' + identifier
    filters = [{'Name': 'vpc-id', 'Values': [vpc_id]}, {'Name': 'group-name', 'Values': [name]}]

    # Inside one worker, batch items using the same rules share one group per VPC.
    with _group_lock:
        groups = read_api.describe_security_groups(Filters=filters)['SecurityGroups']
        if not groups:
            group_id = write_api.create_security_group(
                VpcId=vpc_id, GroupName=name, Description='AWS Control Center initialization firewall',
                TagSpecifications=[{'ResourceType': 'security-group', 'Tags': [{'Key': 'awscc-firewall', 'Value': identifier}]}],
            )['GroupId']
            existing = set()
        else:
            group = groups[0]
            group_id = group['GroupId']
            if not any(tag['Key'] == 'awscc-firewall' and tag['Value'] == identifier for tag in group.get('Tags', [])):
                raise HTTPException(409, '发现同名安全组，无法核实其由 AWS Control Center 创建')
            existing = set()
            for permission in group.get('IpPermissions', []):
                if permission.get('UserIdGroupPairs') or permission.get('PrefixListIds'):
                    raise HTTPException(409, f'初始化安全组 {group_id} 的入站规则已被修改')
                cidrs = [entry['CidrIp'] for entry in permission.get('IpRanges', [])]
                cidrs += [entry['CidrIpv6'] for entry in permission.get('Ipv6Ranges', [])]
                existing.update((permission['IpProtocol'], permission.get('FromPort'), permission.get('ToPort'), cidr)
                                for cidr in cidrs)
            if existing - desired:
                raise HTTPException(409, f'初始化安全组 {group_id} 含有模板之外的入站规则')
        missing = [dict(zip(('protocol', 'from_port', 'to_port', 'cidr'), rule)) for rule in sorted(desired - existing)]
        if missing:
            write_api.authorize_security_group_ingress(GroupId=group_id, IpPermissions=ec2_permissions(missing))
        return group_id


def lightsail_state(read_api, write_api, name, rules, result):
    requested = lightsail_ports(rules)

    def normalize(ports):
        return {(port['protocol'], port['fromPort'], port['toPort'], source)
                for port in ports
                for source in (port.get('cidrs', []) + port.get('ipv6Cidrs', []) + port.get('cidrListAliases', []) or ['unscoped'])}

    actual = read_api.get_instance_port_states(instanceName=name)['portStates']
    if normalize([port for port in actual if port.get('state', 'open') == 'open']) == normalize(requested):
        result['firewall_status'] = 'applied'
        result.pop('firewall_error', None)
        return 'succeeded'
    operation_id = result.get('firewall_operation_id')
    if operation_id:
        operation = read_api.get_operation(operationId=operation_id)['operation']
        if operation.get('status') == 'Failed':
            result['firewall_status'] = 'failed'
            result['firewall_error'] = operation.get('errorCode') or 'Lightsail rejected firewall rules'
            return 'failed'
    elif 'firewall_status' not in result:
        # A timeout is ambiguous. Verify the port state without sending a second write.
        result['firewall_status'] = 'unknown'
        try:
            operation = write_api.put_instance_public_ports(instanceName=name, portInfos=requested)['operation']
        except ClientError as exc:
            if exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 500) < 500:
                result['firewall_status'] = 'failed'
                result['firewall_error'] = exc.response.get('Error', {}).get('Code', 'Lightsail API error')
                return 'failed'
            raise
        result['firewall_operation_id'] = operation['id']
        result['firewall_status'] = 'submitted'
    return 'verifying' if result.get('firewall_status') != 'failed' else 'failed'
