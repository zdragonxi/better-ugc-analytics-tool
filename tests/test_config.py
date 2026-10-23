"""Configuration loading and cookie handling."""
import os
import unittest

from helpers import FAKE_COOKIE, GID_A, TmpDirCase, make_cfg

from roblox_sales import config


class ConfigTests(TmpDirCase):
    def write(self, text, name="config.ini"):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_groups_from_ini_and_env(self):
        p = self.write(f"[group:{GID_A}]\nname = Alpha\ncombine = false\n")
        cfg = config.load(p, environ={"RSA_GROUPS": "555:Env Store, 777:Other"}, read_secret=False)
        srcs = [(s.source, s.name, s.combine) for s in cfg.stores]
        self.assertEqual(srcs, [(f"group:{GID_A}", "Alpha", False),
                                ("group:555", "Env Store", True),
                                ("group:777", "Other", True),
                                ("user", "My account", True)])

    def test_env_overrides_file(self):
        p = self.write("[app]\nport = 9000\n")
        self.assertEqual(config.load(p, environ={"RSA_PORT": "9100"}, read_secret=False).port, 9100)

    def test_dotenv_is_read_but_real_env_wins(self):
        p = self.write("[app]\n")
        self.write("RSA_PORT=9200\nRSA_PROXY='http://proxy.example:3128'\n", ".env")
        cfg = config.load(p, environ={"RSA_PORT": "9300"}, read_secret=False)
        self.assertEqual((cfg.port, cfg.proxy), (9300, "http://proxy.example:3128"))

    def test_cookie_from_env_or_file_never_in_repr(self):
        p = self.write("[app]\ncookie_file = secret.txt\n")
        cfg = config.load(p, environ={"RSA_COOKIE": FAKE_COOKIE})
        self.assertEqual(cfg.cookie(), FAKE_COOKIE)
        self.assertNotIn(FAKE_COOKIE, repr(cfg))
        cp = os.path.join(self.tmp, "secret.txt")
        with open(cp, "w") as f:
            f.write(FAKE_COOKIE + "\n")
        if os.name != "nt":
            os.chmod(cp, 0o600)
        self.assertEqual(config.load(p, environ={}).cookie(), FAKE_COOKIE)

    @unittest.skipIf(os.name == "nt", "POSIX file modes only")
    def test_world_readable_cookie_file_is_refused(self):
        p = self.write("[app]\ncookie_file = secret.txt\n")
        cp = os.path.join(self.tmp, "secret.txt")
        with open(cp, "w") as f:
            f.write(FAKE_COOKIE)
        os.chmod(cp, 0o644)
        with self.assertRaises(config.ConfigError) as e:
            config.load(p, environ={})
        self.assertNotIn(FAKE_COOKIE, str(e.exception))

    def test_no_cookie_is_fine(self):
        p = self.write("[app]\n")
        self.assertEqual(config.load(p, environ={}).cookie(), "")

    def test_remote_bind_needs_a_password(self):
        p = self.write("[app]\nhost = 0.0.0.0\n")
        with self.assertRaises(config.ConfigError):
            config.load(p, environ={}, read_secret=False)
        cfg = config.load(p, environ={"RSA_PASSWORD_HASH": "pbkdf2_sha256$1$AA==$AA=="}, read_secret=False)
        self.assertFalse(cfg.is_loopback)

    def test_bad_values(self):
        for text in ("[app]\nport = abc\n", "[group:notanumber]\n", "[app]\nasset_types = 8,hat\n",
                     "[app]\ncatalog_interval = 60\n", "[app]\nsync_interval = 5\n"):
            with self.assertRaises(config.ConfigError, msg=text):
                config.load(self.write(text), environ={}, read_secret=False)

    def test_asset_types_default_and_override(self):
        p = self.write("[app]\n")
        cfg = config.load(p, environ={}, read_secret=False)
        self.assertEqual(cfg.asset_types, list(config.DEFAULT_ASSET_TYPES))
        self.assertNotIn(2, cfg.asset_types)            # classic clothing is opt-in
        cfg = config.load(p, environ={"RSA_ASSET_TYPES": "8, 41,2"}, read_secret=False)
        self.assertEqual(cfg.asset_types, [8, 41, 2])
        self.assertEqual(cfg.catalog_interval, 600)

    def test_user_store_optional(self):
        cfg = make_cfg(self.tmp, user=False)
        self.assertEqual([s.source for s in cfg.stores], [f"group:{GID_A}", "group:2222222"])
