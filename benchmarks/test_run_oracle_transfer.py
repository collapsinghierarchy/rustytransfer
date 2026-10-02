import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run_oracle_transfer as runner


class RustRowsTests(unittest.TestCase):
    def test_rows_keep_endpoint_metrics_and_add_pairing_provenance(self):
        payload_profile = {
            "source_read_seconds": 0.1,
            "allocation_copy_encrypt_seconds": 0.2,
            "send_wait_seconds": 0.3,
            "receive_wait_seconds": 0.0,
            "decrypt_seconds": 0.0,
            "destination_write_seconds": 0.0,
            "chunk_count": 3,
            "payload_bytes": 12345,
        }
        sender_metric = {
            "role": "sender",
            "size_bytes": 12345,
            "chunk_size": 4096,
            "success": True,
            "pipeline_depth": 3,
            "handshake_seconds": 0.4,
            "payload_seconds": 1.2,
            "shutdown_seconds": 0.1,
            "wall_seconds": 1.7,
            "effective_mib_per_second": 0.0069,
            "path": "direct",
            "payload_profile": payload_profile,
        }
        receiver_metric = {**sender_metric, "role": "receiver"}
        with patch.object(runner, "git_output", return_value="commit"):
            rows = runner.rust_rows(
                sender_metric,
                receiver_metric,
                "a" * 64,
                "a" * 64,
                12345,
                5.0,
                {"user_cpu_seconds": 1.0, "system_cpu_seconds": 0.5, "max_rss_kib": 100},
                {"user_cpu_seconds": 2.0, "system_cpu_seconds": 0.25, "max_rss_kib": 200},
                1,
                False,
                {
                    "build_id": "candidate-a",
                    "sender_binary_sha256": "b" * 64,
                    "receiver_binary_sha256": "c" * 64,
                    "direction": "wsl-to-oracle",
                    "host_pair": "WSL/ubuntu@oracle",
                    "storage_class": "wsl-native-oracle-home",
                    "pairing_mode": "invite",
                    "profile_mode": "payload-profile",
                },
            )

        self.assertEqual([row["role"] for row in rows], ["sender", "receiver"])
        self.assertEqual([row["chunk_size"] for row in rows], [4096, 4096])
        for row in rows:
            self.assertEqual(row["size_bytes"], 12345)
            self.assertEqual(row["pipeline_depth"], 3)
            self.assertEqual(row["pairing_mode"], "invite")
            self.assertNotIn("transport_mode", row)
        self.assertEqual(rows[0]["payload_seconds"], 1.2)
        self.assertEqual([row["wall_seconds"] for row in rows], [5.0, 5.0])
        self.assertAlmostEqual(
            rows[0]["effective_mib_per_second"], (12345 / (1024 * 1024)) / 5.0
        )
        self.assertEqual(rows[0]["sender_cpu_seconds"], 1.5)
        self.assertEqual(rows[0]["receiver_cpu_seconds"], 2.25)
        self.assertIn("WSL to Oracle", rows[0]["measurement_scope"])
        self.assertFalse(rows[0]["direct_route_verified_both"])
        self.assertEqual(rows[0]["profile_mode"], "payload-profile")
        self.assertEqual(rows[0]["payload_profile"], payload_profile)

    def test_direct_only_sweep_stops_after_retaining_unverified_endpoint_rows(self):
        evidence = {
            "classification": "direct",
            "verified": True,
            "direct_stream_tx": 5,
            "direct_stream_rx": 5,
            "relay_stream_tx": 0,
            "relay_stream_rx": 0,
            "lagged": False,
            "missing_path_stats": False,
            "relay_selected": False,
        }
        sender = {
            "size_bytes": 12,
            "chunk_size": 4,
            "success": True,
            "path_evidence": evidence,
        }
        receiver = {**sender, "path_evidence": {**evidence, "classification": "mixed"}}
        with patch.object(runner, "git_output", return_value="commit"):
            rows = runner.rust_rows(
                sender,
                receiver,
                "a" * 64,
                "a" * 64,
                12,
                1.0,
                {"user_cpu_seconds": 0.5, "system_cpu_seconds": 0.2, "max_rss_kib": 100},
                {"user_cpu_seconds": 0.6, "system_cpu_seconds": 0.3, "max_rss_kib": 120},
                1,
                False,
                {
                    "build_id": "candidate-a",
                    "sender_binary_sha256": "b" * 64,
                    "receiver_binary_sha256": "c" * 64,
                    "direction": "wsl-to-oracle",
                    "host_pair": "WSL/ubuntu@oracle",
                    "storage_class": "wsl-native-oracle-home",
                    "pairing_mode": "invite",
                },
            )
        self.assertFalse(rows[0]["direct_route_verified_both"])
        self.assertFalse(rows[1]["direct_route_verified_both"])
        with self.assertRaisesRegex(RuntimeError, "diagnostic rows were retained"):
            runner.ensure_direct_route_evidence(rows, "direct")
        runner.ensure_direct_route_evidence(rows, "relay")

    def test_rows_reject_endpoint_metric_disagreement(self):
        sender_metric = {"size_bytes": 12345, "chunk_size": 4096, "success": True}
        invalid_metrics = (
            ({**sender_metric, "size_bytes": 12344}, "receiver reported 12344 bytes"),
            ({**sender_metric, "chunk_size": 8192}, "invalid or different chunk sizes"),
            ({**sender_metric, "success": False}, "receiver metric did not report success"),
        )
        for receiver_metric, error in invalid_metrics:
            with self.subTest(error=error), self.assertRaisesRegex(RuntimeError, error):
                runner.rust_rows(
                    sender_metric,
                    receiver_metric,
                    "a" * 64,
                    "a" * 64,
                    12345,
                    5.0,
                    {},
                    {},
                    1,
                    False,
                    {
                        "build_id": "candidate-a",
                        "sender_binary_sha256": "b" * 64,
                        "receiver_binary_sha256": "c" * 64,
                        "direction": "wsl-to-oracle",
                        "host_pair": "WSL/ubuntu@oracle",
                        "storage_class": "wsl-native-oracle-home",
                        "pairing_mode": "invite",
                    },
                )

    def test_reverse_rows_label_direction_and_use_sender_receiver_resources(self):
        sender_metric = {
            "size_bytes": 12345,
            "chunk_size": 4096,
            "success": True,
            "role": "sender",
            "path": "direct",
        }
        receiver_metric = {**sender_metric, "role": "receiver"}
        with patch.object(runner, "git_output", return_value="commit"):
            rows = runner.rust_rows(
                sender_metric,
                receiver_metric,
                "a" * 64,
                "a" * 64,
                12345,
                5.0,
                {"user_cpu_seconds": 3.0, "system_cpu_seconds": 0.5, "max_rss_kib": 300},
                {"user_cpu_seconds": 1.0, "system_cpu_seconds": 0.25, "max_rss_kib": 100},
                1,
                False,
                {
                    "build_id": "candidate-a",
                    "sender_binary_sha256": "b" * 64,
                    "receiver_binary_sha256": "c" * 64,
                    "direction": "oracle-to-wsl",
                    "host_pair": "WSL/ubuntu@oracle",
                    "storage_class": "wsl-native-oracle-home",
                    "pairing_mode": "invite",
                },
            )
        self.assertEqual(rows[0]["sender_cpu_seconds"], 3.5)
        self.assertEqual(rows[0]["receiver_cpu_seconds"], 1.25)
        self.assertIn("Oracle to WSL", rows[0]["measurement_scope"])

    def test_full_invite_is_redacted_from_log(self):
        invite = "rt1:peer-id:0123456789abcdef0123456789abcdef"
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "endpoint.log"
            log_path.write_text(f"Direct invite: {invite}\n", encoding="utf-8")
            runner.redact_direct_invites(log_path)
            log = log_path.read_text(encoding="utf-8")
        self.assertNotIn(invite, log)
        self.assertIn("<redacted>", log)

    def test_invite_is_scrubbed_when_parser_did_not_capture_it(self):
        invite = "rt1:peer-id:0123456789abcdef0123456789abcdef"
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "sender.log"
            log_path.write_text(f"debug output contained {invite}\n", encoding="utf-8")
            runner.redact_direct_invites(log_path)
            log = log_path.read_text(encoding="utf-8")
        self.assertNotIn(invite, log)
        self.assertIn("<redacted>", log)

    def test_direct_invite_parser_captures_full_authorization(self):
        invite = "rt1:peer-id:0123456789abcdef0123456789abcdef"
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "sender.log"
            log_path.write_text(f"Direct invite: {invite}\n", encoding="utf-8")
            parsed = runner.wait_for_direct_invite(Mock(poll=lambda: None), log_path)
        self.assertEqual(parsed, invite)


