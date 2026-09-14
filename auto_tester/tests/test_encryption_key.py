from __future__ import annotations

from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from auto_tester.encryption_key import validate_fernet_key


class ValidateFernetKeyTests(SimpleTestCase):
    def test_valid_fernet_key_returned_unchanged(self) -> None:
        valid_key = Fernet.generate_key().decode("ascii")
        result = validate_fernet_key(valid_key)
        self.assertEqual(result, valid_key)

    def test_invalid_key_raises_improperly_configured(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            validate_fernet_key("not-a-key")

    def test_wrong_base64_raises_improperly_configured(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            validate_fernet_key("!" * 44)
