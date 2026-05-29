# Terminator by Chris Jones <cmsj@tenshu.net>
# GPL v2 only
"""terminatorlib.criu - terminator-side glue for CRIU checkpoint/restore.

The actual privileged helper lives at the repo root in libexec/ (installed
to /usr/local/libexec/terminator-criu-helper). This package holds the
unprivileged Python code terminator imports to invoke the helper and to
extend tabs / layouts with checkpoint metadata.
"""
