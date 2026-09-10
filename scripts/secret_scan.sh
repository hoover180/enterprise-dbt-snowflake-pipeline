#!/usr/bin/env bash
# Scan a git diff range for likely secrets before committing/pushing.
#
# Usage: scripts/secret_scan.sh [diff-range]
#   diff-range defaults to main...HEAD.
#
# Runs against the full `git diff`, not scoped to specific paths, so it
# can't be accidentally under-scoped the way an ad hoc path-limited scan
# can be.

set -euo pipefail

range="${1:-main...HEAD}"

# NOTE: no (?i) prefix here -- that's a PCRE inline-flag construct, not
# valid in POSIX ERE (grep -E). GNU grep silently matches nothing when it
# sees it instead of erroring, which would make this scan falsely report
# "clean" on every run. -i below already makes the scan case-insensitive.
pattern='(api[_-]?key|secret|password|passwd|token|private[_-]?key|BEGIN (RSA|EC|OPENSSH|PGP)? ?PRIVATE KEY|aws_access_key_id|aws_secret_access_key|AKIA[0-9A-Z]{16}|xox[baprs]-|ghp_[A-Za-z0-9]{36}|SNOWFLAKE_[A-Z_]*\s*=|-----BEGIN)'

if git diff "$range" | grep -inE "$pattern"; then
    exit 1
fi

echo "clean, no matches"
exit 0
