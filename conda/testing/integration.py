# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
"""
These helpers were originally defined in tests/test_create.py,
but were refactored here so downstream projects can benefit from
them too.
"""

import json
import os
import sys
from contextlib import contextmanager
from functools import lru_cache
from logging import getLogger
from os.path import dirname, exists, isdir, join, lexists
from random import sample
from shutil import copyfile, rmtree
from subprocess import check_output
from tempfile import gettempdir
from uuid import uuid4

import pytest

from conda.auxlib.compat import Utf8NamedTemporaryFile
from conda.auxlib.entity import EntityEncoder
from conda.base.constants import PACKAGE_CACHE_MAGIC_FILE
from conda.base.context import conda_tests_ctxt_mgmt_def_pol, context, reset_context
from conda.cli.conda_argparse import do_call, generate_parser
from conda.cli.main import init_loggers
from conda.common.compat import encode_arguments, on_win
from conda.common.io import (
    argv,
    captured,
    dashlist,
    disable_logger,
    env_var,
    stderr_log_level,
)
from conda.common.url import path_to_url
from conda.core.package_cache_data import PackageCacheData
from conda.core.prefix_data import PrefixData
from conda.deprecations import deprecated
from conda.exceptions import conda_exception_handler
from conda.gateways.disk.create import mkdir_p
from conda.gateways.disk.delete import rm_rf
from conda.gateways.disk.link import link
from conda.gateways.disk.update import touch
from conda.gateways.logging import DEBUG
from conda.models.match_spec import MatchSpec
from conda.models.records import PackageRecord
from conda.utils import massage_arguments

TEST_LOG_LEVEL = DEBUG
PYTHON_BINARY = "python.exe" if on_win else "bin/python"
BIN_DIRECTORY = "Scripts" if on_win else "bin"
UNICODE_CHARACTERS = "ōγђ家固한áêñßôç"
# UNICODE_CHARACTERS_RESTRICTED = u"áêñßôç"
UNICODE_CHARACTERS_RESTRICTED = "abcdef"
which_or_where = "which" if not on_win else "where"
cp_or_copy = "cp" if not on_win else "copy"
env_or_set = "env" if not on_win else "set"

# UNICODE_CHARACTERS = u"12345678abcdef"
# UNICODE_CHARACTERS_RESTRICTED = UNICODE_CHARACTERS

# When testing for bugs, you may want to change this to a _,
# for example to see if a bug is related to spaces in prefixes.
SPACER_CHARACTER = " "

log = getLogger(__name__)


def escape_for_winpath(p):
    return p.replace("\\", "\\\\")


@lru_cache(maxsize=None)
def running_a_python_capable_of_unicode_subprocessing():
    name = None
    # try:
    # UNICODE_CHARACTERS + os.sep +
    with Utf8NamedTemporaryFile(
        mode="w", suffix=UNICODE_CHARACTERS + ".bat", delete=False
    ) as batch_file:
        batch_file.write("@echo Hello World\n")
        batch_file.write("@exit 0\n")
        name = batch_file.name
    if name:
        try:
            out = check_output(name, cwd=dirname(name), stderr=None, shell=False)
            out = out.decode("utf-8") if hasattr(out, "decode") else out
            if out.startswith("Hello World"):
                return True
            return False
        except Exception:
            return False
        finally:
            os.unlink(name)
    return False


tmpdir_in_use = None


@pytest.fixture(autouse=True)
def set_tmpdir(tmpdir):
    global tmpdir_in_use
    if not tmpdir:
        return tmpdir_in_use
    td = tmpdir.strpath
    assert os.sep in td
    tmpdir_in_use = td


