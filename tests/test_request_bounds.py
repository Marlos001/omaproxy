import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import omaproxy


class ResponseBoundsTests(unittest.TestCase):
    def request(self, body):
        response = io.BytesIO(body)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(omaproxy, "REQUEST_MAX_BYTES", 32), \
             patch.object(omaproxy.urllib.request, "build_opener", return_value=opener):
            return omaproxy.request("http://127.0.0.1:18317/v0/management/auth-files", "fake-key")

    def test_exact_limit_json_is_accepted(self):
        self.assertEqual(self.request(b'"' + b'x' * 30 + b'"'), 'x' * 30)

    def test_oversized_response_fails_before_json_decoding_without_echoing_body(self):
        with self.assertRaisesRegex(ValueError, "JSON size limit") as error:
            self.request(b'{"secret": "' + b'x' * 100 + b'"}')
        self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
