# -*- coding: utf-8 -*-
import logging
import string
import sys
from shlex import quote as shell_quote
from socket import gethostname
from subprocess import check_output, CalledProcessError

from configobj import ConfigObj
from filelock import FileLock
from foolscrate.git import Git
from os import access, R_OK, W_OK, X_OK
from os.path import expanduser, join, abspath, exists, dirname
from random import choice
from re import compile as re_compile, DOTALL as RE_DOTALL
from tempfile import NamedTemporaryFile

LOCKFILE_NAME = '.foolscrate.lock'
CONFLICT_STRING = 'CONFLICT_MUST_MANUALLY_MERGE'
GITIGNORE = '.gitignore'
FOOLSCRATE_CONFIG_PATH = join(expanduser("~"), ".foolscrate.conf")
FOOLSCRATE_CONFIG_LOCK = FOOLSCRATE_CONFIG_PATH + '.lock'
FOOLSCRATE_CRONTAB_COMMENT = '# foolscrate sync cronjob'

class SyncError(Exception):
    def __init__(self, directory):
        super().__init__("Could not sync '{}'".format(directory))


class Repository(object):
    _logger = logging.getLogger("Repository")
    _track_lock = FileLock(FOOLSCRATE_CONFIG_LOCK)

    @classmethod
    def create_new(cls, local_directory, remote_url):
        cls._logger.info("Will create new foolscrate-enabled repository in local directory. Remote %s should exist and be empty.", remote_url)

        if exists(join(local_directory, ".git")):
            raise ValueError("Preexisting git repo found")

        git = Git.init(local_directory)
        with open(join(local_directory, GITIGNORE), "a", encoding="utf-8") as f:
            f.write(CONFLICT_STRING)
            f.write(LOCKFILE_NAME)

        git.cmd("remote", "add", "foolscrate", remote_url)
        git.cmd("add", GITIGNORE)
        git.cmd("commit", "-m", "enabling foolscrate")

        return cls.configure_repository(git, local_directory)

    @classmethod
    def configure_repository(cls, git, local_directory):
        client_id = cls.configure_client_id(git)
        cls._align_client_ref_to_master(git, client_id)
        git.cmd("push", "-u", "foolscrate", "master", client_id)
        repo = Repository(local_directory)
        repo.track()
        return repo

    @classmethod
    def connect_existing(cls, local_directory, remote_url):
        cls._logger.info("Will create new git repo in local directory and connect to remote existing foolscrate repository %s", remote_url)

        if exists(join(local_directory, ".git")):
            raise ValueError("Preexisting git repo found")

        git = Git.init(local_directory)
        git.cmd("remote", "add", "foolscrate", remote_url)
        git.cmd("fetch", "--all")
        git.cmd("checkout", "master")

        return cls.configure_repository(git, local_directory)

    def __init__(self, local_directory):
        abs_local_directory = abspath(local_directory)

        if not (
            exists(abs_local_directory) and
            access(abs_local_directory, R_OK | W_OK | X_OK) and
            exists(join(abs_local_directory, ".git"))
                ):
            raise ValueError("{} is not a valid foolscrate-enabled repository".format(abs_local_directory))

        # TODO: what was that alan-mayday error?

        self._git = Git(abs_local_directory)
        self.localdir = abs_local_directory
        self._conflict_string = join(abs_local_directory, CONFLICT_STRING)
        self.client_id = self._git.cmd("config", "--local", "--get", "foolscrate.client-id").strip()
        self._sync_lock = FileLock(join(self.localdir, LOCKFILE_NAME))

    def sync(self):
        # TODO: probably we should sleep a little between merging attempts
        with self._sync_lock.acquire(timeout=60):
            if exists(CONFLICT_STRING):
                self._logger.info("Conflict found, not syncing")
                raise ValueError("Conflict found, not syncing")

            # begin
            for attempt in range(0, 5):
                self._logger.debug("Merge attempt n. %s", attempt)
                self._git.cmd("fetch", "--all")
                self._git.cmd("add", "-A")
                any_change = self._git.cmd("diff", "--staged").strip()

                if any_change != "":
                    self._git.cmd("commit", "-m", "Automatic foolscrate commit")

                try:
                    self._git.cmd("merge", "--no-edit", "foolscrate/master")
                except Exception as e:
                    self._logger.exception("Error while merging, aborting merge")
                    self._git.cmd("merge", "--abort")
                    continue

                self._align_client_ref_to_master(self._git, self.client_id)

                try:
                    self._git.cmd("push", "foolscrate", "master", self.client_id)
                except Exception as e:
                    self._logger.exception("Error while pushing")
                    continue

                break
            else:
                self._logger.error("Couldn't succeed at merging or pushing back our changes, probably we've got a conflict")
                with open(self._conflict_string, "w") as f:
                    pass
                raise SyncError(self.localdir)

            self._logger.info("Sync succeeded")

    def track(self):
        with self._track_lock.acquire(timeout=60):
            cfg = ConfigObj(FOOLSCRATE_CONFIG_PATH, unrepr=True, write_empty_values=True)
            # configobj doesn't support sets natively, only lists.
            track = cfg.get("track", [])
            track.append(self.localdir)
            cfg["track"] = list(set(track))
            cfg.write()

    def untrack(self):
        with self._track_lock.acquire(timeout=60):
            cfg = ConfigObj(FOOLSCRATE_CONFIG_PATH, unrepr=True, write_empty_values=True)
            cfg.setdefault("track", []).remove(self.localdir)
            cfg.write()

    @classmethod
    def configure_client_id(cls, git):
      client_id = 'foolscrate-' + gethostname() + "-" + "".join(choice(string.ascii_lowercase + string.digits) for _ in range(5))
      git.cmd('config', '--local', 'foolscrate.client-id', client_id)
      return client_id

    @classmethod
    def _align_client_ref_to_master(cls, git, client_id):
       return git.cmd('update-ref', "refs/heads/{}".format(client_id), 'master')


    @classmethod
    def sync_all_tracked(cls):
        with cls._track_lock.acquire(timeout=60):
            cls._logger.debug("Now syncing all tracked repositories")
            try:
                cfg = ConfigObj(FOOLSCRATE_CONFIG_PATH, unrepr=True, write_empty_values=True)
            except FileNotFoundError as e:
                # TODO: check whether it really is meaningful with configobj
                cls._logger.debug("file not found while opening foolscrate config file", e)
                return

            for localdir in cfg.get("track", []):
                try:
                    repo = Repository(localdir)
                    repo.sync()
                    cls._logger.info("synced '%s'", localdir)
                except Exception as e:
                    cls._logger.exception("Error while syncing '%s'", localdir)

    @classmethod
    def enable_foolscrate_cronjob(cls, executable=join(dirname(abspath(__file__)), "devenv", "bin", "foolscrate")):
        cron_start = "{} start\n".format(FOOLSCRATE_CRONTAB_COMMENT)
        cron_end = "{} end\n".format(FOOLSCRATE_CRONTAB_COMMENT)
        try:
            old_crontab = check_output(["crontab", "-l"], universal_newlines=True)
        except CalledProcessError:
            old_crontab = ""
        cron_pattern = re_compile("{}.*?{}".format(cron_start, cron_end), RE_DOTALL)
        old_crontab = cron_pattern.sub("", old_crontab)

        if len(old_crontab) > 0 and (old_crontab[-1] != "\n"):
            old_crontab += "\n"

        new_crontab = old_crontab + cron_start + "*/5 * * * * {}".format(shell_quote(executable) + " sync_all_tracked\n") + cron_end

        with NamedTemporaryFile(prefix="foolscrate-temp", mode="w+", encoding="utf-8") as tmp:
            tmp.write(new_crontab)
            tmp.flush()
            check_output(["crontab", tmp.name])

    @classmethod
    def cleanup_tracked(cls):
        cfg = ConfigObj(FOOLSCRATE_CONFIG_PATH, unrepr=True, write_empty_values=True)
        still_to_be_tracked = [directory for directory in cfg["track"] if exists(directory)]
        cfg["track"] = still_to_be_tracked
        cfg.write()

    @classmethod
    def test(cls):
        raise NotImplementedError("not yet implemented")


