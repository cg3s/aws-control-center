"""AWS Control Center V7 resource CLI. Uses the same boto3 business functions as the Web API."""
import argparse
import json
import sys


def parser_factory():
    p = argparse.ArgumentParser(prog='awscc', description='AWS Control Center resource CLI (requires configured AWS credentials)')
    p.add_argument('--json', action='store_true', help='Output JSON (also accepted after subcommand)')
    top = p.add_subparsers(dest='area', required=True)
    def common(x, region=True):
        x.add_argument('--profile', default='default', help='AWS profile or configured AssumeRole alias')
        if region:
            x.add_argument('--region', default='all', help='AWS region, or all for inventory')
        x.add_argument('--json', action='store_true', dest='json_local')
    inst = top.add_parser('instances', help='Instance inventory / lifecycle')
    si = inst.add_subparsers(dest='verb', required=True)
    ls = si.add_parser('list'); common(ls)
    ls.add_argument('--service', choices=['all', 'lightsail', 'ec2'], default='all')
    for verb in ('start','stop','reboot'):
        x = si.add_parser(verb); common(x)
        x.add_argument('--service', choices=['lightsail','ec2'], required=True)
        x.add_argument('--name', required=True, help='Instance name or EC2 instance ID')
        x.add_argument('--yes', action='store_true', help='Confirm state-changing action (for scripts)')
    ip = top.add_parser('ip', help='Lightsail static IP management')
    ip_sub = ip.add_subparsers(dest='verb', required=True)
    for verb in ('list','allocate','attach','detach','release'):
        x=ip_sub.add_parser(verb); common(x)
        if verb != 'list': x.add_argument('--name', required=True)
        if verb == 'attach': x.add_argument('--instance', required=True)
        if verb != 'list': x.add_argument('--yes', action='store_true')
    cat=top.add_parser('catalog', help='Read regional Lightsail bundles, blueprints and zones')
    common(cat)
    tr = top.add_parser('traffic', help='Lightsail network metric sample sums')
    trs=tr.add_subparsers(dest='verb', required=True)
    x=trs.add_parser('report'); common(x)
    x.add_argument('--instance', required=True); x.add_argument('--days', type=int, default=7)
    co=top.add_parser('cost', help='Cost Explorer')
    cos=co.add_subparsers(dest='verb', required=True)
    x=cos.add_parser('report'); common(x, region=False)
    ac=top.add_parser('accounts', help='Account aliases')
    acs=ac.add_subparsers(dest='verb', required=True)
    acs.add_parser('list').add_argument('--json', action='store_true', dest='json_local')
    au=top.add_parser('audit', help='Recent audit entries')
    aus=au.add_subparsers(dest='verb', required=True)
    x=aus.add_parser('list'); x.add_argument('--limit', type=int, default=30)
    x.add_argument('--json', action='store_true', dest='json_local')
    ta=top.add_parser('tasks', help='Durable Lightsail creation jobs')
    tas=ta.add_subparsers(dest='verb', required=True)
    x=tas.add_parser('list'); x.add_argument('--limit', type=int, default=30); x.add_argument('--json', action='store_true', dest='json_local')
    x=tas.add_parser('show'); x.add_argument('id'); x.add_argument('--json', action='store_true', dest='json_local')
    x=tas.add_parser('verify'); x.add_argument('id'); x.add_argument('--json', action='store_true', dest='json_local')
    x=tas.add_parser('cancel'); x.add_argument('id'); x.add_argument('--yes', action='store_true'); x.add_argument('--json', action='store_true', dest='json_local')
    x=tas.add_parser('create'); common(x)
    x.add_argument('--names', required=True, help='Comma-separated instance names, up to 10')
    x.add_argument('--bundle', required=True); x.add_argument('--blueprint', required=True)
    x.add_argument('--zone', default=''); x.add_argument('--yes', action='store_true')
    x.add_argument('--submit', action='store_true', help='Actually queue create requests (default is preview)')
    return p


def confirm(args, description, input_fn=input):
    if args.yes: return
    if not sys.stdin.isatty():
        raise ValueError('Write action requires --yes in non-interactive mode')
    answer = input_fn('Type YES to confirm '+description+': ')
    if answer != 'YES': raise ValueError('Cancelled (not submitted)')


