"""Local browser smoke-test fixture. Never connects to AWS or uses production data."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(APP_PASSWORD='ui-smoke-password-1234', COOKIE_SECURE='false',
                  DATA_DIR=tempfile.mkdtemp(prefix='awscc-ui-'), AWS_REGIONS='us-east-1', AWS_EC2_METADATA_DISABLED='true')
import app
from controlplane.store import stamp
aws = MagicMock()
aws.get_caller_identity.return_value = {'Account': '123456789012', 'Arn': 'arn:aws:iam::123456789012:user/test'}
aws.get_instance.return_value = {'instance': {'name': 'demo-lightsail', 'arn': 'arn:demo', 'createdAt': '2026-11-01', 'state': {'name': 'running'}, 'publicIpAddress': '1.1.1.1'}}
aws.get_bundles.return_value = {'bundles': [{'bundleId': 'micro', 'name': 'Micro', 'price': 5, 'transferPerMonthInGb': 1024}]}
aws.get_blueprints.return_value = {'blueprints': [{'blueprintId': 'debian', 'name': 'Debian', 'platform': 'LINUX_UNIX'}]}
aws.get_regions.return_value = {'regions': [{'name': 'us-east-1', 'availabilityZones': [{'zoneName': 'us-east-1a'}]}]}
aws.get_static_ips.return_value = {'staticIps': []}
aws.get_cost_and_usage.return_value = {'ResultsByTime': [{'Groups': [{'Keys': ['Amazon Lightsail'], 'Metrics': {'UnblendedCost': {'Amount': '5'}}}]}]}
app.base_profiles = lambda: ['default']
app.client = lambda *args: aws
app.mutation_client = lambda *args: (_ for _ in ()).throw(AssertionError('UI fixture cannot modify AWS'))
with app.db() as c:
    items = [dict(id='demo-lightsail', name='demo-lightsail', profile='default', region='us-east-1', service='lightsail',
                  state='running', ip='1.1.1.1', type='micro', account_id='123456789012', partition='aws',
                  resource_key='aws/123456789012/us-east-1/lightsail/demo-lightsail')]
    c.execute('INSERT INTO v2_inventory VALUES(?,?,?,?,?,?)', ('default','123456789012','aws',stamp(),json.dumps(items),'[]'))
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app.app, host='127.0.0.1', port=8765)
