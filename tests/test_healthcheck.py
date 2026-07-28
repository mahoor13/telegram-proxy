import os
import sys
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import healthcheck  # noqa: E402


class HealthcheckTest(unittest.TestCase):
    @patch("healthcheck.os.kill")
    @patch("healthcheck.urllib.request.urlopen")
    def test_success_does_not_terminate_gunicorn(self, urlopen, kill):
        response = Mock()
        response.status = 200
        urlopen.return_value.__enter__.return_value = response

        self.assertEqual(0, healthcheck.main())
        kill.assert_not_called()

    @patch("healthcheck.os.kill")
    @patch(
        "healthcheck.urllib.request.urlopen",
        side_effect=TimeoutError,
    )
    def test_failure_terminates_pid_one_for_docker_restart(self, _urlopen, kill):
        self.assertEqual(1, healthcheck.main())
        kill.assert_called_once_with(1, healthcheck.signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
