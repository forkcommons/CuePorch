"""Structured generation using a workspace's configured loopback Ollama runtime."""

import json
import os
from threading import Event, Thread
from urllib.parse import urlsplit

from filelock import FileLock, Timeout
import requests


class ModelError(ValueError):
    pass


def loopback_origin(value):
    try:
        origin = urlsplit(value)
        if (origin.scheme not in ("http", "https") or origin.hostname not in ("localhost", "127.0.0.1", "::1")
                or origin.username is not None or origin.password is not None or origin.path not in ("", "/")
                or origin.query or origin.fragment or origin.port == 0):
            return None
        return origin._replace(path="").geturl()
    except ValueError:
        return None


def runtime():
    model = os.environ.get("CUEPORCH_OLLAMA_MODEL", "").strip()
    origin = loopback_origin(os.environ.get("CUEPORCH_OLLAMA_HOST", "http://127.0.0.1:11434"))
    return (origin, model) if origin and model and not model.casefold().endswith(":cloud") else None


def generate(prompt, schema):
    configured = runtime()
    if not configured:
        raise ModelError("Configure a local Ollama model before using generation or video questions.")
    origin, model = configured
    with requests.Session() as session:
        session.trust_env = False
        try:
            with session.post(origin + "/api/generate", json={
                "model": model, "prompt": prompt, "format": schema, "stream": False,
                "options": {"temperature": 0, "num_predict": 2400},
            }, timeout=(4, 120), allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ModelError("The configured local model could not generate a result.")
                data = bytearray()
                for chunk in response.iter_content(8192):
                    data.extend(chunk)
                    if len(data) > 128 * 1024:
                        raise ModelError("The configured local model returned too much data.")
                body = json.loads(data)
                if not isinstance(body, dict) or body.get("done") is not True or body.get("done_reason") == "length":
                    raise ModelError("The configured local model returned an incomplete result.")
                return json.loads(body["response"])
        except (requests.RequestException, OSError) as exc:
            raise ModelError("The configured local model is unavailable. Start it and retry.") from exc
        except (json.JSONDecodeError, UnicodeError, KeyError, TypeError) as exc:
            raise ModelError("The configured local model returned invalid JSON.") from exc


def start_worker(app, lock_path, label, process):
    if not runtime():
        return lambda: None
    stopping = Event()

    def run():
        lock = FileLock(str(lock_path), timeout=0)
        while not stopping.wait(1):
            try:
                with lock:
                    process(stopping)
            except Timeout:
                pass
            except Exception:
                app.logger.exception("%s could not process its queue", label)

    worker = Thread(target=run, name=label, daemon=True)
    worker.start()

    def stop():
        stopping.set()
        worker.join(timeout=6)

    return stop
