"""Offline shell transport fixtures: fake curl/python, never a real installer."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


DIRECTORY = Path(__file__).resolve().parent.parent
URL = ("https://raw.githubusercontent.com/FrankRamm/MISC/codex/universal-accel-homing/"
       "Beacon_Accel_Homing/bootstrap.sh")
SHORT_COMMAND = 'installer=$(curl -fsSL %s) && bash -c "$installer"' % URL


class ShortBootstrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        git_bash = Path("C:/Program Files/Git/bin/bash.exe")
        cls.bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
        if not cls.bash:
            raise unittest.SkipTest("Bash unavailable for offline transport fixtures")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.payload = self.directory / "downloaded bootstrap.sh"
        self.calls = self.directory / "python-calls.txt"
        self.stdin = self.directory / "python-stdin.txt"
        self.curl_args = self.directory / "curl-args.txt"
        self.script = (DIRECTORY / "bootstrap.sh").read_text(encoding="utf-8")

    def run_shell(self, payload, command=SHORT_COMMAND, curl_status=0, arguments=()):
        self.payload.write_text(payload, encoding="utf-8", newline="\n")
        for path in (self.calls, self.stdin, self.curl_args):
            if path.exists():
                path.unlink()
        harness = r'''
python3() {
    printf '%s\n' "$@" >> "$PYTHON_CALLS"
    cat > "$PYTHON_STDIN"
    return 0
}
export -f python3
curl() {
    printf '%s\n' "$*" > "$CURL_ARGUMENTS"
    cat "$PAYLOAD_FILE"
    return "$CURL_STATUS"
}
''' + command
        environment = dict(os.environ, PAYLOAD_FILE=self.payload.as_posix(),
                           PYTHON_CALLS=self.calls.as_posix(), PYTHON_STDIN=self.stdin.as_posix(),
                           CURL_ARGUMENTS=self.curl_args.as_posix(), CURL_STATUS=str(curl_status))
        return subprocess.run([self.bash, "-c", harness, "--", *arguments], env=environment,
                              text=True, encoding="utf-8", stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=10)

    def test_short_command_buffers_success_and_keeps_default_source_options(self):
        result = self.run_shell(self.script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), ["-"])
        self.assertEqual(self.curl_args.read_text().strip(), "-fsSL " + URL)
        python_source = self.stdin.read_text()
        self.assertIn('default="FrankRamm/MISC"', python_source)
        self.assertIn('default="codex/universal-accel-homing"', python_source)
        self.assertIn('does not match the pinned Git blob size/hash', python_source)
        self.assertNotIn("--repo", SHORT_COMMAND)
        self.assertNotIn("--ref", SHORT_COMMAND)
        self.assertNotIn("|", SHORT_COMMAND)

    def test_short_command_forwards_optional_arguments_after_double_dash(self):
        command = SHORT_COMMAND + ' -- --dry-run --no-restart'
        result = self.run_shell(self.script, command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), ["-", "--dry-run", "--no-restart"])

    def test_failed_fetch_never_invokes_python_even_when_stdout_is_complete(self):
        for payload in (self.script, self.script[:self.script.rindex("\n)\n") + 3]):
            with self.subTest(complete=payload == self.script):
                result = self.run_shell(payload, curl_status=56)
                self.assertEqual(result.returncode, 56)
                self.assertFalse(self.calls.exists())
                self.assertFalse(self.stdin.exists())

    def test_successful_but_truncated_prefix_never_invokes_python(self):
        invocation = self.script.rindex("\n(\n") + 1
        ends = (1, invocation, invocation + 1, self.script.rindex(' "$@"'),
                self.script.rindex("\n)"), self.script.rindex("\n)\n") + 3,
                len(self.script.rstrip()) - 1)
        for end in ends:
            with self.subTest(prefix_length=end):
                result = self.run_shell(self.script[:end])
                self.assertFalse(self.calls.exists())
                # A comment or function-definitions-only prefix is a shell
                # no-op. Once the final invocation begins, truncation must
                # also fail explicitly; neither case may start Python.
                if end > invocation:
                    self.assertNotEqual(result.returncode, 0)

    def test_complete_file_invocation_has_the_same_guard_and_argument_forwarding(self):
        result = self.run_shell(self.script, 'bash "$PAYLOAD_FILE" --dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), ["-", "--dry-run"])
        self.assertFalse(self.curl_args.exists())

    def test_transport_successful_truncated_file_is_refused_before_python(self):
        prefix = self.script[:self.script.rindex("\n)\n") + 3]
        result = self.run_shell(prefix, 'bash "$PAYLOAD_FILE" --dry-run')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Incomplete bootstrap", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_direct_unbuffered_pipe_is_refused(self):
        result = self.run_shell(self.script, 'cat "$PAYLOAD_FILE" | bash -s -- --dry-run')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("complete buffered command or file", result.stderr)
        self.assertFalse(self.calls.exists())

    def test_guard_requires_the_final_marker_not_merely_an_earlier_occurrence(self):
        result = self.run_shell(self.script + '\nprintf "unexpected"\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Incomplete bootstrap", result.stderr)
        self.assertNotIn("unexpected", result.stdout)
        self.assertFalse(self.calls.exists())

    def test_final_marker_with_trailing_blank_lines_is_complete(self):
        result = self.run_shell(self.script + "\n\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
