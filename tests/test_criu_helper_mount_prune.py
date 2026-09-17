# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""Unit tests for the mount-prune predicate in terminator-criu-helper.

These test the pure `_should_prune` matching logic only — no root, no
mount namespace, no CRIU. The end-to-end pruning behavior (actually
calling unshare/umount2) is covered by integration_tests/criu/, which
needs CAP_SYS_ADMIN and is excluded from normal pytest runs.
"""
import importlib.util
import os
from importlib.machinery import SourceFileLoader

import pytest

_HELPER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "libexec", "terminator-criu-helper")

# The helper has no .py extension (it's invoked directly via sudoers),
# so spec_from_file_location can't infer a loader for it — construct
# one explicitly.
_loader = SourceFileLoader("terminator_criu_helper", _HELPER_PATH)
_spec = importlib.util.spec_from_loader("terminator_criu_helper", _loader)
_helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(_helper)


@pytest.mark.parametrize("mp,fstype,expected", [
    # Existing rules, unaffected by the new one.
    ("/sys/fs/cgroup", "cgroup2", True),
    ("/sys/fs/cgroup/unified", "cgroup2", True),
    ("/run/snapd/ns", "tmpfs", True),
    ("/run/snapd/ns/firefox.mnt", "nsfs", True),
    ("/run/snapd.socket", "sockfs", False),
    ("/run/user/1000/doc", "fuse.portal", True),
    ("/run/user/1000/gvfs", "fuse.gvfsd-fuse", True),
    ("/run/user/1000/sshfs-mount", "fuse.sshfs", True),
    ("/home/user/sshfs-mount", "fuse.sshfs", False),
    ("/", "ext4", False),

    # New rule: dockerd's opaque-bug-check self-test overlay mounts.
    ("/var/lib/docker/overlay2/opaque-bug-check500440038/merged",
     "overlay", True),
    ("/var/lib/docker/overlay2/opaque-bug-check520673387/merged",
     "overlay", True),
    # A real, non-self-test overlay2 layer must NOT be pruned — only
    # the "opaque-bug-check*" basename prefix matches.
    ("/var/lib/docker/overlay2/abcdef0123456789/merged", "overlay", False),
    ("/var/lib/docker/overlay2", "ext4", False),
])
def test_should_prune(mp, fstype, expected):
    assert _helper._should_prune(mp, fstype) is expected
