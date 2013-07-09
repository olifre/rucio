# Copyright European Organization for Nuclear Research (CERN)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Authors:
# - Angelos Molfetas, <angelos.molfetas@cern.ch>, 2012
# - Mario Lassnig, <mario.lassnig@cern.ch>, 2012
# - Vincent Garonne,  <vincent.garonne@cern.ch>, 2011-2013


from ConfigParser import NoOptionError
from functools import wraps
from threading import Lock
from time import sleep

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.exc import DatabaseError, DisconnectionError, OperationalError, DBAPIError, TimeoutError
from sqlalchemy.ext.declarative import declarative_base


from rucio.common.config import config_get
from rucio.common.exception import RucioException, DatabaseException

BASE = declarative_base()
try:
    BASE.metadata.schema = config_get('database', 'schema')
except NoOptionError:
    pass

_SESSION, _ENGINE, _LOCK = None, None, Lock()


def _fk_pragma_on_connect(dbapi_con, con_record):
    # Hack for previous versions of sqlite3
    try:
        dbapi_con.execute('pragma foreign_keys=ON')
    except AttributeError:
        pass


def mysql_ping_listener(dbapi_conn, connection_rec, connection_proxy):
    """
    Ensures that MySQL connections checked out of the
    pool are alive.

    Borrowed from:
    http://groups.google.com/group/sqlalchemy/msg/a4ce563d802c929f

    :param dbapi_conn: DBAPI connection
    :param connection_rec: connection record
    :param connection_proxy: connection proxy
    """

    try:
        dbapi_conn.cursor().execute('select 1')
    except dbapi_conn.OperationalError, ex:
        if ex.args[0] in (2006, 2013, 2014, 2045, 2055):
            msg = 'Got mysql server has gone away: %s' % ex
            raise DisconnectionError(msg)
        else:
            raise


def get_engine(echo=True):
    """ Creates a engine to a specific database.
        :returns: engine
    """
    global _ENGINE
    if not _ENGINE:
        sql_connection = config_get('database', 'default')
        config_params = [('pool_size', int), ('max_overflow', int), ('pool_timeout', int),
                         ('pool_recycle', int), ('echo', int), ('echo_pool', str),
                         ('pool_reset_on_return', str), ('use_threadlocal', int)]
        params = {}
        for param, param_type in config_params:
            try:
                params[param] = param_type(config_get('database', param))
            except NoOptionError:
                pass
        _ENGINE = create_engine(sql_connection, **params)
        if 'mysql' in sql_connection:
            event.listen(_ENGINE, 'checkout', mysql_ping_listener)
        if 'sqlite' in sql_connection:
            event.listen(_ENGINE, 'connect', _fk_pragma_on_connect)
        # Override engine.connect method with db error wrapper
        # To have auto_reconnect (will come in next sqlalchemy releases)
        _ENGINE.connect = wrap_db_error(_ENGINE.connect)
        _ENGINE.connect()
    assert(_ENGINE)
    return _ENGINE


def get_dump_engine(echo=False):
    """ Creates a dump engine to a specific database.
        :returns: engine """

    statements = list()

    def dump(sql, *multiparams, **params):
        statement = str(sql.compile(dialect=engine.dialect))
        if statement in statements:
            return
        statements.append(statement)
        if statement.endswith(')\n\n'):
            if engine.dialect.name == 'oracle':
                print statement.replace(')\n\n', ') PCTFREE 0;\n')
            else:
                print statement.replace(')\n\n', ');\n')
        elif statement.endswith(')'):
            print statement.replace(')', ');\n')
        else:
            print statement
    sql_connection = config_get('database', 'default')

    engine = create_engine(sql_connection, echo=echo, strategy='mock', executor=dump)
    return engine


def is_db_connection_error(args):
    """Return True if error in connecting to db."""
    conn_err_codes = (  # MySQL
                        '2002', '2003', '2006',
                        # Oracle
                        'ORA-00028',  # session has been killed
                        'ORA-01012',  # not logged on
                        'ORA-03113',  # end-of-file on communication channel
                        'ORA-03114',  # not connected to ORACLE
                        'ORA-03135',  # connection lost contact
                        'ORA-25408',)  # can not safely replay call

    for err_code in conn_err_codes:
        if args.find(err_code) != -1:
            return True
    return False