def _get_temp_prefix(name=None, use_restricted_unicode=False):
    tmpdir = tmpdir_in_use or gettempdir()
    capable = running_a_python_capable_of_unicode_subprocessing()

    if not capable or use_restricted_unicode:
        RESTRICTED = UNICODE_CHARACTERS_RESTRICTED
        random_unicode = "".join(sample(RESTRICTED, len(RESTRICTED)))
    else:
        random_unicode = "".join(sample(UNICODE_CHARACTERS, len(UNICODE_CHARACTERS)))
    tmpdir_name = os.environ.get(
        "CONDA_TEST_TMPDIR_NAME",
        (str(uuid4())[:4] + SPACER_CHARACTER + random_unicode)
        if name is None
        else name,
    )
    prefix = join(tmpdir, tmpdir_name)

    # Exit immediately if we cannot use hardlinks, on Windows, we get permissions errors if we use
    # sys.executable so instead use the pdb files.
    src = sys.executable.replace(".exe", ".pdb") if on_win else sys.executable
    dst = os.path.join(tmpdir, os.path.basename(sys.executable))

    try:
        link(src, dst)
    except OSError:
        print(
            "\nWARNING :: You are testing `conda` with `tmpdir`:-\n           {}\n"
            "           not on the same FS as `sys.prefix`:\n           {}\n"
            "           this will be slow and unlike the majority of end-user installs.\n"
            "           Please pass `--basetemp=<somewhere-else>` instead.".format(
                tmpdir, sys.prefix
            )
        )
    try:
        rm_rf(dst)
    except Exception as e:
        print(e)
        pass

    return prefix


def make_temp_prefix(name=None, use_restricted_unicode=False, _temp_prefix=None):
    """
    When the env. you are creating will be used to install Python 2.7 on Windows
    only a restricted amount of Unicode will work, and probably only those chars
    in your current codepage, so the characters in UNICODE_CHARACTERS_RESTRICTED
    should probably be randomly generated from that instead. The problem here is
    that the current codepage needs to be able to handle 'sys.prefix' otherwise
    ntpath will fall over.
    """
    if not _temp_prefix:
        _temp_prefix = _get_temp_prefix(
            name=name, use_restricted_unicode=use_restricted_unicode
        )
    try:
        os.makedirs(_temp_prefix)
    except:
        pass
    assert isdir(_temp_prefix)
    return _temp_prefix


def FORCE_temp_prefix(name=None, use_restricted_unicode=False):
    _temp_prefix = _get_temp_prefix(
        name=name, use_restricted_unicode=use_restricted_unicode
    )
    rm_rf(_temp_prefix)
    os.makedirs(_temp_prefix)
    assert isdir(_temp_prefix)
    return _temp_prefix


class Commands:
    COMPARE = "compare"
    CONFIG = "config"
    CLEAN = "clean"
    CREATE = "create"
    INFO = "info"
    INSTALL = "install"
    LIST = "list"
    REMOVE = "remove"
    SEARCH = "search"
    UPDATE = "update"
    RUN = "run"


@deprecated("23.9", "24.3", addendum="Use `monkeypatch.chdir` instead.")
@contextmanager
def temp_chdir(target_dir):
    curdir = os.getcwd()
    if not target_dir:
        target_dir = curdir
    try:
        os.chdir(target_dir)
        yield
    finally:
        os.chdir(curdir)


