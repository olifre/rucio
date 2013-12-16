# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Martin Barisits, <martin.barisits@cern.ch>, 2013
# - Mario Lassnig, <mario.lassnig@cern.ch>, 2013

"""
Judge-Evaluator is a daemon to re-evaluate and execute replication rules.
"""

import logging
import sys
import threading
import time
import traceback

from sqlalchemy.exc import DatabaseError
from sqlalchemy.sql.expression import bindparam
from sqlalchemy.sql.expression import text

from rucio.common.config import config_get
from rucio.common.exception import DatabaseException, DataIdentifierNotFound
from rucio.db import session as rucio_session
from rucio.db import models
from rucio.core.rule import re_evaluate_did
from rucio.core.monitor import record_gauge, record_counter

graceful_stop = threading.Event()

logging.basicConfig(stream=sys.stdout,
                    level=getattr(logging, config_get('common', 'loglevel').upper()),
                    format='%(asctime)s\t%(process)d\t%(levelname)s\t%(message)s')


def re_evaluator(once=False, process=0, total_processes=1, thread=0, threads_per_process=1):
    """
    Main loop to check the re-evaluation of dids.
    """

    logging.info('re_evaluator: starting')

    logging.info('re_evaluator: started')

    while not graceful_stop.is_set():
        try:
            # Select a bunch of dids for re evaluation for this worker
            session = rucio_session.get_session()
            query = session.query(models.UpdatedDID).\
                with_hint(models.UpdatedDID, "index(updated_dids UPDATED_DIDS_CREATED_AT_IDX)", 'oracle').\
                order_by(models.UpdatedDID.created_at)

            if total_processes*threads_per_process-1 > 0:
                if session.bind.dialect.name == 'oracle':
                    bindparams = [bindparam('worker_number', process*threads_per_process+thread),
                                  bindparam('total_workers', total_processes*threads_per_process-1)]
                    query = query.filter(text('ORA_HASH(name, :total_workers) = :worker_number', bindparams=bindparams))
                elif session.bind.dialect.name == 'mysql':
                    query = query.filter('mod(md5(name), %s) = %s' % (total_processes*threads_per_process-1, process*threads_per_process+thread))
                elif session.bind.dialect.name == 'postgresql':
                    query = query.filter('mod(abs((\'x\'||md5(name))::bit(32)::int), %s) = %s' % (total_processes*threads_per_process-1, process*threads_per_process+thread))

            start = time.time()  # NOQA
            dids = query.limit(1000).all()
            logging.debug('Re-Evaluation index query time %f did-size=%d' % (time.time() - start, len(dids)))

            if not dids and not once:
                logging.info('re_evaluator[%s/%s] did not get any work' % (process*threads_per_process+thread, total_processes*threads_per_process-1))
                time.sleep(10)
            else:
                record_gauge('rule.judge.re_evaluate.threads.%d' % (process*threads_per_process+thread), 1)
                done_dids = {}
                for did in dids:
                    if graceful_stop.is_set():
                        break
                    if '%s:%s' % (did.scope, did.name) in done_dids:
                        if did.rule_evaluation_action in done_dids['%s:%s' % (did.scope, did.name)]:
                            did.delete(flush=False, session=session)
                            continue
                    else:
                        done_dids['%s:%s' % (did.scope, did.name)] = []
                    done_dids['%s:%s' % (did.scope, did.name)].append(did.rule_evaluation_action)

                    try:
                        start_time = time.time()
                        re_evaluate_did(scope=did.scope, name=did.name, rule_evaluation_action=did.rule_evaluation_action)
                        logging.debug('re_evaluator[%s/%s]: evaluation of %s:%s took %f' % (process*threads_per_process+thread, total_processes*threads_per_process-1, did.scope, did.name, time.time() - start_time))
                        did.delete(flush=False, session=session)
                    except DataIdentifierNotFound, e:
                        did.delete(flush=False, session=session)
                    except (DatabaseException, DatabaseError), e:
                        record_counter('rule.judge.exceptions.%s' % e.__class__.__name__)
                        logging.warning('re_evaluator[%s/%s]: Locks detected for %s:%s' % (process*threads_per_process+thread, total_processes*threads_per_process-1, did.scope, did.name))
                record_gauge('rule.judge.re_evaluate.threads.%d' % (process*threads_per_process+thread), 0)
            session.flush()
            session.commit()
            session.remove()
        except Exception, e:
            record_counter('rule.judge.exceptions.%s' % e.__class__.__name__)
            record_gauge('rule.judge.re_evaluate.threads.%d' % (process*threads_per_process+thread), 0)
            logging.error(traceback.format_exc())
        if once:
            break

    logging.info('re_evaluator: graceful stop requested')
    record_gauge('rule.judge.re_evaluate.threads.%d' % (process*threads_per_process+thread), 0)

    logging.info('re_evaluator: graceful stop done')


def stop(signum=None, frame=None):
    """
    Graceful exit.
    """

    graceful_stop.set()


def run(once=False, process=0, total_processes=1, threads_per_process=11):
    """
    Starts up the Judge-Eval threads.
    """
    for i in xrange(process * threads_per_process, max(0, process * threads_per_process + threads_per_process - 1)):
        record_gauge('rule.judge.re_evaluate.threads.%d' % i, 0)

    if once:
        logging.info('main: executing one iteration only')
        re_evaluator(once)
    else:
        logging.info('main: starting threads')
        threads = [threading.Thread(target=re_evaluator, kwargs={'process': process, 'total_processes': total_processes, 'once': once, 'thread': i, 'threads_per_process': threads_per_process}) for i in xrange(0, threads_per_process)]
        [t.start() for t in threads]
        logging.info('main: waiting for interrupts')
        # Interruptible joins require a timeout.
        while threads[0].is_alive():
            [t.join(timeout=3.14) for t in threads]