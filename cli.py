"""Native CLI for the single control-plane implementation."""
import argparse
import json
import sys
from pathlib import Path
from fastapi import HTTPException
from pydantic import ValidationError


def parser_factory():
    p = argparse.ArgumentParser(prog='awscc')
    p.add_argument('area', choices=['instances','tasks','configurations','rules','traffic','automation','accounts','audit','cost'])
    p.add_argument('verb')
    p.add_argument('identifier', nargs='?')
    p.add_argument('--file')
    p.add_argument('--profile', default='default')
    p.add_argument('--region', default='all')
    p.add_argument('--service', choices=['all','ec2','lightsail'], default='all')
    p.add_argument('--instance')
    p.add_argument('--kind', choices=['bootstrap','connection','script','rule'])
    p.add_argument('--window', choices=['5m','1h','24h','7d','30d','day','month'], default='1h')
    p.add_argument('--period', type=int, choices=[60,300], default=300)
    p.add_argument('--timezone', default='UTC')
    p.add_argument('--yes', action='store_true')
    p.add_argument('--json', action='store_true')
    return p


def execute(args, web):
    from controlplane import resources, metrics
    from controlplane.models import JobRequest
    from controlplane.models import ConfigDocument
    from controlplane.configurations import Configurations
    from controlplane.store import documents
    actor = 'local-cli'
    def confirmed():
        if not args.yes:
            raise ValueError('Use --yes to confirm this write operation')
    if args.area == 'instances' and args.verb == 'list':
        return resources.inventory(web, '' if args.profile == 'all' else args.profile, args.region, args.service, limit=500)
    if args.area == 'tasks':
        if args.verb in ('preview','submit'):
            if not args.file:
                raise ValueError('--file JSON is required; preserve request_key between preview and submission')
            data = JobRequest(**json.loads(Path(args.file).read_text(encoding='utf-8-sig')))
            if args.verb == 'preview':
                return web.control_jobs.preview(data, actor)
            confirmed()
            return web.control_jobs.submit(data, actor)
        if args.verb == 'list':
            with web.db() as c:
                ids = [r[0] for r in c.execute('SELECT id FROM v2_jobs ORDER BY created_at DESC LIMIT 100')]
            return {'items': [web.control_jobs.detail(i) for i in ids]}
        if args.verb in ('show','cancel','verify') and args.identifier:
            if args.verb == 'show': return web.control_jobs.detail(args.identifier)
            confirmed()
            return getattr(web.control_jobs, args.verb)(args.identifier, actor)
    if args.area in ('configurations','rules'):
        service = Configurations(web, web.control_jobs)
        kind = 'rule' if args.area == 'rules' else args.kind
        if args.verb in ('save','create') and args.file:
            confirmed()
            data = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
            if args.area == 'rules':
                data['kind'] = 'rule'
            return service.save(ConfigDocument(**data), actor)
        if args.identifier and args.verb in ('enable','disable','delete'):
            confirmed()
            if args.verb == 'delete':
                return service.delete(args.identifier, actor)
            return service.set_enabled(args.identifier, args.verb == 'enable', actor)
        if args.verb != 'list' or not kind:
            raise ValueError('Use list --kind, save --file, or enable/disable/delete ID --yes')
        rows = documents(web, kind)
        # Never print stored connection secrets or initialization scripts from a listing.
        return {'items': [{k: r[k] for k in ('id','kind','name','version','created_at')} for r in rows]}
    if args.area == 'traffic' and args.verb == 'report':
        if args.region == 'all' or args.service == 'all' or not args.instance:
            raise ValueError('Use explicit --region, --service and --instance')
        return metrics.report(web,args.profile,args.region,args.service,args.instance,args.window,args.timezone,args.period)
    if args.area == 'automation':
        if args.verb in ('pause','resume'):
            confirmed()
            with web.db() as c:
                c.execute("INSERT OR REPLACE INTO app_settings VALUES('automation_paused',?)", ('true' if args.verb == 'pause' else 'false',))
            web.audit(actor, 'automation', operation=args.verb)
            return {'paused': args.verb == 'pause'}
        if args.verb == 'events':
            with web.db() as c:
                return {'items': [dict(r) for r in c.execute('SELECT * FROM v2_events ORDER BY created_at DESC LIMIT 100')]}
    if args.area == 'accounts' and args.verb == 'list':
        return {'profiles': web.available_profiles()}
    if args.area == 'audit' and args.verb == 'list':
        return web.audit_list(200)
    if args.area == 'cost' and args.verb == 'report':
        return web.costs(args.profile)
    raise ValueError('Unsupported command; use --help')


def main(argv=None, api=None):
    args = parser_factory().parse_args(argv)
    if api is None:
        import app as api
    try:
        result = execute(args, api)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 3 if isinstance(result, dict) and result.get('errors') else 0
    except ValidationError:
        print('Invalid configuration/task JSON fields',file=sys.stderr)
        return 2
    except (ValueError, HTTPException) as exc:
        print(str(exc.detail) if isinstance(exc, HTTPException) else str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(api.aws_error(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
