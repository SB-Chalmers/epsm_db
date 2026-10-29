"""Structured progress through the application's standard logger."""
from datetime import datetime, timezone
import json
import logging

logger = logging.getLogger('epsm_db.lake')


def report(event, **values):
    logger.info(json.dumps({'event': event, 'time': datetime.now(timezone.utc).isoformat(), **values}))
