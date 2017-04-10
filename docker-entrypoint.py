#!/usr/bin/env python2

from __future__ import print_function

from codecs import open
from collections import namedtuple
from os import path
from subprocess import call, check_call, check_output, Popen, PIPE, CalledProcessError

import os
import shlex
import sys
import itertools

# Global Constants
UID = int(check_output(['id', '-u']))

PGDATA = os.environ['PGDATA']
PGUID = "postgres"
PGRUNDIR = '/var/run/postgresql'

# This is needed for `pg_ctl`
if 'PGUSER' not in os.environ:
    os.environ['PGUSER'] = 'postgres'

PGConfig = namedtuple('PGConfig', ['init_args', 'db_config', 'postgres_password', 'users', 'databases'])
PGUser = namedtuple('PGUser', ['name', 'password', 'superuser'])
PGDatabase = namedtuple('PGDatabase', ['name', 'owner'])
PGDbConf = namedtuple('PGDbConf', ['name', 'value'])


def load_config():
    """
    This function loads and parses the database / user configuration for this postgres instance.

    Any environment variable $ENV read may also be set by pointing $ENV_FILE to a file with the
    contents of that environment variable.

    For historical reasons, user information is read from multiple places, as documented below.

    Each configured user may be associated with a password and / or configured as a superuser.
    To allow a user access without a password, set the password to the empty string. Not setting
    the password won't allow the user to login at all.

    Access to the `public` schema for all database is revoked and granted only to the owner of
    that database.

    Note: The user 'postgres' is always a superuser. The database 'postgres'
          always exists and it's owner will always be the 'postgres' user

    1. POSTGRES_USER, POSTGRES_DB=$POSTGRES_USER, POSTGRES_PASSWORD
        Configures a single user and grants access to the specific database.

        Always configured as a superuser, _USER defaults to `postgres`

    2. POSTGRES_USERS, POSTGRES_DATABASES
        Configures multiple users and databases. Prefix a username with ! to create a superuser.

        Example:

        POSTGRES_USERS=user1:pass1|!user2:
        POSTGRES_DATABASES=db1:user1|db2:user1|db3

        This will create `user1` allowing login with `pass1` and `user2` as a superuser which may
        log in without a password. Furthermore, the databases db1 and db2 are created with user1
        as the owner and the database db3 is created with `postgres` as an owner.

    3. POSTGRES_USER_0, POSTGRES_PASSWORD_0, POSTGRES_DATABASES_0, POSTGRES_SUPERUSER_0
        Configures multiple users and databases. The variables may be set any number of times,
        consecutively numbered from 0. This is helpful when only one piece of data can be specified
        per variable. Set POSTGRES_SUPERUSER_0 to 1 to create a superuser.

        Example, which produces the same result as the one above:

        POSTGRES_USER_0=user1
        POSTGRES_PASSWORD_0=pass1
        POSTGRES_DATABASES_0=db1|db2

        POSTGRES_USER_1=user2
        POSTGRES_PASSWORD_1=
        POSTGRES_SUPERUSER_1=1

        POSTGRES_DATABASES=db3          # create a database without an owner by using 2.
        POSTGRES_USER_3=foo             # ignored, since POSTGRES_USER_2 was not specified.
    """

    seen_users = set()
    seen_databases = set()

    # One element array do allow modification in inner function
    postgres_password = [None]

    databases = []
    users = []

    init_args = shlex.split(file_env('POSTGRES_INITDB_ARGS', default=''))

    db_config = []
    double_split_apply('POSTGRES_CONFIGS', lambda name, value: db_config.append(PGDbConf(name, value)))

    def add_user(name, password, superuser):
        if name in seen_users:
            error_exit('user {} registered twice'.format(name))

        if name == 'postgres':
            if not superuser:
                error_exit('cannot change postgres to a non-superuser')

            postgres_password[0] = password
        else:
            users.append(PGUser(name, password, superuser))

        seen_users.add(name)

    def add_database(name, owner):
        if name == 'postgres':
            if owner is not None and owner != 'postgres':
                error_exit('cannot change owner of database postgres to {}'.format(owner))

            return  # nothing to be done for postgres

        if name in seen_databases:
            error_exit('database {} registered twice'.format(name))

        databases.append(PGDatabase(name, owner))
        seen_databases.add(name)

    def add_user_from_multi(name, password):
        superuser = False

        if name.startswith('!'):
            name = name[1:].strip()
            superuser = True

        add_user(name, password, superuser)

    if file_env('POSTGRES_USER') is not None:
        add_user(file_env('POSTGRES_USER'), file_env('POSTGRES_PASSWORD'), superuser=True)

    if file_env('POSTGRES_DATABASE') is not None:
        add_database(file_env('POSTGRES_USER'), file_env('POSTGRES_USER'))

    double_split_apply('POSTGRES_USERS', add_user_from_multi, 2)

    double_split_apply('POSTGRES_DATABASES', add_database, 2)

    for i in itertools.count():
        pg_user_x = 'POSTGRES_USER_{}'.format(i)
        pg_pw_x = 'POSTGRES_PASSWORD_{}'.format(i)
        pg_super_x = 'POSTGRES_SUPERUSER_{}'.format(i)
        pg_databases_x = 'POSTGRES_DATABASES_{}'.format(i)

        if file_env(pg_user_x) is None:
            break

        user = file_env(pg_user_x)

        add_user(user, file_env(pg_pw_x), file_env(pg_super_x) == '1')

        for db in (db.strip() for db in file_env(pg_databases_x, '').split('|') if db):
            add_database(db, user)

    return PGConfig(init_args, db_config, postgres_password[0], users, databases)