class OracleRunnerArgumentTests(unittest.TestCase):
    def test_rust_rows_are_saved_before_path_or_evidence_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "metrics.jsonl"
            path_mismatch_row = {"success": True, "direct_route_verified_both": True}
            with self.assertRaisesRegex(RuntimeError, "selected relay"):
                runner.append_and_validate_rust_rows(
                    output, [path_mismatch_row], "direct", "relay"
                )
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8").splitlines()[0]),
                path_mismatch_row,
            )

            evidence_row = {"success": True, "direct_route_verified_both": False}
            with self.assertRaisesRegex(RuntimeError, "diagnostic rows were retained"):
                runner.append_and_validate_rust_rows(
                    output, [evidence_row], "direct", "direct"
                )
            self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 2)

    def test_payload_profile_missing_endpoint_is_retained_then_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "profile.jsonl"
            rows = [
                {"profile_mode": "payload-profile", "payload_profile": {}},
                {"profile_mode": "payload-profile"},
            ]
            with self.assertRaisesRegex(RuntimeError, "invalid or missing payload_profile"):
                runner.append_and_validate_rust_rows(output, rows, "relay", "relay")
            self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 2)

    def parse(self, temp_dir, rusty_only, direction="wsl-to-oracle", auth="pake", profile=False, remote_inputs=None):
        root = Path(temp_dir)
        paths = [root / name for name in ("key", "rusty", "64.bin", "512.bin")]
        for path in paths:
            path.touch()
        output = root / "out"
        output.mkdir()
        argv = [
            "--host", "example.invalid",
            "--ssh-key", str(paths[0]),
            "--rusty-sender", str(paths[1]),
            "--remote-rusty", "/opt/rustytransfer",
            "--input-64", str(paths[2]),
            "--input-512", str(paths[3]),
            "--output-dir", str(output),
            "--remote-root", "/tmp/bench",
            "--build-id", "candidate-a",
            "--storage-class", "wsl-native-oracle-home",
            "--direction", direction,
            "--rusty-auth", auth,
        ]
        if rusty_only:
            argv.append("--rusty-only")
        if profile:
            argv.append("--payload-profile")
        if remote_inputs is not None:
            argv.extend(["--remote-input-64", remote_inputs[0], "--remote-input-512", remote_inputs[1]])
        parser = runner.build_parser()
        return parser, parser.parse_args(argv)

    def test_rusty_only_does_not_require_croc_paths_or_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parser, args = self.parse(temp_dir, rusty_only=True)
            runner.validate_local_args(parser, args)
            self.assertIsNone(args.croc)
            self.assertIsNone(args.remote_croc)

    def test_profile_flag_is_opt_in_and_clears_inherited_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {"RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE": "1"}
            runner.configure_profile_env(env, SimpleNamespace())
            self.assertNotIn("RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE", env)

            parser, args = self.parse(temp_dir, rusty_only=True, profile=True)
            self.assertTrue(args.payload_profile)
            env = {}
            runner.configure_profile_env(env, args)
            self.assertEqual(env["RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE"], "1")

            command = runner.remote_process_command(
                SimpleNamespace(
                    rusty_path="direct",
                    rusty_auth="invite",
                    payload_profile=True,
                ),
                "/tmp/run",
                ["/opt/rustytransfer", "send"],
                "/tmp/run/metrics.jsonl",
                "/tmp/run/time.json",
                "/tmp/run/sender.log",
                "/tmp/run/sender.pid",
            )
            self.assertIn("env -u RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE", command)
            self.assertIn("RUSTYTRANSFER_BENCH_PAYLOAD_PROFILE=1", command)

    def test_comparison_requires_both_croc_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parser, args = self.parse(temp_dir, rusty_only=False)
            with self.assertRaises(SystemExit):
                runner.validate_local_args(parser, args)

    def test_reverse_direction_is_available_only_for_rusty_only(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parser, args = self.parse(temp_dir, rusty_only=True, direction="oracle-to-wsl")
            runner.validate_local_args(parser, args)
            self.assertEqual(args.direction, "oracle-to-wsl")

    def test_reverse_croc_comparison_is_rejected_explicitly(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parser, args = self.parse(temp_dir, rusty_only=False, direction="oracle-to-wsl")
            error = io.StringIO()
            with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
                runner.validate_local_args(parser, args)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Croc reverse comparison is unavailable", error.getvalue())

    def test_pre_staged_inputs_require_a_pair_and_reverse_direction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parser, args = self.parse(
                temp_dir,
                rusty_only=True,
                direction="oracle-to-wsl",
                remote_inputs=("/oracle/inputs/64.bin", "/oracle/inputs/512.bin"),
            )
            runner.validate_local_args(parser, args)

        with tempfile.TemporaryDirectory() as temp_dir:
            parser, args = self.parse(
                temp_dir,
                rusty_only=True,
                remote_inputs=("/oracle/inputs/64.bin", "/oracle/inputs/512.bin"),
            )
            with self.assertRaises(SystemExit):
                runner.validate_local_args(parser, args)

        with tempfile.TemporaryDirectory() as temp_dir:
            parser, args = self.parse(temp_dir, rusty_only=True, direction="oracle-to-wsl")
            args.remote_input_64 = "/oracle/inputs/64.bin"
            with self.assertRaises(SystemExit):
                runner.validate_local_args(parser, args)

    def test_pre_staged_source_is_verified_without_scp_or_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _parser, args = self.parse(
                temp_dir,
                rusty_only=True,
                direction="oracle-to-wsl",
                remote_inputs=("/oracle/inputs/64.bin", "/oracle/inputs/512.bin"),
            )
            with patch.object(runner, "verify_remote_file", return_value="a" * 64) as verify:
                with patch.object(runner, "copy_to_remote") as copy:
                    selected, temporary = runner.prepare_remote_source(
                        args,
                        Path("input-64.bin"),
                        "/tmp/bench/trial/source.bin",
                        64,
                        64 * 1024 * 1024,
                        "a" * 64,
                    )
            self.assertEqual(selected, "/oracle/inputs/64.bin")
            self.assertFalse(temporary)
            verify.assert_called_once_with(
                args, "/oracle/inputs/64.bin", 64 * 1024 * 1024, "a" * 64
            )
            copy.assert_not_called()
            files = runner.reverse_trial_cleanup_files(
                selected,
                temporary,
                "/tmp/bench/trial/sender.jsonl",
                "/tmp/bench/trial/sender.time.json",
                "/tmp/bench/trial/sender.log",
                "/tmp/bench/trial/sender.pid",
            )
            self.assertNotIn(selected, files)

    def test_pre_staged_source_must_match_expected_size_and_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _parser, args = self.parse(
                temp_dir,
                rusty_only=True,
                direction="oracle-to-wsl",
                remote_inputs=("/oracle/inputs/64.bin", "/oracle/inputs/512.bin"),
            )
            with patch.object(runner, "remote", return_value=f"12\n{'a' * 64}  file"):
                self.assertEqual(
                    runner.verify_remote_file(args, "/oracle/inputs/64.bin", 12, "a" * 64),
                    "a" * 64,
                )
            with patch.object(runner, "remote", return_value=f"11\n{'a' * 64}  file"):
                with self.assertRaisesRegex(RuntimeError, "remote file mismatch"):
                    runner.verify_remote_file(args, "/oracle/inputs/64.bin", 12, "a" * 64)
            with patch.object(runner, "remote", return_value=f"12\n{'b' * 64}  file"):
                with self.assertRaisesRegex(RuntimeError, "remote file mismatch"):
                    runner.verify_remote_file(args, "/oracle/inputs/64.bin", 12, "a" * 64)

    def test_reverse_direction_dispatches_to_remote_sender_runner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _parser, args = self.parse(
                temp_dir, rusty_only=True, direction="oracle-to-wsl", auth="invite"
            )
            with patch.object(runner, "run_rusty_reverse", return_value=([{"role": "sender"}], "direct")) as reverse:
                result = runner.run_rusty(
                    args, Path("input.bin"), 64, 64 * 1024 * 1024,
                    "a" * 64, Path(temp_dir), 1, False
                )
        self.assertEqual(result, ([{"role": "sender"}], "direct"))
        reverse.assert_called_once()

    def test_binary_hashes_follow_sender_and_receiver_roles_in_both_directions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _parser, args = self.parse(temp_dir, rusty_only=True)
            args.local_rusty_sha256 = "a" * 64
            args.remote_rusty_sha256 = "b" * 64
            forward = runner.rust_provenance(args)
            args.direction = "oracle-to-wsl"
            reverse = runner.rust_provenance(args)
        self.assertEqual(forward["sender_binary_sha256"], "a" * 64)
        self.assertEqual(forward["receiver_binary_sha256"], "b" * 64)
        self.assertEqual(reverse["sender_binary_sha256"], "b" * 64)
        self.assertEqual(reverse["receiver_binary_sha256"], "a" * 64)

    def test_remote_sender_command_uses_invite_mode_and_scoped_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _parser, args = self.parse(
                temp_dir, rusty_only=True, direction="oracle-to-wsl", auth="invite"
            )
            command = runner.remote_sender_command(
                args,
                "/tmp/bench/run",
                "/tmp/bench/run/source.bin",
                "/tmp/bench/run/sender.jsonl",
                "/tmp/bench/run/sender.time.json",
                "/tmp/bench/run/sender.log",
                "/tmp/bench/run/sender.pid",
            )
        self.assertIn("setsid --wait", command)
        self.assertIn("--direct", command)
        self.assertIn("--file /tmp/bench/run/source.bin", command)
        self.assertIn(">/tmp/bench/run/sender.log 2>&1 < /dev/null", command)

    def test_remote_cleanup_checks_trial_cwd_and_process_group(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _parser, args = self.parse(
                temp_dir, rusty_only=True, direction="oracle-to-wsl", auth="invite"
            )
            with patch.object(runner, "remote") as remote:
                runner.terminate_remote_process_group(
                    args, "/tmp/bench/run/sender.pid", "/tmp/bench/run"
                )
        command = remote.call_args.args[1]
        self.assertIn("ps -o pgid=", command)
        self.assertIn("/proc/\"$pid\"/cwd", command)
        self.assertIn("test \"$cwd\" = \"$trial_dir\"", command)
        self.assertIn("readlink -f -- /tmp/bench/run", command)
        self.assertIn("kill -TERM -- \"-$pid\"", command)

    def test_remote_log_scrubber_removes_invites_even_without_a_parsed_value(self):
        args = SimpleNamespace(ssh="ssh", ssh_key=Path("key"), user="ubuntu", host="oracle")
        with patch.object(runner, "remote") as remote:
            runner.redact_remote_direct_invites(args, "/tmp/bench/run/sender.log")
        command = remote.call_args.args[1]
        self.assertIn("python3 -c", command)
        self.assertIn("rt1:", command)
        self.assertIn("/tmp/bench/run/sender.log", command)

    def test_local_destination_verification_checks_size_and_full_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "received.bin"
            destination.write_bytes(b"benchmark payload")
            digest = runner.sha256_file(destination)
            self.assertEqual(
                runner.verify_local_file(destination, len(b"benchmark payload"), digest),
                digest,
            )
            with self.assertRaisesRegex(RuntimeError, "local file mismatch"):
                runner.verify_local_file(destination, len(b"benchmark payload") - 1, digest)
            with self.assertRaisesRegex(RuntimeError, "local file mismatch"):
                runner.verify_local_file(destination, len(b"benchmark payload"), "0" * 64)


class ResourceMappingTests(unittest.TestCase):
    def test_resource_values_are_assigned_by_endpoint_role(self):
        resources = runner.resource_values(
            {"user_cpu_seconds": 1.0, "system_cpu_seconds": 0.5, "max_rss_kib": 100},
            {"user_cpu_seconds": 2.0, "system_cpu_seconds": 0.25, "max_rss_kib": 200},
        )
        self.assertEqual(resources["sender_cpu_seconds"], 1.5)
        self.assertEqual(resources["receiver_cpu_seconds"], 2.25)
        self.assertEqual(resources["sender_max_rss_kib"], 100)
        self.assertEqual(resources["receiver_max_rss_kib"], 200)


@unittest.skipUnless(os.name == "posix" and shutil.which("setsid"), "requires POSIX setsid")
class RemoteProcessLifecycleTests(unittest.TestCase):
    def test_child_survives_launcher_exit_without_holding_ssh_pipe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pid_path = root / "sender.pid"
            command = runner.remote_process_command(
                SimpleNamespace(rusty_path="direct", rusty_auth="invite"),
                str(root),
                ["/bin/sleep", "30"],
                str(root / "sender.jsonl"),
                str(root / "sender.time.json"),
                str(root / "sender.log"),
                str(pid_path),
            )
            launcher = subprocess.Popen(
                ["/bin/sh", "-c", command],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            child_pid = None
            try:
                deadline = time.monotonic() + 3
                while not pid_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(pid_path.exists(), "detached child did not publish its PID")
                child_pid = int(pid_path.read_text(encoding="utf-8").strip())
                self.assertEqual(os.getpgid(child_pid), child_pid)
                self.assertEqual(os.getsid(child_pid), child_pid)
                self.assertEqual(os.readlink(f"/proc/{child_pid}/cwd"), str(root))

                launcher.kill()
                launcher.communicate(timeout=2)
                os.killpg(child_pid, 0)
            finally:
                if launcher.poll() is None:
                    launcher.kill()
                    launcher.wait(timeout=2)
                if child_pid is not None:
                    try:
                        os.killpg(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        try:
                            os.killpg(child_pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.01)
                    else:
                        try:
                            os.killpg(child_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
