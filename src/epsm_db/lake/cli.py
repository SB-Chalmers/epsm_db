"""The epsm-db GIS-first ingestion and publication commands."""
import argparse
import json
import logging
import sys

import psycopg

from .database import Lake
from .ingest import KINDS


def build_parser():
    parser = argparse.ArgumentParser(prog='epsm-db', description='Import source evidence, link entities, publish indexed GIS layers.')
    parser.add_argument('--log-level', choices=('DEBUG','INFO','WARNING','ERROR','CRITICAL'), default='INFO')
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('init', 'migrate', 'ingest', 'link', 'publish', 'run', 'status', 'grant', 'extract', 'fetch-scb'):
        sub = commands.add_parser(command)
        sub.add_argument('--database-url', help='Defaults to EPSM_DATABASE_URL, then UBEM_DATABASE_URL')
        if command in ('ingest', 'run'):
            sub.add_argument('manifest', help='GIS lake TOML source manifest')
            sub.add_argument('--source-root', help='Override the manifest root without changing source identities')
            sub.add_argument('--kinds', nargs='+', choices=KINDS)
            sub.add_argument('--batch-rows', type=int, default=4096)
            sub.add_argument('--batch-bytes', type=int, default=32 * 1024**2)
            sub.add_argument('--file-checksums', action='store_true', help='Optional once-per-file checksum; no geometry hashing')
        if command in ('link', 'run'):
            sub.add_argument('--minimum-overlap', type=float, default=0.5)
        if command == 'fetch-scb':
            sub.add_argument('catalogue', help='SCB download catalogue TOML')
            sub.add_argument('output', help='New or resumable immutable snapshot directory')
        if command == 'extract':
            from .extract import DEFAULT_LAYERS
            sub.add_argument('output', help='New GeoPackage file; existing files are never overwritten')
            sub.add_argument('--layers', nargs='+', default=DEFAULT_LAYERS)
            sub.add_argument('--municipality', dest='municipality_code')
            sub.add_argument('--bbox', nargs=4, type=float, metavar=('XMIN','YMIN','XMAX','YMAX'))
            sub.add_argument('--area-type')
            sub.add_argument('--area-code')
            sub.add_argument('--boundary-version')
            sub.add_argument('--batch-rows', type=int, default=10000)
        if command == 'publish':
            sub.add_argument('--force', action='store_true', help='Refresh GIS snapshots even when their revision is unchanged')
        if command == 'grant':
            sub.add_argument('role', help='Existing participant or read-only group role')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging,args.log_level),format='%(message)s')
    try:
        if args.command == 'fetch-scb':
            from .scb import fetch_scb
            print(json.dumps(fetch_scb(args.catalogue, args.output), ensure_ascii=False, indent=2))
            return 0
        lake = Lake(args.database_url)
        result = {}
        if args.command in ('init', 'run'):
            result['init'] = lake.initialize()
        if args.command in ('ingest', 'run'):
            result['ingest'] = lake.ingest(args.manifest, source_root=args.source_root, kinds=args.kinds,
                batch_size=args.batch_rows, batch_bytes=args.batch_bytes, file_checksums=args.file_checksums)
        if args.command in ('link', 'run'):
            result['link'] = lake.link(args.minimum_overlap)
        if args.command in ('publish', 'run'):
            result['publish'] = lake.publish(force=getattr(args, 'force', False))
        if args.command == 'extract':
            result = lake.extract(args.output, layers=args.layers, municipality_code=args.municipality_code,
                bbox=args.bbox, area_type=args.area_type, area_code=args.area_code,
                boundary_version=args.boundary_version, batch_rows=args.batch_rows)
        if args.command == 'migrate':
            result = lake.migrate()
        if args.command == 'status':
            result = lake.status()
        if args.command == 'grant':
            result = lake.grant_reader(args.role)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except KeyboardInterrupt:
        print('Interrupted. Committed import batches can be resumed with the same manifest.', file=sys.stderr)
        return 130
    except (ValueError, OSError, psycopg.Error) as error:
        print(f'Error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