@deprecated("23.9", "24.3", addendum="Use `conda.testing.conda_cli` instead.")
def run_command(command, prefix, *arguments, **kwargs):
    assert isinstance(arguments, tuple), "run_command() arguments must be tuples"
    arguments = massage_arguments(arguments)

    use_exception_handler = kwargs.get("use_exception_handler", False)
    # These commands require 'dev' mode to be enabled during testing because
    # they end up calling run_script() in link.py and that uses wrapper scripts for e.g. activate.
    # Setting `dev` means that, in these scripts, conda is executed via:
    #   `sys.prefix/bin/python -m conda` (or the Windows equivalent).
    # .. and the source code for `conda` is put on `sys.path` via `PYTHONPATH` (a bit gross but
    # less so than always requiring `cwd` to be the root of the conda source tree in every case).
    # If you do not want this to happen for some test you must pass dev=False as a kwarg, though
    # for nearly all tests, you want to make sure you are running *this* conda and not some old
    # conda (it was random which you'd get depending on the initial values of PATH and PYTHONPATH
    # - and likely more variables - before `dev` came along). Setting CONDA_EXE is not enough
    # either because in the 4.5 days that would just run whatever Python was found first on PATH.
    command_defaults_to_dev = command in (
        Commands.CREATE,
        Commands.INSTALL,
        Commands.REMOVE,
        Commands.RUN,
    )
    dev = kwargs.get("dev", True if command_defaults_to_dev else False)
    debug = kwargs.get("debug_wrapper_scripts", False)

    p = generate_parser()

    if command is Commands.CONFIG:
        arguments.append("--file")
        arguments.append(join(prefix, "condarc"))
    if command in (
        Commands.LIST,
        Commands.COMPARE,
        Commands.CREATE,
        Commands.INSTALL,
        Commands.REMOVE,
        Commands.UPDATE,
        Commands.RUN,
    ):
        arguments.insert(0, "-p")
        arguments.insert(1, prefix)
    if command in (Commands.CREATE, Commands.INSTALL, Commands.REMOVE, Commands.UPDATE):
        arguments.extend(["-y", "-q"])

    arguments.insert(0, command)
    if dev:
        arguments.insert(1, "--dev")
    if debug:
        arguments.insert(1, "--debug-wrapper-scripts")

    # It would be nice at this point to re-use:
    # from conda.cli.python_api import run_command as python_api_run_command
    # python_api_run_command
    # .. but that does not support no_capture and probably more stuff.

    args = p.parse_args(arguments)
    context._set_argparse_args(args)
    init_loggers()
    cap_args = () if not kwargs.get("no_capture") else (None, None)
    # list2cmdline is not exact, but it is only informational.
    print(
        "\n\nEXECUTING COMMAND >>> $ conda %s\n\n" % " ".join(arguments),
        file=sys.stderr,
    )
    with stderr_log_level(TEST_LOG_LEVEL, "conda"), stderr_log_level(
        TEST_LOG_LEVEL, "requests"
    ):
        arguments = encode_arguments(arguments)
        with argv(["python_api"] + arguments), captured(*cap_args) as c:
            if use_exception_handler:
                result = conda_exception_handler(do_call, args, p)
            else:
                result = do_call(args, p)
        stdout = c.stdout
        stderr = c.stderr
        print(stdout, file=sys.stdout)
        print(stderr, file=sys.stderr)

    # Unfortunately there are other ways to change context, such as Commands.CREATE --offline.
    # You will probably end up playing whack-a-bug here adding more and more the tuple here.
    if command in (Commands.CONFIG,):
        reset_context([os.path.join(prefix + os.sep, "condarc")], args)
    return stdout, stderr, result


@contextmanager
def make_temp_env(*packages, **kwargs):
    name = kwargs.pop("name", None)
    use_restricted_unicode = kwargs.pop("use_restricted_unicode", False)

    prefix = kwargs.pop("prefix", None) or _get_temp_prefix(
        name=name, use_restricted_unicode=use_restricted_unicode
    )
    clean_prefix = kwargs.pop("clean_prefix", None)
    if clean_prefix:
        if os.path.exists(prefix):
            rm_rf(prefix)
    if not isdir(prefix):
        make_temp_prefix(name, use_restricted_unicode, prefix)
    with disable_logger("fetch"):
        try:
            # try to clear any config that's been set by other tests
            # CAUTION :: This does not partake in the context stack management code
            #            of env_{var,vars,unmodified} and, when used in conjunction
            #            with that code, this *must* be called first.
            reset_context([os.path.join(prefix + os.sep, "condarc")])
            run_command(Commands.CREATE, prefix, *packages, **kwargs)
            yield prefix
        finally:
            if "CONDA_TEST_SAVE_TEMPS" not in os.environ:
                rmtree(prefix, ignore_errors=True)
            else:
                log.warning(
                    f"CONDA_TEST_SAVE_TEMPS :: retaining make_temp_env {prefix}"
                )


