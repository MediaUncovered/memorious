import os
import io
import yaml
import logging
import time
from datetime import timedelta, datetime
from importlib import import_module

import redis
import blinker

from memorious import settings
from memorious.signals import signals
from memorious.core import session, local_queue, redis_pool
from memorious.model import Tag, Event, Result
from memorious.logic.context import handle
from memorious.logic.stage import CrawlerStage
from memorious.helpers.dates import parse_date

log = logging.getLogger(__name__)


class Crawler(object):
    """A processing graph that constitutes a crawler."""
    SCHEDULES = {
        'daily': timedelta(days=1),
        'weekly': timedelta(weeks=1),
        'monthly': timedelta(weeks=4)
    }

    def __init__(self, manager, source_file):
        self.manager = manager
        self.source_file = source_file
        with io.open(source_file, encoding='utf-8') as fh:
            self.config_yaml = fh.read()
            self.config = yaml.load(self.config_yaml)

        self.name = os.path.basename(source_file)
        self.name = self.config.get('name', self.name)
        self.description = self.config.get('description', self.name)
        self.category = self.config.get('category', 'scrape')
        self.schedule = self.config.get('schedule')
        self.disabled = self.config.get('disabled', False)
        self.init_stage = self.config.get('init', 'init')
        self.delta = Crawler.SCHEDULES.get(self.schedule)
        self.delay = int(self.config.get('delay', 0))
        self.expire = int(self.config.get('expire', settings.EXPIRE))
        self.stealthy = self.config.get('stealthy', False)
        self.cleanup_method_name = self.config.get('cleanup_method')

        self.stages = {}
        for name, stage in self.config.get('pipeline', {}).items():
            self.stages[name] = CrawlerStage(self, name, stage)

    def last_run(self):
        if settings.REDIS_HOST:
            r = redis.Redis(connection_pool=redis_pool)
            last_run = r.get(self.name+":last_run")
            if last_run:
                return parse_date(last_run)
        return None

    def check_due(self):
        """Check if the last execution of this crawler is older than
        the scheduled interval."""
        if self.disabled:
            return False
        if self.delta is None:
            return False
        last_run = self.last_run()
        if last_run is None:
            return True
        now = datetime.utcnow()
        if now > last_run + self.delta:
            return True
        return False

    def get_op_count(self):
        """Total operations performed for this crawler"""
        if settings.REDIS_HOST:
            r = redis.Redis(connection_pool=redis_pool)
            total_ops = r.get(self.name+":total_ops")
            if total_ops:
                return int(total_ops)
        return None

    def is_running(self):
        """Is the crawler currently running?"""
        if settings.REDIS_HOST:
            r = redis.Redis(connection_pool=redis_pool)
            active_ops = r.get(self.name)
            if active_ops and int(active_ops) > 0:
                return True
        return False

    def flush(self):
        """Delete all run-time data generated by this crawler."""
        Tag.delete(self.name)
        Event.delete(self.name)
        Result.delete(self.name)
        session.commit()
        flushed_signal = blinker.signal(signals.CRAWLER_FLUSHED)
        flushed_signal.send(self)

    def run(self, incremental=None):
        """Queue the execution of a particular crawler."""
        state = {
            'crawler': self.name,
            'incremental': settings.INCREMENTAL
        }
        if incremental is not None:
            state['incremental'] = incremental
        stage = self.get(self.init_stage)
        handle.delay(state, stage.name, {})

        # If running in eager mode, we need to block until all the queued
        # tasks are finished.
        while not local_queue.is_empty:
            time.sleep(1)

    def replay(self, stage):
        """Re-run all tasks issued to a particular stage.

        This sort of requires a degree of idempotence for each operation.
        Usually used to re-parse a set of crawled documents.
        """
        query = Result.by_crawler_next_stage(self.name, stage)
        for result in query:
            state = {'crawler': self.name}
            handle.delay(state, stage, result.data)

    @property
    def cleanup_method(self):
        method = self.cleanup_method_name
        if ':' in method:
            package, method = method.rsplit(':', 1)
        module = import_module(package)
        return getattr(module, method)

    def cleanup(self):
        """Run a cleanup method after the crawler finishes running"""
        if settings.REDIS_HOST:
            r = redis.Redis(connection_pool=redis_pool)
            active_ops = r.get(self.name)
            if not active_ops or int(active_ops) != 0:
                log.info("Clean up did not run: Crawler %s has not run or is"
                         " currently running" % self.name)
                return
        if self.cleanup_method_name:
            log.info("Running clean up for %s" % self.name)
            self.cleanup_method()
        else:
            pass

    def get(self, name):
        return self.stages.get(name)

    def __iter__(self):
        return iter(self.stages.values())

    def __repr__(self):
        return '<Crawler(%s)>' % self.name