ENV_CACHE = {}


def file_env(var, default=None):
    """
    Read a value from the environment.

    The value is read either from the environment variable specified by `var` or from the file
    given by the environment variable `$var_FILE`. Only on of both environment variables may be set.

    Read values will be stripped / trimmed before being returned.

    If neither variable is set, `default` is returned.

    All values are cached once loaded.
    """

    def inner():
        file_var = '{}_FILE'.format(var)

        if var in os.environ and file_var in os.environ:
            error_exit("{} and {} were set, only one allowed".format(var, file_var))

        if var in os.environ:
            return os.environ[var].strip()

        if file_var in os.environ:
            with open(os.environ[file_var], mode='rt', encoding='utf8') as f:
                return f.read().strip()

        return default

    global ENV_CACHE

    if var not in ENV_CACHE:
        ENV_CACHE[var] = inner()

    return ENV_CACHE[var]


def print_trust_warning():
    WARNING = '''
****************************************************
WARNING: No password has been set for the database.
         This will allow anyone with access to the
         Postgres port to access your database. In
         Docker's default configuration, this is
         effectively any other container on the same
         system.

         Use "-e POSTGRES_PASSWORD=password" to set
         it in "docker run".
****************************************************
'''

    print(WARNING, file=sys.stderr)


def init_with_root():
    check_call(['mkdir', '-p', PGDATA])
    check_call(['chown', '-R', PGUID, PGDATA])
    check_call(['chmod', '700', PGDATA])

    check_call(['mkdir', '-p', PGRUNDIR])
    check_call(['chown', '-R', PGUID, PGRUNDIR])
    check_call(['chmod', 'g+s', PGRUNDIR])

    os.execlp('gosu', 'gosu', PGUID, *sys.argv)


