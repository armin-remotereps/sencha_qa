from __future__ import annotations

from cryptography.fernet import Fernet
from django.test import SimpleTestCase, override_settings

from projects.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret

TEST_KEY = Fernet.generate_key().decode("ascii")
OTHER_KEY = Fernet.generate_key().decode("ascii")


@override_settings(FIELD_ENCRYPTION_KEY=TEST_KEY)
class SecretRoundTripTests(SimpleTestCase):
    def test_encrypt_then_decrypt_returns_original(self) -> None:
        token = encrypt_secret("0eyzDpbYhU9PyC699.3r-njX..ViodS/uBW3jGGob")
        self.assertNotIn("0eyzDpbYhU9", token)
        self.assertEqual(
            decrypt_secret(token), "0eyzDpbYhU9PyC699.3r-njX..ViodS/uBW3jGGob"
        )

    def test_two_encryptions_differ_but_decrypt_equal(self) -> None:
        first = encrypt_secret("same")
        second = encrypt_secret("same")
        self.assertNotEqual(first, second)
        self.assertEqual(decrypt_secret(first), decrypt_secret(second))

    def test_garbage_token_raises(self) -> None:
        with self.assertRaises(SecretDecryptionError):
            decrypt_secret("not-a-token")

    def test_token_from_other_key_raises(self) -> None:
        with override_settings(FIELD_ENCRYPTION_KEY=OTHER_KEY):
            token = encrypt_secret("value")
        with self.assertRaises(SecretDecryptionError):
            decrypt_secret(token)
