# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""Project-wide pytest configuration.

`integration_tests/` holds end-to-end tests that need CRIU installed,
sudo NOPASSWD, and CAP_SYS_ADMIN for PID-namespace creation. None of
those are available in the GitHub Actions CI environment, so we exclude
the directory from pytest discovery. Run those tests by hand on a
suitably-configured machine.
"""
collect_ignore_glob = ["integration_tests/**"]
