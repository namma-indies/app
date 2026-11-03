"""Small, credential-silent AWS CLI wrapper for opt-in operator tools."""
import json
import subprocess


class OpsError(Exception):
    """Only fixed classifications may reach a public Actions log."""


def aws_command(args, *, endpoint="", missing_ok=False):
    command = ["aws", "--no-cli-pager", "--cli-connect-timeout", "5",
               "--cli-read-timeout", "20"]
    if endpoint:
        command += ["--endpoint-url", endpoint]
    try:
        result = subprocess.run(command + ["s3api", *args], capture_output=True,
                                timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise OpsError("object_store_command_failed") from None
    if result.returncode:
        if missing_ok and b"(NoSuchKey)" in result.stderr:
            return None
        raise OpsError("object_store_command_failed")
    try:
        return json.loads(result.stdout or b"{}")
    except (ValueError, UnicodeError):
        raise OpsError("object_store_response_invalid") from None
