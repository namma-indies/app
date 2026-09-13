"""The forced command for the CI deploy key: what a leaked key can and cannot do.

The key used to be pinned with `command="bash -s"`, which the runbook described
as limiting a leaked key to "the deploy script". It did not. A forced command
replaces the command the client *asked for*; `bash -s` then reads its script
from stdin, and stdin over SSH is entirely the client's to write. Combined with
a deploy user that runs `sudo docker`, a leaked key was root on the box.

These tests pin the replacement: a fixed set of named actions, everything else
refused, and stdin never executed. They run the real script, because the thing
under test is shell quoting and that is not a thing to assert about by reading.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[2] / "deploy" / "authorized-command.sh"

# Every action the wrapper is allowed to reach, and the script it must land on.
ACTIONS = {
    "deploy": "remote.sh",
    "deploy-staging": "remote-staging.sh",
    "backfill": "remote-backfill.sh",
    "find-duplicates": "remote-find-duplicates.sh",
    "seed-models": "remote-seed-models.sh",
    "backup-db": "remote-backup-db.sh",
}


@pytest.fixture
def box(tmp_path):
    """A stand-in for ~/app on the box: every remote script replaced by a stub
    that reports which one ran and what env it was handed."""
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for script in ACTIONS.values():
        (deploy / script).write_text(
            "#!/usr/bin/env bash\n"
            f'echo "RAN {script}"\n'
            'echo "BRANCH=${BRANCH:-} DRY_RUN=${DRY_RUN:-} EXTRA_ARGS=${EXTRA_ARGS:-}"\n'
            'echo "DETAIL=${DETAIL:-} UPLOAD=${UPLOAD:-}"\n'
            'cat > /tmp/should-never-exist-stdin 2>/dev/null || true\n'
        )
    return tmp_path


def run(box, command, stdin=""):
    return subprocess.run(
        ["bash", str(WRAPPER)],
        env={**os.environ, "APP_DIR": str(box), "SSH_ORIGINAL_COMMAND": command},
        input=stdin,
        text=True,
        capture_output=True,
        timeout=30,
        cwd=box,
    )


@pytest.mark.parametrize(
    "command,script",
    [
        ("deploy", "remote.sh"),
        ("deploy-staging my-branch", "remote-staging.sh"),
        ("backfill dry-run", "remote-backfill.sh"),
        ("backfill run", "remote-backfill.sh"),
        ("backfill run 0.5", "remote-backfill.sh"),
        ("find-duplicates", "remote-find-duplicates.sh"),
        ("find-duplicates detail", "remote-find-duplicates.sh"),
        ("seed-models upload", "remote-seed-models.sh"),
        ("seed-models no-upload", "remote-seed-models.sh"),
        ("backup-db", "remote-backup-db.sh"),
    ],
)
def test_allowed_actions_reach_their_script(box, command, script):
    r = run(box, command)
    assert r.returncode == 0, r.stderr
    assert f"RAN {script}" in r.stdout


def test_every_allowed_action_is_covered_by_a_case(box):
    """If someone adds an action to the wrapper, it must appear in ACTIONS
    above -- otherwise the allowlist grows without a test that says so."""
    body = WRAPPER.read_text()
    declared = {a for a in ACTIONS if f"\n  {a})" in body}
    assert declared == set(ACTIONS), (
        f"wrapper and test disagree on the action list: {declared ^ set(ACTIONS)}"
    )


@pytest.mark.parametrize(
    "command",
    [
        "",                                  # interactive session with the key
        "bash -s",                           # the old pinning, now meaningless
        "bash",
        "sh -c 'id'",
        "rm -rf /",
        "deploy; id",                        # separator is not a separator here
        "deploy && id",
        "deploy | id",
        "deploy $(id)",
        "deploy `id`",
        "deploy extra-arg",
        "deploy-staging",                    # branch is required
        "deploy-staging --upload-pack=id",   # leading dash
        "deploy-staging ../../etc/passwd",
        "deploy-staging a..b",
        "backfill",                          # mode is required
        "backfill maybe",
        "backfill run abc",                  # sleep must be a number
        "backfill run 1 2",                  # arity
        "find-duplicates loud",
        "seed-models yes",
        "backup-db now",
        "unknown-action",
    ],
)
def test_everything_else_is_refused(box, command):
    r = run(box, command)
    assert r.returncode != 0, f"{command!r} was allowed: {r.stdout}"
    assert "RAN " not in r.stdout, f"{command!r} reached a script"
    assert "refused" in r.stderr


def test_stdin_is_drained_and_never_executed(box, tmp_path):
    """CI still pipes the script in, so both this wrapper and the old `bash -s`
    pinning work with one workflow. The bytes must be read and dropped: draining
    rather than closing, so the writer never takes SIGPIPE and fails the job."""
    marker = tmp_path / "executed"
    r = run(box, "deploy", stdin=f"touch {marker}\necho SHOULD-NOT-RUN\n")
    assert r.returncode == 0, r.stderr
    assert "RAN remote.sh" in r.stdout
    assert "SHOULD-NOT-RUN" not in r.stdout
    assert not marker.exists(), "stdin was executed"


def test_a_glob_in_the_action_cannot_expand_into_arguments(box):
    """Unquoted expansion does pathname expansion as well as word splitting, so
    without `set -f` an action of `deploy *` arrives as a list of local
    filenames -- attacker-chosen strings reaching the checks below."""
    (box / "deploy-staging").mkdir(exist_ok=True)
    r = run(box, "deploy *")
    assert r.returncode != 0
    assert "RAN " not in r.stdout


def test_arguments_are_passed_as_environment_not_interpolated(box):
    r = run(box, "backfill run 2")
    assert "DRY_RUN=false" in r.stdout
    assert "EXTRA_ARGS=--sleep 2" in r.stdout
    r = run(box, "deploy-staging feat/some_branch-1.2")
    assert "BRANCH=feat/some_branch-1.2" in r.stdout
