import fcntl
import logging
import os
import shutil
import subprocess
import time

from .config import config

log = logging.getLogger(__name__)


def enforce_repo_state(repo_url, dest_path, branch, remove_on_error=True):
    """
    Use git to either clone or update a given repo, forcing it to switch to the
    specified branch.

    :param repo_url:  The full URL to the repo (not including the branch)
    :param dest_path: The full path to the destination directory
    :param branch:    The branch.
    :param remove:    Whether or not to remove dest_dir when an error occurs
    :raises:          BranchNotFoundError if the branch is not found;
                      RuntimeError for other errors
    """
    validate_branch(branch)
    try:
        if not os.path.isdir(dest_path):
            clone_repo(repo_url, dest_path, branch)
        elif time.time() - os.stat('/etc/passwd').st_mtime > 60:
            # only do this at most once per minute
            fetch(dest_path)
            out = subprocess.check_output(('touch', dest_path))
            if out:
                log.info(out)
        else:
            log.info("%s was just updated; assuming it is current", branch)

        reset_repo(repo_url, dest_path, branch)
    except BranchNotFoundError:
        if remove_on_error:
            shutil.rmtree(dest_path, ignore_errors=True)
        raise


def clone_repo(repo_url, dest_path, branch):
    """
    Clone a repo into a path

    :param repo_url:  The full URL to the repo (not including the branch)
    :param dest_path: The full path to the destination directory
    :param branch:    The branch.
    :raises:          BranchNotFoundError if the branch is not found;
                      RuntimeError for other errors
    """
    validate_branch(branch)
    log.info("Cloning %s %s from upstream", repo_url, branch)
    proc = subprocess.Popen(
        ('git', 'clone', '--branch', branch, repo_url, dest_path),
        cwd=os.path.dirname(dest_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    if proc.wait() != 0:
        not_found_str = "Remote branch %s not found" % branch
        out = proc.stdout.read()
        log.error(out)
        if not_found_str in out:
            raise BranchNotFoundError(branch, repo_url)
        else:
            raise RuntimeError("git clone failed!")


def fetch(repo_path):
    """
    Call "git fetch -p origin"

    :param repo_path: The full path to the repository
    :raises:          RuntimeError if the operation fails
    """
    log.debug("Fetching from upstream into %s", repo_path)
    proc = subprocess.Popen(
        ('git', 'fetch', '-p', 'origin'),
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    if proc.wait() != 0:
        out = proc.stdout.read()
        log.error(out)
        raise RuntimeError("git fetch failed!")


def fetch_branch(repo_path, branch):
    """
    Call "git fetch -p origin <branch>"

    :param repo_path: The full path to the repository
    :param branch:    The branch.
    :raises:          BranchNotFoundError if the branch is not found;
                      RuntimeError for other errors
    """
    validate_branch(branch)
    log.info("Fetching %s from upstream", branch)
    proc = subprocess.Popen(
        ('git', 'fetch', '-p', 'origin', branch),
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    if proc.wait() != 0:
        not_found_str = "fatal: Couldn't find remote ref %s" % branch
        out = proc.stdout.read()
        log.error(out)
        if not_found_str in out:
            raise BranchNotFoundError(branch)
        else:
            raise RuntimeError("git fetch failed!")


def reset_repo(repo_url, dest_path, branch):
    """

    :param repo_url:  The full URL to the repo (not including the branch)
    :param dest_path: The full path to the destination directory
    :param branch:    The branch.
    :raises:          BranchNotFoundError if the branch is not found;
                      RuntimeError for other errors
    """
    validate_branch(branch)
    log.debug('Resetting repo at %s to branch %s', dest_path, branch)
    # This try/except block will notice if the requested branch doesn't
    # exist, whether it was cloned or fetched.
    try:
        subprocess.check_output(
            ('git', 'reset', '--hard', 'origin/%s' % branch),
            cwd=dest_path,
        )
    except subprocess.CalledProcessError:
        raise BranchNotFoundError(branch, repo_url)


class BranchNotFoundError(ValueError):
    def __init__(self, branch, repo=None):
        self.branch = branch
        self.repo = repo

    def __str__(self):
        if self.repo:
            repo_str = " in repo: %s" % self.repo
        else:
            repo_str = ""
        return "Branch '{branch}' not found{repo_str}!".format(
            branch=self.branch, repo_str=repo_str)


def validate_branch(branch):
    if ' ' in branch:
        raise ValueError("Illegal branch name: '%s'" % branch)


def fetch_qa_suite(branch):
    """
    Make sure ceph-qa-suite is checked out.

    :param branch: The branch to fetch
    :returns:      The destination path
    """
    src_base_path = config.src_base_path
    dest_path = os.path.join(src_base_path, 'ceph-qa-suite_' + branch)
    qa_suite_url = os.path.join(config.ceph_git_base_url, 'ceph-qa-suite')
    # only let one worker create/update the checkout at a time
    lock = filelock(dest_path.rstrip('/') + '.lock')
    lock.acquire()
    try:
        enforce_repo_state(qa_suite_url, dest_path, branch)
    finally:
        lock.release()
    return dest_path


def fetch_teuthology_branch(branch):
    """
    Make sure we have the correct teuthology branch checked out and up-to-date

    :param branch: The branche we want
    :returns:      The destination path
    """
    src_base_path = config.src_base_path
    dest_path = os.path.join(src_base_path, 'teuthology_' + branch)
    # only let one worker create/update the checkout at a time
    lock = filelock(dest_path.rstrip('/') + '.lock')
    lock.acquire()
    try:
        teuthology_git_upstream = config.ceph_git_base_url + \
            'teuthology.git'
        enforce_repo_state(teuthology_git_upstream, dest_path, branch)

        log.debug("Bootstrapping %s", dest_path)
        # This magic makes the bootstrap script not attempt to clobber an
        # existing virtualenv. But the branch's bootstrap needs to actually
        # check for the NO_CLOBBER variable.
        env = os.environ.copy()
        env['NO_CLOBBER'] = '1'
        cmd = './bootstrap'
        boot_proc = subprocess.Popen(cmd, shell=True, cwd=dest_path, env=env,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        returncode = boot_proc.wait()
        if returncode != 0:
            for line in boot_proc.stdout.readlines():
                log.warn(line.strip())
        log.info("Bootstrap exited with status %s", returncode)

    finally:
        lock.release()

    return dest_path


class filelock(object):
    # simple flock class
    def __init__(self, fn):
        self.fn = fn
        self.fd = None

    def acquire(self):
        assert not self.fd
        self.fd = file(self.fn, 'w')
        fcntl.lockf(self.fd, fcntl.LOCK_EX)

    def release(self):
        assert self.fd
        fcntl.lockf(self.fd, fcntl.LOCK_UN)
        self.fd = None