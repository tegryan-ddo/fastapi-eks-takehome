"""Offline checks for the secret loader. All values and AWS clients are fake."""

import importlib.util
import io
import json
import logging
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import warnings

from botocore.exceptions import ClientError


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "load_secrets.py"
SPEC = importlib.util.spec_from_file_location("load_secrets", SCRIPT)
loader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loader)

ACCOUNT = "123456789012"
REGION = "us-west-2"
NAME = "/mobile-api/staging/runtime"
ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{NAME}-aBc123"
TOKEN = "11111111-2222-4333-8444-555555555555"
# Deliberately fake strings; never replace these with real credentials.
DATABASE = "DUMMY_DATABASE_SECRET_SENTINEL_é"
WEBHOOK = "DUMMY_WEBHOOK_SECRET_SENTINEL_42"


class TerminalBuffer(io.StringIO):
    def __init__(self, terminal=True):
        super().__init__()
        self.terminal = terminal

    def isatty(self):
        return self.terminal


def aws_error(code, message="Dummy AWS failure", operation="DescribeSecret"):
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


class SecretLoaderTests(unittest.TestCase):
    def setUp(self):
        self.sts = Mock(name="fake_sts")
        self.sts.get_caller_identity.return_value = {"Account": ACCOUNT}
        self.secrets = Mock(name="fake_secretsmanager")
        self.secrets.describe_secret.side_effect = aws_error("ResourceNotFoundException")
        self.addCleanup(logging.disable, logging.root.manager.disable)

    def invoke(self, inputs=None, extra=(), non_terminal=None, prompt_effect=None):
        argv = [
            "--profile", "dummy-local-profile", "--region", REGION,
            "--expected-account-id", ACCOUNT, "--secret-name", NAME,
            *extra,
        ]
        streams = {name: TerminalBuffer(name != non_terminal)
                   for name in ("stdin", "stdout", "stderr")}
        if inputs is None:
            inputs = [NAME, DATABASE, DATABASE, WEBHOOK, WEBHOOK]
        with (
            patch.object(loader, "make_clients", return_value=(self.sts, self.secrets)) as factory,
            patch.object(loader.getpass, "getpass", side_effect=prompt_effect or inputs) as prompt,
            patch.object(loader.uuid, "uuid4", return_value=TOKEN),
            patch.object(loader.sys, "stdin", streams["stdin"]),
            patch.object(loader.sys, "stdout", streams["stdout"]),
            patch.object(loader.sys, "stderr", streams["stderr"]),
        ):
            try:
                status = loader.main(argv)
            except SystemExit as exc:
                status = exc.code
        output = streams["stdout"].getvalue() + streams["stderr"].getvalue()
        for value in (DATABASE, WEBHOOK):
            self.assertNotIn(value, output)
        self.assertNotIn("Traceback", output)
        return status, output, factory, prompt

    def assert_no_write(self):
        self.secrets.create_secret.assert_not_called()
        self.secrets.put_secret_value.assert_not_called()
        self.secrets.get_secret_value.assert_not_called()

    def test_create_sends_values_only_in_secret_string(self):
        status, output, _, _ = self.invoke()
        self.assertEqual(status, 0)
        request = self.secrets.create_secret.call_args.kwargs
        self.assertEqual(request["Name"], NAME)
        self.assertEqual(request["ClientRequestToken"], TOKEN)
        self.assertEqual(json.loads(request["SecretString"]), {
            "DATABASE_URL": DATABASE, "PAYMENTS_WEBHOOK_SECRET": WEBHOOK,
        })
        self.assertEqual(set(request), {"Name", "ClientRequestToken", "SecretString"})
        self.assertIn("AWS confirmed the write", output)
        self.secrets.put_secret_value.assert_not_called()
        self.secrets.get_secret_value.assert_not_called()

    def test_non_tty_streams_block_before_client_creation(self):
        for stream in ("stdin", "stdout", "stderr"):
            with self.subTest(stream=stream):
                status, _, factory, prompt = self.invoke(non_terminal=stream)
                self.assertEqual(status, 1)
                factory.assert_not_called()
                prompt.assert_not_called()
        self.assert_no_write()

    def test_wrong_account_blocks_before_secret_lookup_or_prompts(self):
        self.sts.get_caller_identity.return_value = {"Account": "999999999999"}
        status, output, _, prompt = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn("does not match", output)
        self.secrets.describe_secret.assert_not_called()
        prompt.assert_not_called()
        self.assert_no_write()

    def test_destination_mismatch_blocks_before_value_entry(self):
        status, output, _, prompt = self.invoke(inputs=[NAME + "-wrong"])
        self.assertEqual(status, 1)
        self.assertIn("Destination confirmation did not match", output)
        self.assertEqual(prompt.call_count, 1)
        self.assert_no_write()

    def test_confirmation_mismatch_does_not_echo_input(self):
        status, output, _, prompt = self.invoke(inputs=[NAME, DATABASE, WEBHOOK])
        self.assertEqual(status, 1)
        self.assertIn("values did not match", output)
        self.assertEqual(prompt.call_count, 3)
        self.assert_no_write()

    def test_empty_value_is_rejected(self):
        status, output, _, _ = self.invoke(inputs=[NAME, ""])
        self.assertEqual(status, 1)
        self.assertIn("Empty secret values", output)
        self.assert_no_write()

    def test_hidden_input_warning_fails_before_fallback_input(self):
        def unsafe_prompt(_prompt):
            warnings.warn(DATABASE, loader.getpass.GetPassWarning)
            self.fail("A GetPassWarning must stop input before a fallback can proceed")

        status, output, _, _ = self.invoke(prompt_effect=unsafe_prompt)
        self.assertEqual(status, 1)
        self.assertIn("Hidden input is unavailable", output)
        self.assert_no_write()

    def test_existing_secret_is_not_updated_without_explicit_flag(self):
        self.secrets.describe_secret.side_effect = None
        self.secrets.describe_secret.return_value = {"ARN": ARN}
        status, output, _, prompt = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn("secret already exists", output)
        prompt.assert_not_called()
        self.assert_no_write()

    def test_create_race_does_not_fall_back_to_update(self):
        self.secrets.create_secret.side_effect = aws_error(
            "ResourceExistsException", DATABASE, "CreateSecret"
        )
        status, output, _, _ = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn("No automatic create-to-update fallback", output)
        self.secrets.create_secret.assert_called_once()
        self.secrets.put_secret_value.assert_not_called()
        self.secrets.get_secret_value.assert_not_called()

    def test_update_uses_resolved_arn_and_replaces_complete_payload(self):
        self.secrets.describe_secret.side_effect = None
        self.secrets.describe_secret.return_value = {"ARN": ARN}
        status, output, _, _ = self.invoke(extra=["--update-existing"])
        self.assertEqual(status, 0)
        self.secrets.describe_secret.assert_called_once_with(SecretId=NAME)
        request = self.secrets.put_secret_value.call_args.kwargs
        self.assertEqual(request["SecretId"], ARN)
        self.assertEqual(request["ClientRequestToken"], TOKEN)
        self.assertEqual(json.loads(request["SecretString"]), {
            "DATABASE_URL": DATABASE, "PAYMENTS_WEBHOOK_SECRET": WEBHOOK,
        })
        self.assertIn("any other existing fields", output)
        self.secrets.create_secret.assert_not_called()
        self.secrets.get_secret_value.assert_not_called()

    def test_missing_secret_update_stops_before_prompts(self):
        status, output, _, prompt = self.invoke(extra=["--update-existing"])
        self.assertEqual(status, 1)
        self.assertIn("secret does not exist", output)
        prompt.assert_not_called()
        self.assert_no_write()

    def test_secret_pending_deletion_is_not_updated(self):
        self.secrets.describe_secret.side_effect = None
        self.secrets.describe_secret.return_value = {"ARN": ARN, "DeletedDate": "dummy-date"}
        status, output, _, prompt = self.invoke(extra=["--update-existing"])
        self.assertEqual(status, 1)
        self.assertIn("scheduled for deletion", output)
        prompt.assert_not_called()
        self.assert_no_write()

    def test_oversized_utf8_payload_stops_before_write(self):
        oversized = "\U0001f510" * 17000
        status, output, _, _ = self.invoke(inputs=[NAME, oversized, oversized, WEBHOOK, WEBHOOK])
        self.assertEqual(status, 1)
        self.assertIn("size limit", output)
        self.assertNotIn(oversized, output)
        self.assertNotIn("Version token:", output)
        self.assert_no_write()

    def test_write_exception_is_sanitized_and_reports_uncertain_outcome(self):
        self.secrets.create_secret.side_effect = RuntimeError(
            f"Simulated timeout containing request values: {DATABASE} {WEBHOOK}"
        )
        status, output, _, _ = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn(f"Version token: {TOKEN}", output)
        self.assertIn("outcome may be unknown", output)
        self.assertNotIn("AWS confirmed", output)
        self.secrets.create_secret.assert_called_once()
        self.secrets.put_secret_value.assert_not_called()

    def test_lookup_exception_is_sanitized(self):
        self.secrets.describe_secret.side_effect = aws_error("AccessDeniedException", DATABASE)
        status, output, _, prompt = self.invoke()
        self.assertEqual(status, 1)
        self.assertIn("Cannot inspect", output)
        prompt.assert_not_called()
        self.assert_no_write()

    def test_confirmation_preserves_whitespace_and_unicode(self):
        value = "  DUMMY spaced unicode \u00e9 value  "
        status, output, _, _ = self.invoke(inputs=[NAME, value, value, WEBHOOK, WEBHOOK])
        self.assertEqual(status, 0)
        request = self.secrets.create_secret.call_args.kwargs
        self.assertEqual(json.loads(request["SecretString"])["DATABASE_URL"], value)
        self.assertNotIn(value, output)

    def test_invalid_cli_arguments_are_not_repeated_in_errors(self):
        status, output, factory, _ = self.invoke(extra=["--secret-string", DATABASE])
        self.assertEqual(status, 2)
        self.assertIn("Invalid options", output)
        factory.assert_not_called()
        self.assert_no_write()

    def test_keyboard_interrupt_is_sanitized(self):
        status, output, _, _ = self.invoke(prompt_effect=KeyboardInterrupt(DATABASE))
        self.assertEqual(status, 130)
        self.assertIn("Cancelled", output)
        self.assert_no_write()

    def test_optional_kms_key_is_used_only_for_create(self):
        status, _, _, _ = self.invoke(extra=["--kms-key-id", "alias/dummy-key"])
        self.assertEqual(status, 0)
        self.assertEqual(self.secrets.create_secret.call_args.kwargs["KmsKeyId"], "alias/dummy-key")

    def test_kms_key_change_with_update_is_rejected_before_clients(self):
        status, _, factory, _ = self.invoke(extra=["--update-existing", "--kms-key-id", "alias/dummy-key"])
        self.assertEqual(status, 2)
        factory.assert_not_called()
        self.assert_no_write()

    def test_sdk_clients_keep_tls_and_ignore_endpoint_overrides(self):
        args = loader.parse_args([
            "--profile", "dummy-local-profile", "--region", REGION,
            "--expected-account-id", ACCOUNT, "--secret-name", NAME,
        ])
        with patch("boto3.Session") as session_class:
            loader.make_clients(args)
        session_class.assert_called_once_with(profile_name="dummy-local-profile", region_name=REGION)
        calls = session_class.return_value.client.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["sts", "secretsmanager"])
        for call in calls:
            self.assertIs(call.kwargs["verify"], True)
            config = call.kwargs["config"]
            self.assertIs(config.ignore_configured_endpoint_urls, True)
            self.assertEqual(config.retries["total_max_attempts"], 3)


if __name__ == "__main__":
    unittest.main()