def wrap_db_error(f):
    """Retry DB connection."""
    def _wrap(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except DatabaseError, e:
            raise RucioException(e.args[0])
        except OperationalError, e:
            if not is_db_connection_error(e.args[0]):
                raise
            # To Do: Should come from the configuration
            remaining_attempts = 10
            retry_interval = 0.5
            while True:
                print 'SQL connection failed. %d attempts left.' % remaining_attempts
                remaining_attempts -= 1
                sleep(retry_interval)
                try:
                    return f(*args, **kwargs)
                except OperationalError, e:
                    if (remaining_attempts == 0 or not is_db_connection_error(e.args[0])):
                        raise
                except DBAPIError:
                    raise
        except DBAPIError:
            raise
    _wrap.func_name = f.func_name
    return _wrap


def get_session():
    """ Creates a session to a specific database, assumes that schema already in place.
        :returns: session
    """
    global _SESSION, _LOCK
    if not _SESSION:
        _LOCK.acquire()
        try:
            get_engine()
            _get_session()
        finally:
            _LOCK.release()
    assert _SESSION
    return _SESSION


def _get_session(autocommit=False, autoflush=True, expire_on_commit=True):
    """Internal method to grab session"""
    global _SESSION, _ENGINE
    if not _SESSION:
        assert _ENGINE
        _SESSION = scoped_session(sessionmaker(bind=_ENGINE, autocommit=autocommit, autoflush=autoflush, expire_on_commit=expire_on_commit))
    assert _SESSION


def read_session(function):
    '''
    decorator that set the session variable to use inside a function.
    With that decorator it's possible to use the session variable like if a global variable session is declared.

    session is a sqlalchemy session, and you can get one calling get_session().
    This is useful if only SELECTs and the like are being done; anything involving
    INSERTs, UPDATEs etc should use transactional_session.
    '''
    @wraps(function)
    def new_funct(*args, **kwargs):
        s = kwargs.get('session', '')
        if not s:
            session = get_session()
            try:
                kwargs['session'] = session
                result = function(*args, **kwargs)
            except TimeoutError, e:
                print e
                session.rollback()
                raise DatabaseException(str(e))
            except DatabaseError, e:
                print e
                session.rollback()
                raise DatabaseException(str(e))
            except:
                session.rollback()
                raise
            finally:
                session.remove()
        else:
            result = function(*args, **kwargs)
        return result
    new_funct.__doc__ = function.__doc__
    return new_funct


def transactional_session(function):
    '''
    decorator that set the session variable to use inside a function.
    With that decorator it's possible to use the session variable like if a global variable session is declared.

    session is a sqlalchemy session, and you can get one calling get_session().
    '''
    @wraps(function)
    def new_funct(*args, **kwargs):
        s = kwargs.get('session', '')
        if not s:
            session = get_session()
            # session.begin(subtransactions=True)
            try:
                kwargs['session'] = session
                result = function(*args, **kwargs)
            except TimeoutError, e:
                print e
                session.rollback()
                raise DatabaseException(str(e))
            except DatabaseError, e:
                print e
                session.rollback()
                raise DatabaseException(str(e))
            except:
                session.rollback()
                raise
            else:
                session.commit()
            finally:
                session.remove()
        else:
            result = function(*args, **kwargs)
        return result
    new_funct.__doc__ = function.__doc__
    return new_funct


def in_transaction(nested=False):
    '''
    decorator that set the session variable to use inside a function.
    With that decorator it's possible to use the session variable like if a global variable session is declared.

    session is a sqlalchemy session, and you can get one calling get_session().
    '''
    def decorator(function):
        @wraps(function)
        def new_funct(*args, **kwargs):
            s = kwargs.get('session', None)
            if not s:
                session = get_session()
                if nested:
                    session.begin(subtransactions=True)
                try:
                    kwargs['session'] = session
                    result = function(*args, **kwargs)
                except TimeoutError, e:
                    session.rollback()
                    raise DatabaseException(str(e))
                except:
                    session.rollback()
                    raise
                else:
                    session.commit()
                finally:
                    session.close()
            else:
                result = function(*args, **kwargs)
            return result
        new_funct.__doc__ = function.__doc__
        return new_funct
    return decorator
