"""Preview legacy resource pins; use --apply during a backed-up maintenance window."""
import argparse
import asyncio
import json

from vibecanvas_api.services.pin_resource_versions import pin_resource_versions
from vibecanvas_api.services.tenant_db import session_scope_admin


async def main(apply):
    async with session_scope_admin() as session:
        report = await pin_resource_versions(session, apply=apply)
        print(json.dumps({"applied": apply, "resources": report}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    asyncio.run(main(parser.parse_args().apply))
