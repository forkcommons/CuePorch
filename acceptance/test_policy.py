"""Exercise every configured policy on fresh and reopened institutional databases."""

import json
import secrets
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from institution import STEPS, create_app


class Policy(unittest.TestCase):
    def test_all_steps(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as services:
            # These workers hold Windows files and are unrelated to the policy API.
            for worker in ("library_worker", "library_caption_jobs", "review_push"):
                services.enter_context(patch(f"institution.{worker}.start"))
            root = Path(temp)
            secret = secrets.token_hex(24)
            headers = {"Authorization": "Bearer " + secret}

            def institution(name):
                library = root / name
                library.mkdir(exist_ok=True)
                (library / "manifest.json").write_text(
                    json.dumps(dict(institution=name, schema=1, works=[]))
                )
                app = create_app(library, root / (name + ".db"), secret, name)
                services.callback(app.extensions["stop_event_capture"])
                services.callback(app.extensions["stop_editor"])
                services.callback(app.extensions["stop_file_links"])
                return app.test_client()

            first = institution("station")
            other = institution("arts")
            self.assertEqual(first.get("/api/policy").status_code, 403)
            self.assertEqual(
                first.get("/api/policy", headers=headers).json,
                {key: False for key in STEPS},
            )
            for step in STEPS:

                def put(client, path, data):
                    return client.put(path, headers=headers, json=data)

                policy = "/api/policy/" + step
                action = "/api/actions/" + step
                body = dict(step=step, payload={"revision": "a"})
                self.assertEqual(
                    put(first, action + "-auto", body).json["status"], "eligible"
                )
                put(first, policy, {"required": True})
                self.assertFalse(other.get("/api/policy", headers=headers).json[step])
                for suffix in ("released", "rejected", "approved"):
                    self.assertEqual(
                        put(first, action + suffix, body).json["status"], "waiting"
                    )
                first = institution("station")
                self.assertTrue(first.get("/api/policy", headers=headers).json[step])
                for suffix, decision in [
                    ("rejected", "reject"),
                    ("approved", "approve"),
                ]:
                    self.assertEqual(
                        first.post(
                            action + suffix + "/decision",
                            headers=headers,
                            json={"decision": decision},
                        ).status_code,
                        204,
                    )
                put(first, policy, {"required": False})
                for suffix, state, decider in [
                    ("rejected", "rejected", "operator"),
                    ("approved", "eligible", "operator"),
                    ("released", "eligible", None),
                ]:
                    rec = first.get(action + suffix, headers=headers).json
                    self.assertEqual((rec["status"], rec["actor"]), (state, decider))
                    self.assertEqual(
                        put(first, action + suffix, body).json["status"], state
                    )
            public = dict(
                destination="http://localhost:19000/videos/embed/one",
                public_authorized=True,
                description="Meeting",
                revision="live",
            )
            path = "/api/routes/meeting/weekly"
            put(first, path, public)
            put(first, "/api/policy/replay", {"required": True})
            replay = dict(
                public,
                destination="http://localhost:19000/videos/embed/two",
                revision="replay",
            )
            put(first, path, replay)
            self.assertEqual(
                first.get("/embed/meeting/weekly").location, public["destination"]
            )
            put(first, "/api/policy/replay", {"required": False})
            self.assertEqual(
                first.get("/embed/meeting/weekly").location, replay["destination"]
            )
            put(first, path, public)
            self.assertEqual(
                first.get("/embed/meeting/weekly").location, replay["destination"]
            )


if __name__ == "__main__":
    unittest.main()