@contextmanager
def make_temp_package_cache():
    prefix = make_temp_prefix(use_restricted_unicode=on_win)
    pkgs_dir = join(prefix, "pkgs")
    mkdir_p(pkgs_dir)
    touch(join(pkgs_dir, PACKAGE_CACHE_MAGIC_FILE))

    try:
        with env_var(
            "CONDA_PKGS_DIRS", pkgs_dir, stack_callback=conda_tests_ctxt_mgmt_def_pol
        ):
            assert context.pkgs_dirs == (pkgs_dir,)
            yield pkgs_dir
    finally:
        rmtree(prefix, ignore_errors=True)
        if pkgs_dir in PackageCacheData._cache_:
            del PackageCacheData._cache_[pkgs_dir]


@contextmanager
def make_temp_channel(packages):
    package_reqs = [pkg.replace("-", "=") for pkg in packages]
    package_names = [pkg.split("-")[0] for pkg in packages]

    with make_temp_env(*package_reqs) as prefix:
        for package in packages:
            assert package_is_installed(prefix, package.replace("-", "="))
        data = [
            p for p in PrefixData(prefix).iter_records() if p["name"] in package_names
        ]
        run_command(Commands.REMOVE, prefix, *package_names)
        for package in packages:
            assert not package_is_installed(prefix, package.replace("-", "="))

    repodata = {"info": {}, "packages": {}}
    tarfiles = {}
    for package_data in data:
        pkg_data = package_data
        fname = pkg_data["fn"]
        tarfiles[fname] = join(PackageCacheData.first_writable().pkgs_dir, fname)

        pkg_data = pkg_data.dump()
        for field in ("url", "channel", "schannel"):
            pkg_data.pop(field, None)
        repodata["packages"][fname] = PackageRecord(**pkg_data)

    with make_temp_env() as channel:
        subchan = join(channel, context.subdir)
        noarch_dir = join(channel, "noarch")
        channel = path_to_url(channel)
        os.makedirs(subchan)
        os.makedirs(noarch_dir)
        for fname, tar_old_path in tarfiles.items():
            tar_new_path = join(subchan, fname)
            copyfile(tar_old_path, tar_new_path)

        with open(join(subchan, "repodata.json"), "w") as f:
            f.write(json.dumps(repodata, cls=EntityEncoder))
        with open(join(noarch_dir, "repodata.json"), "w") as f:
            f.write(json.dumps({}, cls=EntityEncoder))

        yield channel


def create_temp_location():
    return _get_temp_prefix()


@contextmanager
def tempdir():
    prefix = create_temp_location()
    try:
        os.makedirs(prefix)
        yield prefix
    finally:
        if lexists(prefix):
            rm_rf(prefix)


def reload_config(prefix):
    prefix_condarc = join(prefix + os.sep, "condarc")
    reset_context([prefix_condarc])


def package_is_installed(prefix, spec):
    spec = MatchSpec(spec)
    prefix_recs = tuple(PrefixData(prefix).query(spec))
    if len(prefix_recs) > 1:
        raise AssertionError(
            "Multiple packages installed.%s"
            % (dashlist(prec.dist_str() for prec in prefix_recs))
        )
    return bool(len(prefix_recs))


@deprecated(
    "23.9",
    "24.3",
    addendum="Use `conda.core.prefix_data.PrefixData().get()` instead.",
)
def get_conda_list_tuple(prefix, package_name):
    stdout, stderr, _ = run_command(Commands.LIST, prefix)
    stdout_lines = stdout.split("\n")
    package_line = next(
        (line for line in stdout_lines if line.lower().startswith(package_name + " ")),
        None,
    )
    return package_line.split()


def get_shortcut_dir():
    assert on_win
    user_mode = "user" if exists(join(sys.prefix, ".nonadmin")) else "system"
    try:
        from menuinst.win32 import dirs_src as win_locations

        return win_locations[user_mode]["start"][0]
    except ImportError:
        try:
            from menuinst.win32 import dirs as win_locations

            return win_locations[user_mode]["start"]
        except ImportError:
            raise
