"""V7 read-only catalog and post-submission verification; no real AWS calls."""
import sys
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_PASSWORD', 'strong-v7-test-password-1234')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('AWS_REGIONS', 'ap-northeast-1')
    monkeypatch.setenv('COOKIE_SECURE', 'false')
    for name in ('taskqueue', 'app'):
        sys.modules.pop(name, None)
    import app
    import taskqueue as q
    monkeypatch.setattr(q, 'valid_profile', lambda p: ('base', p))
    return app, q


def not_found():
    return ClientError({'Error': {'Code': 'NotFoundException'},
                        'ResponseMetadata': {'HTTPStatusCode': 404}}, 'GetInstance')


def test_catalog_pagination_and_regions(env, monkeypatch):
    _, q = env
    aws = Mock()
    aws.get_bundles.side_effect = [
        {'bundles': [{'bundleId': 'nano', 'price': 7, 'isActive': True}], 'nextPageToken': 'more'},
        {'bundles': [{'bundleId': 'micro', 'price': 12, 'isActive': True}]},
    ]
    aws.get_blueprints.return_value = {'blueprints': [{'blueprintId': 'ubuntu', 'name': 'Ubuntu'}]}
    aws.get_regions.return_value = {'regions': [{'name': 'ap-northeast-1', 'availabilityZones': [{'zoneName': 'ap-northeast-1a'}]}]}
    monkeypatch.setattr(q, 'client', lambda *args: aws)
    result = q.catalog('default', 'ap-northeast-1')
    assert [x['id'] for x in result['bundles']] == ['nano', 'micro']
    assert result['zones'] == ['ap-northeast-1a']
    assert aws.get_bundles.call_args_list[1].kwargs['pageToken'] == 'more'


def test_submitted_verification_does_not_create(env, monkeypatch):
    _, q = env
    aws = Mock()
    aws.get_bundles.return_value = {'bundles': [{'bundleId': 'nano', 'price': 7}]}
    aws.get_blueprints.return_value = {'blueprints': [{'blueprintId': 'ubuntu'}]}
    aws.get_instance.side_effect = [not_found(), {'instance': {'state': {'name': 'running'}}}]
    monkeypatch.setattr(q, 'client', lambda *args: aws)
    task = q.submit(q.CreateBatch(profile='default', region='ap-northeast-1',
                 names=['demo-01'], bundle_id='nano', blueprint_id='ubuntu',
                 dry_run=False, confirm='CREATE'))
    q.run_once()
    result = q.verify_task(task['id'])
    assert result['items'][0]['verification'] == 'running'
    assert result['items'][0]['verified_at']
    assert aws.create_instances.call_count == 1
    assert aws.get_instance.call_count == 2


def test_ambiguous_failed_create_can_be_found(env, monkeypatch):
    _, q = env
    aws = Mock()
    aws.get_bundles.return_value = {'bundles': [{'bundleId': 'nano', 'price': 7}]}
    aws.get_blueprints.return_value = {'blueprints': [{'blueprintId': 'ubuntu'}]}
    aws.get_instance.side_effect = [not_found(), {'instance': {'state': {'name': 'pending'}}}]
    aws.create_instances.side_effect = TimeoutError('unknown outcome')
    monkeypatch.setattr(q, 'client', lambda *args: aws)
    task = q.submit(q.CreateBatch(profile='default', region='ap-northeast-1',
                 names=['demo-01'], bundle_id='nano', blueprint_id='ubuntu',
                 dry_run=False, confirm='CREATE'))
    q.run_once()
    result = q.verify_task(task['id'])
    assert result['status'] == 'failed'  # immutable AWS request outcome
    assert result['items'][0]['verification'] == 'found'
    assert aws.create_instances.call_count == 1


def test_new_endpoints_protected(env):
    app, _ = env
    c = TestClient(app.app)
    assert c.get('/api/lightsail/catalog?profile=default&region=ap-northeast-1').status_code == 401
    assert c.post('/api/tasks/123/verify').status_code == 401


def test_cli_new_commands(env, monkeypatch):
    _, q = env
    import cli
    monkeypatch.setattr(q, 'catalog', lambda profile, region: {'profile': profile, 'region': region})
    p = cli.parser_factory()
    args = p.parse_args(['catalog', '--profile', 'default', '--region', 'ap-northeast-1'])
    assert cli.execute(args, None)['region'] == 'ap-northeast-1'
    args = p.parse_args(['tasks', 'verify', 'abcd'])
    monkeypatch.setattr(q, 'verify_task', lambda identifier, actor: {'id': identifier})
    assert cli.execute(args, None)['id'] == 'abcd'