def execute(args, api, input_fn=input):
    if args.area == 'tasks':
        from taskqueue import CreateBatch, submit, list_tasks, detail, cancel, verify_task
        if args.verb == 'list': return list_tasks(args.limit)
        if args.verb == 'show': return detail(args.id)
        if args.verb == 'verify': return verify_task(args.id,actor='local-cli')
        if args.verb == 'cancel':
            confirm(args, 'cancel remaining pending items for task ' + args.id, input_fn)
            return cancel(args.id, actor='local-cli')
        if args.region == 'all': raise ValueError('--region must be explicit')
        names = [x.strip() for x in args.names.split(',')]
        if args.submit: confirm(args, 'create ' + ', '.join(names), input_fn)
        return submit(CreateBatch(profile=args.profile,region=args.region,names=names,
                      bundle_id=args.bundle,blueprint_id=args.blueprint,
                      availability_zone=args.zone,dry_run=not args.submit,
                      confirm='CREATE' if args.submit else ''),actor='local-cli')
    if args.area == 'catalog':
        from taskqueue import catalog
        if args.region == 'all': raise ValueError('--region must be explicit for catalog')
        return catalog(args.profile,args.region)
    if args.area == 'instances':
        if args.verb == 'list':
            return api.instances(profile=args.profile, service=args.service, region=args.region)
        if args.region == 'all': raise ValueError('--region must be an explicit enabled AWS region for writes')
        confirm(args, f'{args.verb} {args.service} {args.name} ({args.profile}/{args.region})', input_fn)
        data=api.InstanceAction(service=args.service, profile=args.profile, region=args.region,
                                instance=args.name, action=args.verb)
        try:
            api.invoke_action(data)
            api.audit('local-cli','instance',profile=args.profile, region=args.region,
                      service=args.service,resource=args.name,operation=args.verb,result='submitted')
        except Exception as exc:
            api.audit('local-cli','instance',profile=args.profile, region=args.region,
                      service=args.service,resource=args.name,operation=args.verb,result='failed',detail=type(exc).__name__)
            raise
        return {'ok': True, 'submitted': True, 'operation':args.verb, 'instance':args.name}
    if args.area == 'ip':
        if args.region == 'all': raise ValueError('--region must be explicit for IP operations')
        if args.verb == 'list': return api.static_ips(profile=args.profile, region=args.region)
        confirm(args, f'{args.verb} Lightsail static IP {args.name} ({args.profile}/{args.region})', input_fn)
        data=api.StaticIpAction(profile=args.profile,region=args.region,name=args.name,
                                action=args.verb,instance=getattr(args,'instance',''),
                                confirm=args.name if args.verb=='release' else '')
        # Shared Web API handler enforces attached-IP checks and writes audit on success/errors.
        from starlette.requests import Request
        request=Request({'type':'http','method':'POST','path':'/cli','headers':[],
                         'client':('local-cli',0),'scheme':'http','server':('localhost',0)})
        return api.static_ip_action(data, request)
    if args.area == 'traffic':
        if args.region == 'all': raise ValueError('--region must be explicit for traffic reports')
        return api.traffic(profile=args.profile,region=args.region,instance=args.instance,days=args.days)
    if args.area == 'cost': return api.costs(profile=args.profile)
    if args.area == 'accounts':
        return {'base_profiles':api.base_profiles(),'role_aliases':[{k:v for k,v in a.items() if k!='external_id'} for a in api.accounts()]}
    if args.area == 'audit': return api.audit_list(limit=args.limit)
    raise ValueError('Unknown command')


def display(result, json_mode):
    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    items = result.get('items') if isinstance(result,dict) else None
    if isinstance(items,list):
        if not items: print('(no results)')
        else:
            cols = {'profile','region','service','id','name','state','ip','attached_to','operation','result','created_at'}
            keys = [k for k in items[0] if k in cols] or list(items[0])[:6]
            widths={k:min(38,max(len(k), *(len(str(x.get(k,''))) for x in items))) for k in keys}
            print('  '.join(k.ljust(widths[k]) for k in keys))
            print('  '.join('-'*widths[k] for k in keys))
            for x in items:
                print('  '.join(str(x.get(k,''))[:widths[k]].ljust(widths[k]) for k in keys))
        if result.get('errors'):
            print('Partial AWS region/service errors:', file=sys.stderr)
            print(json.dumps(result['errors'],ensure_ascii=False),file=sys.stderr)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def main(argv=None, api=None, input_fn=input):
    p=parser_factory()
    args=p.parse_args(argv)
    try:
        if api is None:
            import app as api
        result=execute(args,api,input_fn)
        display(result,args.json or getattr(args,'json_local',False))
        if isinstance(result,dict) and result.get('errors'): return 3
        return 0
    except (ValueError,KeyboardInterrupt) as exc:
        print('Error: '+str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        # Avoid printing AWS exception payloads or credentials to CLI output.
        from botocore.exceptions import ClientError, BotoCoreError
        from fastapi import HTTPException
        if isinstance(exc,HTTPException): message=str(exc.detail)
        elif isinstance(exc,(ClientError,BotoCoreError)) and api is not None: message=api.aws_error(exc)
        else: message=f'{type(exc).__name__}: operation failed (see local audit/logs)'
        print('Error: '+message,file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