def setup():
    check_call(['mkdir', '-p', PGDATA])

    with open(os.devnull, mode='wb') as devnull:
        call(['chown', '-R', PGUID, PGDATA], stderr=devnull)
        call(['chmod', '700', PGDATA], stderr=devnull)

    if path.isfile(path.join(PGDATA, 'PG_VERSION')):
        print("database already created, skipping setup")
        return

    config = load_config()

    check_call(['initdb', '--username=postgres'] + config.init_args)

    with open(path.join(PGDATA, 'pg_hba.conf'), mode='at', encoding='ascii') as pg_hba:
        def add_trust(name):
            pg_hba.write('host all {} all trust\n'.format(name))

        any_trust = False

        if config.postgres_password == '':
            add_trust('postgres')
            any_trust = True

        for user_config in config.users:
            if user_config.password == '':
                add_trust(user_config.name)
                any_trust = True

        # Allow everyone md5 authentication
        pg_hba.write('host all all all md5\n')

    if any_trust:
        print_trust_warning()

    check_call(['pg_ctl', '-D', PGDATA, '-o', "-c listen_addresses='localhost'", '-w', 'start'])

    if config.postgres_password:
        psql('ALTER USER "postgres" WITH SUPERUSER PASSWORD \'{}\'', config.postgres_password)

    for user_config in config.users:
        pass_clause = 'PASSWORD \'{}\''.format(user_config.password) if user_config.password else ''
        super_clause = 'SUPERUSER' if user_config.superuser else ''
        psql('CREATE USER "{}" WITH {} {}', user_config.name, pass_clause, super_clause)

    psql_db('postgres', 'REVOKE ALL ON schema public FROM public')
    # postgres is a superuser and owner of postgres, no need to GRANT again

    for db_config in config.databases:
        owner_clause = 'WITH OWNER "{}"'.format(db_config.owner) if db_config.owner else ''
        psql('CREATE DATABASE "{}" {}', db_config.name, owner_clause)
        psql_db(db_config.name, 'REVOKE ALL ON schema public FROM public')

        if db_config.owner:
            psql_db(db_config.name, 'GRANT ALL ON schema public TO "{}"', db_config.owner)

    for setting in config.db_config:
        psql('ALTER SYSTEM SET {} = "{}"', setting.name, setting.value)

    check_call(['pg_ctl', '-D', PGDATA, '-m', 'fast', '-w', 'stop'])

    print('\nPostgreSQL init process complete; ready for start up.\n')


def start():
    os.execlp(sys.argv[1], *sys.argv[1:])


def main():
    if len(sys.argv) < 2:
        error_exit("No command given. At least 'postgres' must be specified to start the database")

    # In the name of backward compatibility:
    if sys.argv[1][1] == '-':
        sys.argv[1:] = ['postgres'] + sys.argv[1:]

    cmd = sys.argv[1]

    if cmd == 'postgres' and UID == 0:
        init_with_root()

    if cmd == 'postgres':
        setup()

    start()


# ------------------------------------ Start Utility Functions ------------------------------------

def double_split_apply(env, func, pad_len=0):
    for spec in (spec.strip() for spec in file_env(env, default='').split('|') if spec):
        func(*rpad((p.strip() for p in spec.split(':')), pad_len, None))


def error_exit(*args, **kwargs):
    """Print the given message to stderr and exit with an exit code of 1."""
    print(*args, file=sys.stderr, **kwargs)
    sys.exit(1)


def rpad(lst, n, pad):
    lst = list(lst)
    if len(lst) >= n:
        return lst
    return lst + [pad] * (n - len(lst))


def check_call_pipe(*args, **kwargs):
    if 'input' not in kwargs:
        return check_call(*args, **kwargs)

    input_str = kwargs['input']
    del kwargs['input']

    assert 'stdin' not in kwargs, "stdin may not be used with check_call_pipe"

    p = Popen(*args, stdin=PIPE, **kwargs)
    p.stdin.write(input_str)
    p.stdin.close()

    retcode = p.wait()

    # From subprocess.check_call
    if retcode:
        cmd = kwargs.get("args")
        if cmd is None:
            cmd = args[0]
        raise CalledProcessError(retcode, cmd)


def _psql(sql, args):
    check_call_pipe(['psql', '-v', 'ON_ERROR_STOP=1'] + args, input=sql)


def psql(sql, *args, **kwargs):
    _psql(sql.format(*args, **kwargs), ['--username', 'postgres'])


def psql_db(db, sql, *args, **kwargs):
    _psql(sql.format(*args, **kwargs), ['--username', 'postgres', '--dbname', db])


# ------------------------------------ End Utility Functions ------------------------------------

if __name__ == '__main__':
    main()
