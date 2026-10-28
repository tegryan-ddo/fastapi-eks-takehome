#!/usr/bin/env python3
"""Enter application secrets locally and send them directly to AWS Secrets Manager.

Run in your own trusted terminal, outside chat, an agent terminal, or CI.
Only destination metadata is accepted as command-line arguments. An update
replaces the complete two-key JSON payload; it does not merge existing data.
"""

import argparse
import getpass
import json
import logging
import re
import sys
import uuid
import warnings


class SafeError(Exception):
    """An error whose message is authored here and contains no secret values."""


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats invalid arguments, which could contain a
        # value accidentally supplied on the command line.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid options. Use --help; enter secret values only at hidden prompts.\n")


def parse_args(argv=None):
    parser = QuietParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--profile", required=True, help="Existing AWS profile, preferably SSO")
    parser.add_argument("--region", required=True, help="Target AWS Region")
    parser.add_argument("--expected-account-id", required=True, help="Expected 12-digit AWS account")
    parser.add_argument("--secret-name", required=True, help="Name, e.g. /mobile-api/staging/runtime")
    parser.add_argument("--update-existing", action="store_true", help="Replace both keys of an existing secret")
    parser.add_argument("--kms-key-id", help="Optional KMS key ARN or alias for creation only")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9]{12}", args.expected_account_id):
        parser.error("Invalid account")
    if not re.fullmatch(r"[a-z0-9-]+", args.region):
        parser.error("Invalid region")
    if not re.fullmatch(r"[A-Za-z0-9/_+=.@-]{1,512}", args.secret_name):
        parser.error("Invalid secret name")
    if not args.profile or any(ord(c) < 32 or ord(c) == 127 for c in args.profile):
        parser.error("Invalid profile")
    if args.kms_key_id and not re.fullmatch(r"[A-Za-z0-9/_+=.@:-]+", args.kms_key_id):
        parser.error("Invalid KMS key")
    if args.update_existing and args.kms_key_id:
        parser.error("KMS changes are not supported during value replacement")
    return args


def require_terminal():
    if not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr)):
        raise SafeError("Use an interactive local terminal without redirected input or output.")


def hidden_input(prompt):
    # getpass otherwise warns and falls back to potentially visible input.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise SafeError("Hidden input is unavailable; no value was accepted.") from None


def confirmed_value(key):
    value = hidden_input(f"{key} (hidden): ")
    if not value:
        raise SafeError("Empty secret values are not accepted.")
    if hidden_input(f"Confirm {key} (hidden): ") != value:
        raise SafeError("The values did not match. Nothing was submitted.")
    # Preserve the exact input. Do not strip, log, or put it in the environment.
    return value


def make_clients(args):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise SafeError("Install the dependencies from requirements-secrets.txt first.") from None

    config = Config(
        signature_version="v4",
        connect_timeout=5,
        read_timeout=20,
        retries={"mode": "standard", "total_max_attempts": 3},
        # Do not send real secrets to an environment/shared-config endpoint override.
        ignore_configured_endpoint_urls=True,
    )
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    return (
        session.client("sts", config=config, verify=True),
        session.client("secretsmanager", config=config, verify=True),
    )


def resolve_target(client, args):
    from botocore.exceptions import ClientError

    try:
        metadata = client.describe_secret(SecretId=args.secret_name)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise SafeError("Cannot inspect the target secret. Check permissions and AWS connectivity.") from None
        if args.update_existing:
            raise SafeError("The secret does not exist. Omit --update-existing to create it.") from None
        return None
    if not args.update_existing:
        raise SafeError("The secret already exists. Use --update-existing only to replace its entire payload.")
    if metadata.get("DeletedDate"):
        raise SafeError("The secret is scheduled for deletion. Resolve that separately before updating.")
    return metadata["ARN"]


def run(args):
    require_terminal()
    sts, client = make_clients(args)
    account = sts.get_caller_identity()["Account"]
    if account != args.expected_account_id:
        raise SafeError("The signed-in AWS account does not match --expected-account-id. Nothing was written.")
    target_arn = resolve_target(client, args)
    action = "REPLACE ENTIRE PAYLOAD" if args.update_existing else "CREATE"
    print(f"Action: {action}\nAccount: {account}\nRegion: {args.region}\nSecret: {args.secret_name}")
    print("Keys: DATABASE_URL, PAYMENTS_WEBHOOK_SECRET")
    if args.update_existing:
        print("This replaces both keys and any other existing fields; it does not merge or read the old value.")
    if hidden_input("Type the secret name to confirm the destination (hidden): ") != args.secret_name:
        raise SafeError("Destination confirmation did not match. Nothing was written.")

    values = {key: confirmed_value(key) for key in ("DATABASE_URL", "PAYMENTS_WEBHOOK_SECRET")}
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > 65536:
        raise SafeError("The encoded payload exceeds Secrets Manager's size limit. Nothing was submitted.")

    # One token identifies this write and is reused by the SDK's bounded retries.
    # It is metadata, not a secret; it also helps resolve an uncertain response.
    version_token = str(uuid.uuid4())
    print(f"Version token: {version_token}", flush=True)
    try:
        if args.update_existing:
            client.put_secret_value(
                SecretId=target_arn,
                ClientRequestToken=version_token,
                SecretString=payload,
            )
        else:
            request = {
                "Name": args.secret_name,
                "ClientRequestToken": version_token,
                "SecretString": payload,
            }
            if args.kms_key_id:
                request["KmsKeyId"] = args.kms_key_id
            client.create_secret(**request)
    except Exception:
        # Never print raw SDK exceptions: they may contain request material.
        # A lost response does not establish whether the write reached AWS.
        raise SafeError(
            "AWS did not confirm the write. Its outcome may be unknown. "
            "Check version metadata for the token above before starting another write. "
            "No automatic create-to-update fallback was attempted."
        ) from None
    finally:
        values.clear()
        payload = None
        # Dropping references is housekeeping, not a guarantee of memory erasure.
    print("AWS confirmed the write. No secret values were displayed or read back.")
    return 0


def main(argv=None):
    # This is a standalone operator tool. Disable SDK/HTTP debug logging before
    # importing the SDK; diagnostics below deliberately use static messages.
    logging.disable(logging.CRITICAL)
    args = parse_args(argv)
    try:
        return run(args)
    except SafeError as error:
        print(f"Error: {error}", file=sys.stderr)
    except (KeyboardInterrupt, EOFError):
        print("Cancelled. If a version token was printed, check its metadata before retrying.", file=sys.stderr)
        return 130
    except Exception:
        print("Operation stopped. Check the AWS profile, SDK installation, and connectivity. Details suppressed.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
